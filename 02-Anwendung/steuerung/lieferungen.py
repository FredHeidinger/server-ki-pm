"""Explizite technische Lieferbedarfe; keine Ableitung aus Freitext oder automatische Abnahme."""

import hashlib
import json
import re

from django.db import transaction
from django.db.models import F, Max

from . import dienst
from .models import Abhaengigkeit, Auftrag, Lieferbedarf, Lieferung, Meldung


def _aktuell(akteur, auftrag, revision):
    aktuell = Auftrag.objects.select_related("projekt").get(pk=auftrag.pk)
    dienst._projekt_sichtbar(akteur, aktuell.projekt)
    if type(revision) is not int or revision != aktuell.revision_nr:
        raise dienst.Konflikt("Veraltete Auftragsrevision.")
    if not aktuell.projekt.aktiv or aktuell.ausfuehrungsweg != Auftrag.WEG_CHAT:
        raise dienst.Konflikt("Lieferautomatik gilt nur für aktive Chat-Aufträge.")
    if not aktuell.hat_bearbeiter or aktuell.uebernommene_revision != revision:
        raise dienst.Konflikt("Keine gültige Übernahme dieser Revision.")
    if aktuell.zustand not in Auftrag.BEARBEITER_ZUSTAENDE:
        raise dienst.Konflikt("Auftrag ist nicht in Bearbeitung.")
    return aktuell


def _lieferkennung(wert):
    if not isinstance(wert, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,79}", wert):
        raise dienst.DienstFehler("Ungültiger Lieferschlüssel (kleine Buchstaben, Ziffern, ._-).")
    return wert


def _zeilen(wert, name):
    if not isinstance(wert, list) or not 1 <= len(wert) <= 100:
        raise dienst.DienstFehler(f"{name} braucht 1 bis 100 nichtleere Einträge.")
    if any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in wert):
        raise dienst.DienstFehler(f"Ungültige Einträge in {name}.")
    return sorted(set(x.strip() for x in wert))


def _blocker(auftrag):
    return auftrag.meldungen.filter(
        revision_nr=auftrag.revision_nr,
        historisch=False,
        art__in=(*Meldung.BLOCKIERENDE, Meldung.ANTWORT),
    ).first()


def _kreis(auftrag, quelle):
    offen, gesehen = {quelle.pk}, set()
    while offen:
        if auftrag.pk in offen:
            raise dienst.DienstFehler("Der Lieferbedarf würde einen Abhängigkeitskreis erzeugen.")
        gesehen |= offen
        vorgaenger = set(
            Abhaengigkeit.objects.filter(auftrag_id__in=offen)
            .exclude(vorgaenger__zustand__in=Auftrag.ENDZUSTAENDE)
            .values_list("vorgaenger_id", flat=True)
        )
        bedarfe = Lieferbedarf.objects.filter(
            meldung__auftrag_id__in=offen,
            meldung__auftrag__zustand=Auftrag.BLOCKIERT,
            meldung__revision_nr=F("meldung__auftrag__revision_nr"),
            erfuellt_durch__isnull=True,
        ).select_related("meldung__auftrag")
        for bedarf in bedarfe:
            aktuell = _blocker(bedarf.meldung.auftrag)
            if aktuell and aktuell.pk == bedarf.meldung_id:
                vorgaenger.add(bedarf.lieferauftrag_id)
        offen = vorgaenger - gesehen


def _bedarf_pruefen(akteur, auftrag, bedarf):
    if not isinstance(bedarf, list) or not 1 <= len(bedarf) <= 20:
        raise dienst.DienstFehler("Lieferbedarf braucht 1 bis 20 konkrete Lieferungen.")
    geprueft, eindeutig = [], set()
    for eintrag in bedarf:
        if not isinstance(eintrag, dict) or set(eintrag) - {
            "auftrag",
            "revision",
            "lieferkennung",
            "nach_lieferung",
        }:
            raise dienst.DienstFehler("Ungültiger Lieferbedarf.")
        quelle = dienst.auftrag_holen(
            akteur, dienst.auftrag_id_aus_nummer(eintrag.get("auftrag", ""))
        )
        if quelle.projekt_id != auftrag.projekt_id:
            raise dienst.KeinZugriff("Lieferungen müssen zum selben Projekt gehören.")
        revision = eintrag.get("revision")
        if type(revision) is not int or revision != quelle.revision_nr:
            raise dienst.Konflikt("Die Lieferrevision ist nicht mehr aktuell.")
        if quelle.ausfuehrungsweg != Auftrag.WEG_CHAT:
            raise dienst.Konflikt("Werkbank-Lieferungen brauchen ihren eigenen Vertragsweg.")
        if quelle.zustand == Auftrag.ABGEBROCHEN:
            raise dienst.Konflikt("Ein abgebrochener Auftrag liefert keinen neuen Anschluss.")
        lieferkennung = _lieferkennung(eintrag.get("lieferkennung"))
        nach = eintrag.get("nach_lieferung", 0)
        if type(nach) is not int or nach < 0:
            raise dienst.DienstFehler("nach_lieferung muss eine nichtnegative Liefernummer sein.")
        if (
            nach
            and not Lieferung.objects.filter(
                pk=nach, auftrag=quelle, revision_nr=revision, lieferkennung=lieferkennung
            ).exists()
        ):
            raise dienst.DienstFehler("nach_lieferung gehört nicht zu diesem Lieferprofil.")
        # Bereits zum Wiederaufnehmen benutzte Lieferungen nie nochmals als neu werten.
        verbraucht = (
            Lieferbedarf.objects.filter(
                meldung__auftrag=auftrag,
                meldung__revision_nr=auftrag.revision_nr,
                lieferauftrag=quelle,
                lieferrevision=revision,
                lieferkennung=lieferkennung,
            ).aggregate(n=Max("erfuellt_durch_id"))["n"]
            or 0
        )
        nach = max(nach, verbraucht)
        ident = (quelle.pk, revision, lieferkennung)
        if ident in eindeutig:
            raise dienst.DienstFehler("Doppelter Lieferbedarf.")
        eindeutig.add(ident)
        _kreis(auftrag, quelle)
        geprueft.append((quelle, revision, lieferkennung, nach))
    return geprueft


def _registrieren(akteur, auftrag, meldung, geprueft):
    vorhanden = set(
        meldung.lieferbedarf.values_list(
            "lieferauftrag_id", "lieferrevision", "lieferkennung", "nach_lieferung"
        )
    )
    erwartet = {(a.pk, r, s, n) for a, r, s, n in geprueft}
    if vorhanden:
        if vorhanden != erwartet:
            raise dienst.Konflikt("Dieser Blocker hat bereits einen anderen Lieferbedarf.")
    else:
        Lieferbedarf.objects.bulk_create(
            [
                Lieferbedarf(
                    meldung=meldung,
                    lieferauftrag=a,
                    lieferrevision=r,
                    lieferkennung=s,
                    nach_lieferung=n,
                )
                for a, r, s, n in geprueft
            ]
        )
        dienst._ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Technischen Lieferbedarf registriert",
            revision=str(auftrag.revision_nr),
        )
    _freisetzen(akteur, meldung)


@dienst._ohne_aktive_werkbank("Auf technische Lieferung warten")
def warten(akteur, auftrag, revision, *, text, bedarf, nur_lieferungen):
    with transaction.atomic():
        aktuell = _aktuell(akteur, auftrag, revision)
        if not akteur.ist_bearbeiter_von(aktuell):
            raise dienst.KeinZugriff("Nur der eigene Bearbeiter meldet diesen Lieferbedarf.")
        if nur_lieferungen is not True:
            raise dienst.DienstFehler(
                "Lieferautomatik setzt ausschließlich technische Lieferungen voraus."
            )
        if aktuell.zustand == Auftrag.BLOCKIERT:
            bestehend = _blocker(aktuell)
            if (
                bestehend is None
                or bestehend.art != Meldung.HINDERNIS
                or not bestehend.lieferbedarf.exists()
            ):
                raise dienst.Konflikt(
                    "Eine bestehende Frage, ein Fehler oder untypisierter Blocker wird "
                    "nicht durch eigenen Lieferbedarf ersetzt; "
                    "zuerst klären oder vom Manager einordnen."
                )
        geprueft = _bedarf_pruefen(akteur, aktuell, bedarf)
        meldung = dienst._meldung(
            akteur,
            aktuell,
            revision,
            Meldung.HINDERNIS,
            text,
            "Wartet auf technische Lieferung",
            neuer_zustand=Auftrag.BLOCKIERT,
        )
        aktuell.refresh_from_db()
        _registrieren(akteur, aktuell, meldung, geprueft)
    auftrag.refresh_from_db()
    return meldung


@dienst._ohne_aktive_werkbank("Lieferbedarf festlegen")
def festlegen(akteur, auftrag, revision, *, bedarf, nur_lieferungen, hindernis_id=None):
    """Einmalige Einordnung vorhandener Blockaden durch Fred/Manager; kein neuer Lebensimpuls."""
    dienst._planung_erforderlich(akteur, "Technischen Lieferbedarf festlegen")
    with transaction.atomic():
        aktuell = _aktuell(akteur, auftrag, revision)
        meldung = _blocker(aktuell)
        if (
            nur_lieferungen is not True
            or aktuell.zustand != Auftrag.BLOCKIERT
            or meldung is None
            or meldung.art != Meldung.HINDERNIS
        ):
            raise dienst.DienstFehler(
                "Nur einen ausschließlich technischen Hindernisblocker einordnen."
            )
        if type(hindernis_id) is not int:
            raise dienst.DienstFehler("Die zuvor gelesene hindernis_id ist erforderlich.")
        if meldung.pk != hindernis_id:
            raise dienst.Konflikt("Die Hindernismeldung hat sich geändert; zuerst neu lesen.")
        geprueft = _bedarf_pruefen(akteur, aktuell, bedarf)
        _registrieren(akteur, aktuell, meldung, geprueft)
    auftrag.refresh_from_db()
    return meldung


def _freisetzen(akteur, meldung):
    auftrag = Auftrag.objects.select_related("projekt").get(pk=meldung.auftrag_id)
    letzter = _blocker(auftrag)
    if (
        auftrag.zustand != Auftrag.BLOCKIERT
        or auftrag.revision_nr != meldung.revision_nr
        or auftrag.uebernommene_revision != auftrag.revision_nr
        or not auftrag.hat_bearbeiter
        or not auftrag.projekt.aktiv
        or auftrag.ausfuehrungsweg != Auftrag.WEG_CHAT
        or dienst._werkbank_aktiv(auftrag)
        or letzter is None
        or letzter.pk != meldung.pk
    ):
        return
    passend = []
    for bedarf in meldung.lieferbedarf.select_related("lieferauftrag"):
        if (
            bedarf.lieferauftrag.revision_nr != bedarf.lieferrevision
            or bedarf.lieferauftrag.projekt_id != auftrag.projekt_id
            or bedarf.lieferauftrag.zustand == Auftrag.ABGEBROCHEN
            or bedarf.lieferauftrag.ausfuehrungsweg != Auftrag.WEG_CHAT
        ):
            return
        lieferung = (
            Lieferung.objects.filter(
                auftrag=bedarf.lieferauftrag,
                revision_nr=bedarf.lieferrevision,
                lieferkennung=bedarf.lieferkennung,
                pk__gt=bedarf.nach_lieferung,
            )
            .order_by("-pk")
            .first()
        )
        if lieferung is None:
            return
        passend.append((bedarf, lieferung))
    if not passend:
        return
    # Aufruf ausschließlich innerhalb derselben IMMEDIATE-Transaktion wie Registrierung/Lieferung.
    if (
        Auftrag.objects.filter(pk=auftrag.pk, zustand=Auftrag.BLOCKIERT).update(
            zustand=Auftrag.IN_ARBEIT
        )
        != 1
    ):
        return
    for bedarf, lieferung in passend:
        bedarf.erfuellt_durch = lieferung
        bedarf.save(update_fields=["erfuellt_durch"])
    bezuege = ", ".join(
        f"{lieferung.auftrag.nummer}/{lieferung.lieferkennung} L-{lieferung.pk}"
        for _, lieferung in passend
    )
    Meldung.objects.create(
        auftrag=auftrag,
        revision_nr=auftrag.revision_nr,
        art=Meldung.ANTWORT,
        akteur_name="KI-PM (Lieferautomatik)",
        text=f"Technische Lieferung gemeldet: {bezuege}. "
        "Automatisch zur Anschlussprüfung wiederaufgenommen. "
        "Lieferstände/Prüfbelege selbst prüfen; keine Abnahme, Bindung oder neue Berechtigung.",
    )
    dienst._ereignis(
        akteur,
        projekt=auftrag.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=auftrag.nummer,
        ereignis="Nach technischer Lieferung automatisch wiederaufgenommen",
        anlass=bezuege,
        revision=str(auftrag.revision_nr),
    )


@dienst._ohne_aktive_werkbank("Lieferung melden")
def melden(
    akteur,
    auftrag,
    revision,
    *,
    lieferkennung,
    zusammenfassung,
    dateien,
    pruefungen,
    fehlgeschlagen=(),
):
    with transaction.atomic():
        aktuell = _aktuell(akteur, auftrag, revision)
        if not akteur.ist_bearbeiter_von(aktuell):
            raise dienst.KeinZugriff("Nur der tatsächliche Bearbeiter meldet diese Lieferung.")
        lieferkennung = _lieferkennung(lieferkennung)
        if not isinstance(zusammenfassung, str) or not zusammenfassung.strip():
            raise dienst.DienstFehler("Zusammenfassung fehlt.")
        dateien = _zeilen(dateien, "Dateien")
        pruefungen = _zeilen(pruefungen, "Prüfungen")
        if not isinstance(fehlgeschlagen, (list, tuple)) or fehlgeschlagen:
            raise dienst.DienstFehler(
                "Eine fehlgeschlagene Lieferung hebt keinen Lieferbedarf auf."
            )
        for datei in dateien:
            pfad, trenn, commit = datei.rpartition("@")
            if (
                not pfad.strip()
                or not trenn
                or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
            ):
                raise dienst.DienstFehler("Dateien brauchen pfad@vollständigem Git-Commit.")
        inhalt = dict(
            lieferkennung=lieferkennung,
            zusammenfassung=zusammenfassung.strip(),
            dateien=dateien,
            pruefungen=pruefungen,
        )
        # Geänderte Prosa/Prüfbehauptungen machen dieselben Dateien nicht zu einer neuen Lieferung.
        artefakte = dict(lieferkennung=lieferkennung, dateien=dateien)
        sha = hashlib.sha256(
            json.dumps(artefakte, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        vorhanden = Lieferung.objects.filter(
            auftrag=aktuell,
            revision_nr=revision,
            lieferkennung=lieferkennung,
            inhalt_sha256=sha,
        ).first()
        if vorhanden:
            return vorhanden
        meldung = dienst._meldung(
            akteur,
            aktuell,
            revision,
            Meldung.FORTSCHRITT,
            f"Lieferung {lieferkennung}: {zusammenfassung.strip()}",
            "Technische Lieferung gemeldet",
        )
        lieferung = Lieferung.objects.create(
            auftrag=aktuell,
            revision_nr=revision,
            meldung=meldung,
            lieferkennung=lieferkennung,
            zusammenfassung=inhalt["zusammenfassung"],
            dateien=dateien,
            pruefungen=pruefungen,
            inhalt_sha256=sha,
        )
        blocker_ids = (
            Lieferbedarf.objects.filter(
                lieferauftrag=aktuell,
                lieferrevision=revision,
                lieferkennung=lieferkennung,
                erfuellt_durch__isnull=True,
            )
            .values_list("meldung_id", flat=True)
            .distinct()
        )
        for blocker in Meldung.objects.filter(pk__in=blocker_ids):
            _freisetzen(akteur, blocker)
    return lieferung


def details(auftrag):
    """Nur lesen; keine Entblockung in GET oder Sitzungsstart."""
    lieferungen = [
        {
            "nummer": lieferung.pk,
            "revision": lieferung.revision_nr,
            "lieferkennung": lieferung.lieferkennung,
            "zusammenfassung": lieferung.zusammenfassung,
            "dateien": lieferung.dateien,
            "pruefungen": lieferung.pruefungen,
            "inhalt_sha256": lieferung.inhalt_sha256,
        }
        for lieferung in auftrag.lieferungen.filter(revision_nr=auftrag.revision_nr).order_by(
            "-pk"
        )[:100]
    ]
    letzter_blocker = _blocker(auftrag)
    blocker = (
        auftrag.meldungen.filter(
            revision_nr=auftrag.revision_nr,
            historisch=False,
            lieferbedarf__isnull=False,
        )
        .distinct()
        .first()
    )
    aktiv = (
        auftrag.zustand == Auftrag.BLOCKIERT
        and blocker is not None
        and letzter_blocker is not None
        and letzter_blocker.pk == blocker.pk
    )
    bedarf = (
        []
        if blocker is None
        else [
            {
                "auftrag": b.lieferauftrag.nummer,
                "revision": b.lieferrevision,
                "lieferkennung": b.lieferkennung,
                "nach_lieferung": b.nach_lieferung,
                "erfuellt_durch": b.erfuellt_durch_id,
                "aktiv": aktiv,
                "aktuelle_lieferrevision": b.lieferauftrag.revision_nr,
            }
            for b in blocker.lieferbedarf.filter(
                lieferauftrag__projekt_id=auftrag.projekt_id
            ).select_related("lieferauftrag")
        ]
    )
    angefordert = []
    for b in auftrag.lieferbedarfe.filter(
        lieferrevision=auftrag.revision_nr,
        erfuellt_durch__isnull=True,
        meldung__auftrag__projekt_id=auftrag.projekt_id,
        meldung__auftrag__zustand=Auftrag.BLOCKIERT,
        meldung__revision_nr=F("meldung__auftrag__revision_nr"),
    ).select_related("meldung__auftrag"):
        aktueller = _blocker(b.meldung.auftrag)
        if aktueller and aktueller.pk == b.meldung_id:
            angefordert.append(
                {
                    "verbraucher": b.meldung.auftrag.nummer,
                    "revision": b.meldung.revision_nr,
                    "lieferkennung": b.lieferkennung,
                    "nach_lieferung": b.nach_lieferung,
                    "beschreibung": b.meldung.text,
                }
            )
    return {
        "lieferungen": lieferungen,
        "lieferbedarf": bedarf,
        "angeforderte_lieferungen": angefordert,
    }

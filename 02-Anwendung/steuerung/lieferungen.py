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


VERWAIST = "verwaist"
NICHT_PASSEND = "nicht_passend"
ERFUELLT = "erfuellt"
WARTET = "wartet"
GELIEFERT = "geliefert"


def _passende_lieferung(bedarf):
    return (
        Lieferung.objects.filter(
            auftrag=bedarf.lieferauftrag,
            revision_nr=bedarf.lieferrevision,
            lieferkennung=bedarf.lieferkennung,
            pk__gt=bedarf.nach_lieferung,
        )
        .order_by("-pk")
        .first()
    )


def _bedarf_stand(bedarf, projekt_id):
    """Einzige Stelle, die über einen einzelnen Lieferbedarf entscheidet: (Status, Grund,
    passende Lieferung). Nur ``geliefert`` lässt freisetzen; jeder andere Status trägt seinen
    Grund, damit kein Bedarf still verworfen wird."""
    quelle = bedarf.lieferauftrag
    if quelle.projekt_id != projekt_id:
        return NICHT_PASSEND, "Der Lieferauftrag gehört nicht mehr zu diesem Projekt.", None
    if bedarf.erfuellt_durch_id:
        return ERFUELLT, "", None
    if quelle.zustand == Auftrag.ABGEBROCHEN:
        return NICHT_PASSEND, f"{quelle.nummer} ist abgebrochen und liefert nicht mehr.", None
    if quelle.ausfuehrungsweg != Auftrag.WEG_CHAT:
        return (
            NICHT_PASSEND,
            f"{quelle.nummer} läuft über eine Werkbank; Chat-Lieferungen passen nicht.",
            None,
        )
    if quelle.revision_nr != bedarf.lieferrevision:
        return (
            VERWAIST,
            f"{quelle.nummer} hat inzwischen Revision {quelle.revision_nr}, der Bedarf "
            f"{bedarf.lieferkennung} ist an Revision {bedarf.lieferrevision} gebunden. "
            "Neu binden kann nur der Manager (kipm_lieferbedarf_festlegen mit frischer "
            "hindernis_id); eine Lieferung der neuen Revision weckt sonst nicht.",
            None,
        )
    lieferung = _passende_lieferung(bedarf)
    if lieferung is None:
        return (
            WARTET,
            f"Noch keine passende Lieferung {bedarf.lieferkennung} von {quelle.nummer} "
            f"(Revision {bedarf.lieferrevision}).",
            None,
        )
    return GELIEFERT, "", lieferung


def _blocker_grund(auftrag, meldung):
    """Einzige Stelle, die über den Lieferblocker des Verbrauchers als Ganzes entscheidet:
    leer, wenn er freigesetzt werden kann, sonst der Grund."""
    if auftrag.zustand != Auftrag.BLOCKIERT:
        return f"{auftrag.nummer} ist {auftrag.get_zustand_display()}, nicht blockiert."
    if auftrag.revision_nr != meldung.revision_nr:
        return "Der Lieferblocker gehört zu einer früheren Revision des Verbrauchers."
    if not auftrag.hat_bearbeiter or auftrag.uebernommene_revision != auftrag.revision_nr:
        return "Der Verbraucher hat keine gültige Übernahme dieser Revision."
    if not auftrag.projekt.aktiv:
        return "Das Projekt ist archiviert."
    if auftrag.ausfuehrungsweg != Auftrag.WEG_CHAT or dienst._werkbank_aktiv(auftrag):
        return "Der Verbraucher läuft über eine Werkbank."
    letzter = _blocker(auftrag)
    if letzter is None or letzter.pk != meldung.pk:
        return (
            "Eine neuere Meldung (Rückfrage, Hindernis, Fehler oder Antwort) hat diesen "
            "Lieferbedarf abgelöst."
        )
    return ""


def _kette(bedarf, verbraucher):
    return {
        "verbraucher": verbraucher.nummer,
        "verbraucher_revision": verbraucher.revision_nr,
        "anbieter": bedarf.lieferauftrag.nummer,
        "lieferkennung": bedarf.lieferkennung,
    }


def betroffene_ketten(auftrag, *, als_anbieter=True):
    """Offene technische Lieferketten, die eine neue Revision oder eine Antwort dieses Auftrags
    trennen würde: als Anbieter die wartenden Verbraucher seiner aktuellen Revision, als
    Verbraucher der eigene aktive Lieferbedarf. Werkbank-Aufträge haben keine Chat-Lieferketten."""
    if auftrag.ausfuehrungsweg != Auftrag.WEG_CHAT:
        return []
    ketten = []
    if als_anbieter:
        for b in auftrag.lieferbedarfe.filter(
            lieferrevision=auftrag.revision_nr,
            erfuellt_durch__isnull=True,
            meldung__auftrag__projekt_id=auftrag.projekt_id,
            meldung__auftrag__zustand=Auftrag.BLOCKIERT,
            meldung__revision_nr=F("meldung__auftrag__revision_nr"),
        ).select_related("meldung__auftrag__projekt", "lieferauftrag"):
            if not _blocker_grund(b.meldung.auftrag, b.meldung):
                ketten.append(_kette(b, b.meldung.auftrag))
    if auftrag.zustand == Auftrag.BLOCKIERT:
        meldung = _blocker(auftrag)
        if meldung is not None and not _blocker_grund(auftrag, meldung):
            for b in meldung.lieferbedarf.filter(
                erfuellt_durch__isnull=True, lieferauftrag__projekt_id=auftrag.projekt_id
            ).select_related("lieferauftrag"):
                ketten.append(_kette(b, auftrag))
    return sorted(ketten, key=lambda k: (k["verbraucher"], k["anbieter"], k["lieferkennung"]))


def ketten_text(ketten):
    return "; ".join(
        f"{k['verbraucher']} wartet auf {k['anbieter']}/{k['lieferkennung']}" for k in ketten
    )


BESTAETIGEN = (
    "Nur nach Freds ausdrücklichem Ja erneut mit lieferketten_bestaetigt=true und neuer "
    "anfrage_kennung senden; in der Oberfläche das Häkchen setzen. Es wurde nichts geändert."
)


def warnung_bearbeiten(auftrag, ketten):
    return (
        f"Die Änderung legt eine neue Revision von {auftrag.nummer} an und trennt technische "
        f"Lieferketten: {ketten_text(ketten)}. Wartende Verbraucher bleiben danach blockiert, "
        "bis der Manager ihren verwaisten Bedarf neu bindet (kipm_lieferbedarf_festlegen); "
        f"ein eigener Lieferbedarf von {auftrag.nummer} entfällt. {BESTAETIGEN}"
    )


def warnung_antworten(ketten):
    liste = ", ".join(f"{k['anbieter']}/{k['lieferkennung']}" for k in ketten)
    return (
        f"Damit entfällt der technische Lieferbedarf auf {liste}: Die Antwort hebt die Blockade "
        f"auf, eine spätere Lieferung weckt {ketten[0]['verbraucher']} nicht mehr. {BESTAETIGEN}"
    )


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


def _neu_binden_plan(bestand, geprueft, projekt_id):
    """Welche verwaisten Bedarfe auf die aktuelle Anbieterrevision umgebunden werden; ``None``,
    wenn sich mehr ändern soll als genau das."""
    alt = {(b.lieferauftrag_id, b.lieferkennung): b for b in bestand}
    neu = {(a.pk, s): (a, r, n) for a, r, s, n in geprueft}
    if set(alt) != set(neu):
        return None
    plan = []
    for schluessel, b in alt.items():
        a, r, n = neu[schluessel]
        if (b.lieferrevision, b.nach_lieferung) == (r, n):
            continue
        if _bedarf_stand(b, projekt_id)[0] != VERWAIST or r != a.revision_nr:
            return None
        plan.append((b, r, n))
    return plan or None


def _registrieren(akteur, auftrag, meldung, geprueft, *, neu_binden=False):
    bestand = list(meldung.lieferbedarf.select_related("lieferauftrag"))
    vorhanden = {
        (b.lieferauftrag_id, b.lieferrevision, b.lieferkennung, b.nach_lieferung) for b in bestand
    }
    erwartet = {(a.pk, r, s, n) for a, r, s, n in geprueft}
    if vorhanden:
        if vorhanden != erwartet:
            plan = _neu_binden_plan(bestand, geprueft, auftrag.projekt_id) if neu_binden else None
            if plan is None:
                raise dienst.Konflikt(
                    "Dieser Blocker hat bereits einen anderen Lieferbedarf. Neu binden lassen "
                    "sich nur verwaiste Bedarfe, je auf die aktuelle Revision desselben "
                    "Anbieters und Lieferschlüssels."
                )
            umgebunden = []
            for b, r, n in plan:
                umgebunden.append(
                    f"{b.lieferauftrag.nummer}/{b.lieferkennung} Revision {b.lieferrevision} → {r}"
                )
                b.lieferrevision, b.nach_lieferung = r, n
                b.save(update_fields=["lieferrevision", "nach_lieferung"])
            dienst._ereignis(
                akteur,
                projekt=auftrag.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=auftrag.nummer,
                ereignis="Verwaisten Lieferbedarf neu gebunden",
                anlass="; ".join(umgebunden),
                revision=str(auftrag.revision_nr),
            )
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
    """Einordnung vorhandener Blockaden durch Fred/Manager; kein neuer Lebensimpuls. Hat der
    Blocker schon einen Bedarf, darf nur ein verwaister auf die aktuelle Anbieterrevision neu
    gebunden werden."""
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
        _registrieren(akteur, aktuell, meldung, geprueft, neu_binden=True)
    auftrag.refresh_from_db()
    return meldung


def _freisetzen(akteur, meldung):
    # Jeder Abbruchgrund steht in _blocker_grund oder _bedarf_stand und ist in details sichtbar.
    auftrag = Auftrag.objects.select_related("projekt").get(pk=meldung.auftrag_id)
    if _blocker_grund(auftrag, meldung):
        return
    passend = []
    for bedarf in meldung.lieferbedarf.select_related("lieferauftrag"):
        status, _, lieferung = _bedarf_stand(bedarf, auftrag.projekt_id)
        if status != GELIEFERT:
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
    blocker = (
        auftrag.meldungen.filter(
            revision_nr=auftrag.revision_nr,
            historisch=False,
            lieferbedarf__isnull=False,
        )
        .distinct()
        .first()
    )
    inaktiv_grund = "" if blocker is None else _blocker_grund(auftrag, blocker)
    bedarf, ausgeblendet = [], []
    if blocker is not None:
        for b in blocker.lieferbedarf.select_related("lieferauftrag").order_by("pk"):
            status, grund, _ = _bedarf_stand(b, auftrag.projekt_id)
            if b.lieferauftrag.projekt_id != auftrag.projekt_id:
                # Nichts über fremde Projekte verraten, den Bedarf aber nicht verschweigen.
                ausgeblendet.append(
                    {"lieferkennung": b.lieferkennung, "status": status, "grund": grund}
                )
                continue
            bedarf.append(
                {
                    "auftrag": b.lieferauftrag.nummer,
                    "revision": b.lieferrevision,
                    "lieferkennung": b.lieferkennung,
                    "nach_lieferung": b.nach_lieferung,
                    "erfuellt_durch": b.erfuellt_durch_id,
                    "aktiv": not inaktiv_grund,
                    "inaktiv_grund": inaktiv_grund,
                    "aktuelle_lieferrevision": b.lieferauftrag.revision_nr,
                    "status": status,
                    "verwaist": status == VERWAIST,
                    "grund": grund,
                }
            )
    angefordert, verwaist = [], []
    for b in auftrag.lieferbedarfe.filter(
        erfuellt_durch__isnull=True,
        meldung__auftrag__projekt_id=auftrag.projekt_id,
        meldung__auftrag__zustand=Auftrag.BLOCKIERT,
        meldung__revision_nr=F("meldung__auftrag__revision_nr"),
    ).select_related("meldung__auftrag__projekt", "lieferauftrag"):
        verbraucher = b.meldung.auftrag
        if _blocker_grund(verbraucher, b.meldung):
            continue  # Der Verbraucher wartet nicht mehr darauf; sein details nennt den Grund.
        eintrag = {
            "verbraucher": verbraucher.nummer,
            "revision": b.meldung.revision_nr,
            "lieferkennung": b.lieferkennung,
            "nach_lieferung": b.nach_lieferung,
            "beschreibung": b.meldung.text,
        }
        if b.lieferrevision == auftrag.revision_nr:
            angefordert.append(eintrag)
            continue
        status, grund, _ = _bedarf_stand(b, verbraucher.projekt_id)
        verwaist.append(
            eintrag
            | {
                "gebunden_an_revision": b.lieferrevision,
                "aktuelle_revision": auftrag.revision_nr,
                "status": status,
                "grund": grund,
            }
        )
    return {
        "lieferungen": lieferungen,
        "lieferbedarf": bedarf,
        "lieferbedarf_ausgeblendet": ausgeblendet,
        "angeforderte_lieferungen": angefordert,
        "verwaiste_anforderungen": verwaist,
    }


def offener_bedarf(auftrag):
    """Aktiver, noch nicht erfüllter Lieferbedarf eines blockierten Chat-Auftrags (Oberfläche
    und „Wartet auf dich“)."""
    if auftrag.zustand != Auftrag.BLOCKIERT or auftrag.ausfuehrungsweg != Auftrag.WEG_CHAT:
        return []
    return [b for b in details(auftrag)["lieferbedarf"] if b["aktiv"] and b["status"] != ERFUELLT]

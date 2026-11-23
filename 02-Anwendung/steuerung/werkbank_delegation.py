"""Drei Wege C01/KP01: echte projektgebundene W1-Chatrechte, ohne Owner-Imitation.

Der Dienst liest Rechte aus gespeicherten Fassungen. Chattext und mitgelieferte Rollen
erteilen nichts. Neue delegierte Auftraege bleiben vor dem v2-Transport fuer v1 gesperrt.
"""

import re
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import dienst, werkbank, werkbank_vertrag
from .models import (
    AUSFUEHRUNG_AKTIV,
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Projekt,
    WerkbankAusfuehrung,
    WerkbankChatDelegation,
    WerkbankChatDelegationsfassung,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)

VERTRAG = "kipm-chat-delegation/1"
AKTIONEN = (
    "auftrag_anlegen",
    "auftrag_aendern",
    "freigeben",
    "zuweisen",
    "starten",
    "fortsetzen",
    "integrieren",
)
BETRAEGE = ("geld_mikro", "zeit_ms", "aufrufe")
MAX_GANZ = 2**53 - 1
WAEHRUNGEN = ("USD", "EUR")


def _owner(akteur):
    if (
        not akteur.ist_owner
        or akteur.bindung is not None
        or akteur.benutzer is None
        or not get_user_model()
        .objects.filter(pk=akteur.benutzer.pk, is_active=True, is_superuser=True)
        .exists()
    ):
        raise dienst.KeinZugriff(
            "W1-Erlaubnisse darf nur der angemeldete Owner erteilen oder widerrufen."
        )


def budget_pruefen(roh):
    if not isinstance(roh, dict) or set(roh) != {"waehrung", *BETRAEGE}:
        raise dienst.DienstFehler("Budget braucht Währung, Geldobergrenze, Zeit und KI-Aufrufe.")
    if (
        not isinstance(roh["waehrung"], str)
        or not re.fullmatch(r"[A-Z]{3}", roh["waehrung"])
        or roh["waehrung"] not in WAEHRUNGEN
    ):
        raise dienst.DienstFehler(
            "Als Budgetwährung sind USD und EUR unterstützt; "
            "es erfolgt keine automatische Umrechnung."
        )
    for name in BETRAEGE:
        minimum = 0 if name == "geld_mikro" else 1
        if type(roh[name]) is not int or not minimum <= roh[name] <= MAX_GANZ:
            raise dienst.DienstFehler(f"Ungültige Budgetgrenze: {name}.")
    return dict(roh)


def _bindung(akteur, projekt):
    if akteur.bindung is None:
        raise dienst.KeinZugriff("Diese W1-Aktion braucht eine authentifizierte Chatbindung.")
    bindung = ChatBindung.objects.filter(pk=akteur.bindung.pk).first()
    if (
        bindung is None
        or bindung.art not in (ChatBindung.ART_PC, ChatBindung.ART_PROJEKT)
        or not bindung.aktiv
    ):
        raise dienst.KeinZugriff("Chat-Zugang ungültig, abgelaufen oder widerrufen.")
    if akteur.projekt is None or akteur.projekt.pk != projekt.pk:
        raise dienst.KeinZugriff("Der authentifizierte Projektkontext passt nicht zur W1-Aktion.")
    if bindung.art == ChatBindung.ART_PROJEKT and bindung.projekt_id != projekt.pk:
        raise dienst.KeinZugriff("Nicht gefunden.")
    return bindung


def _ort_fassung(ort):
    return werkbank_vertrag.sha256_hex(
        {
            "id": ort.pk,
            "projekt_id": ort.projekt_id,
            "pfad": ort.pfad,
            "repository": ort.repository,
            "rechner": ort.rechner,
            "modul_id": ort.modul_id,
        }
    )


def _zielstand(pb):
    return {
        "id": pb.pk,
        "version": pb.version,
        "instanz_id": pb.instanz_id,
        "zugangsbindung_id": pb.instanz.bindung_id,
        "werkbank_id": str(pb.instanz.werkbank_id),
        "arbeitsort_id": pb.arbeitsort_id,
        "arbeitsort_sha256": _ort_fassung(pb.arbeitsort) if pb.arbeitsort else None,
        "repo_fingerabdruck": pb.repo_fingerabdruck,
    }


def erteilen(akteur, projekt, bindung, *, aktionen, ziele, budget, gueltig_bis, standard_ziel=None):
    """Nur Owner; keine automatische Umstellung alter Zugänge oder Aufträge."""
    _owner(akteur)
    budget = budget_pruefen(budget)
    if (
        not isinstance(aktionen, (list, tuple))
        or not aktionen
        or any(not isinstance(a, str) for a in aktionen)
        or set(aktionen) - set(AKTIONEN)
    ):
        raise dienst.DienstFehler(
            "Mindestens eine bekannte W1-Aktion muss ausdrücklich erlaubt sein."
        )
    if not isinstance(ziele, (list, tuple)) or not 1 <= len(ziele) <= 100:
        raise dienst.DienstFehler("Eine Erlaubnis braucht ein bis hundert konkrete Werkbankziele.")
    jetzt = timezone.now()
    if (
        not isinstance(gueltig_bis, datetime)
        or timezone.is_naive(gueltig_bis)
        or gueltig_bis <= jetzt
    ):
        raise dienst.DienstFehler("Die Erlaubnis braucht ein zukünftiges Ablaufdatum mit Zeitzone.")
    with transaction.atomic():
        _owner(akteur)
        jetzt = timezone.now()
        if gueltig_bis <= jetzt:
            raise dienst.DienstFehler("Die Erlaubnis ist vor dem Speichern abgelaufen.")
        projekt = Projekt.objects.get(pk=projekt.pk)
        b = ChatBindung.objects.get(pk=bindung.pk)
        if (
            not projekt.aktiv
            or not b.aktiv
            or b.art not in (ChatBindung.ART_PC, ChatBindung.ART_PROJEKT)
        ):
            raise dienst.DienstFehler("Projekt oder Chat-Zugang ist nicht aktiv.")
        if b.art == ChatBindung.ART_PROJEKT and b.projekt_id != projekt.pk:
            raise dienst.DienstFehler("Der Chat-Zugang gehört zu einem anderen Projekt.")
        if gueltig_bis > b.gueltig_bis:
            raise dienst.DienstFehler("Die Erlaubnis darf nicht länger als der Chat-Zugang gelten.")
        if WerkbankChatDelegation.objects.filter(
            bindung=b, projekt=projekt, widerrufen_am=None
        ).exists():
            raise dienst.Konflikt(
                "Zuerst die bisherige W1-Erlaubnis widerrufen; sie wird nicht still ersetzt."
            )
        snapshots = []
        for ziel in ziele:
            pb = WerkbankProjektbindung.objects.select_related(
                "instanz__bindung", "arbeitsort"
            ).get(pk=ziel.pk)
            if pb.projekt_id != projekt.pk or not werkbank.projekt_erlaubt(pb.instanz, projekt):
                raise dienst.DienstFehler(
                    "Ein Werkbankziel ist für dieses Projekt nicht zugelassen."
                )
            if (
                pb.instanz.bindung is None
                or not pb.instanz.bindung.aktiv
                or pb.instanz.bindung.art != ChatBindung.ART_WERKBANK
            ):
                raise dienst.DienstFehler("Ein Werkbankziel braucht einen aktiven Werkbank-Zugang.")
            if (
                pb.arbeitsort is None
                or pb.arbeitsort.projekt_id != projekt.pk
                or not pb.arbeitsort.aktiv
                or not pb.arbeitsort.bestaetigt
            ):
                raise dienst.DienstFehler(
                    "Ein Werkbankziel braucht einen aktiven, bestätigten Arbeitsort."
                )
            snapshots.append(_zielstand(pb))
        ids = {z["id"] for z in snapshots}
        if len(ids) != len(snapshots) or (
            standard_ziel is not None and standard_ziel.pk not in ids
        ):
            raise dienst.DienstFehler(
                "Werkbankziele müssen eindeutig sein; das Standardziel muss dazugehören."
            )
        d = WerkbankChatDelegation.objects.create(
            bindung=b,
            projekt=projekt,
            aussteller=akteur.benutzer,
            reserviert=dict.fromkeys(BETRAEGE, 0),
        )
        WerkbankChatDelegationsfassung.objects.create(
            delegation=d,
            revision=1,
            aktionen=sorted(set(aktionen)),
            ziele=snapshots,
            standard_ziel=standard_ziel.pk if standard_ziel is not None else None,
            budget=budget,
            gueltig_ab=jetzt,
            gueltig_bis=gueltig_bis,
        )
        dienst._ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="W1-Erlaubnis",
            objekt_kennung=f"W1-{d.pk}",
            ereignis="W1-Erlaubnis erteilt",
            revision="1",
            anlass=(
                f"Chat {b.name}; Aktionen: {', '.join(sorted(set(aktionen)))}; "
                f"gültig bis {gueltig_bis:%d.%m.%Y %H:%M}."
            ),
        )
    return d


def widerrufen(akteur, delegation, *, erwartet_revision):
    _owner(akteur)
    with transaction.atomic():
        _owner(akteur)
        d = WerkbankChatDelegation.objects.select_for_update().get(pk=delegation.pk)
        if d.widerrufen_am is not None:
            return d
        if type(erwartet_revision) is not int or d.revision != erwartet_revision:
            raise dienst.Konflikt("Die W1-Erlaubnis wurde inzwischen geändert. Ansicht neu laden.")
        stand = WerkbankChatDelegationsfassung.objects.get(delegation=d, revision=d.revision)
        d.revision += 1
        d.widerrufen_am = timezone.now()
        d.save(update_fields=["revision", "widerrufen_am"])
        WerkbankChatDelegationsfassung.objects.create(
            delegation=d,
            revision=d.revision,
            aktionen=stand.aktionen,
            ziele=stand.ziele,
            standard_ziel=stand.standard_ziel,
            budget=stand.budget,
            gueltig_ab=stand.gueltig_ab,
            gueltig_bis=stand.gueltig_bis,
            widerrufen=True,
        )
        dienst._ereignis(
            akteur,
            projekt=d.projekt,
            objekt_typ="W1-Erlaubnis",
            objekt_kennung=f"W1-{d.pk}",
            ereignis="W1-Erlaubnis widerrufen",
            revision=str(d.revision),
            anlass="Keine neuen berechtigungspflichtigen Schritte; "
            "laufende Wirkung erst nach lokaler Quittung beendet.",
        )
        freigaben_pruefen(akteur, d.projekt, delegation_id=d.pk)
    return d


def _stand_pruefen(d, revision):
    if d.widerrufen_am is not None:
        raise dienst.KeinZugriff("Die W1-Erlaubnis ist widerrufen.")
    if d.revision != revision:
        raise dienst.Konflikt("Die W1-Erlaubnis hat eine andere Fassung. Berechtigungen neu lesen.")
    stand = WerkbankChatDelegationsfassung.objects.filter(delegation=d, revision=d.revision).first()
    jetzt = timezone.now()
    if stand is None:
        raise dienst.Konflikt("Die gespeicherte W1-Erlaubnis ist unvollständig.")
    if (
        stand.widerrufen
        or not stand.gueltig_ab <= jetzt < stand.gueltig_bis
        or not d.bindung.aktiv
        or d.bindung.art not in (ChatBindung.ART_PC, ChatBindung.ART_PROJEKT)
        or d.bindung.art == ChatBindung.ART_PROJEKT
        and d.bindung.projekt_id != d.projekt_id
    ):
        raise dienst.KeinZugriff(
            "Die W1-Erlaubnis oder der Chat-Zugang ist abgelaufen oder noch nicht gültig."
        )
    if not d.projekt.aktiv or not d.aussteller.is_active or not d.aussteller.is_superuser:
        raise dienst.KeinZugriff("Projekt oder Rechteaussteller ist nicht mehr aktiv berechtigt.")
    return stand


def pruefen(
    akteur, projekt, *, delegation_id, revision, aktion, arbeitsort=None, owner_erlaubt=False
):
    """Aktueller DB-Stand, niemals die behauptete Rolle oder alte Objektkopie."""
    if (
        type(delegation_id) is not int
        or type(revision) is not int
        or delegation_id < 1
        or revision < 1
    ):
        raise dienst.KeinZugriff("Die versionsgebundene W1-Erlaubnis fehlt.")
    mensch = owner_erlaubt and akteur.bindung is None
    if mensch:
        _owner(akteur)
        bindung = None
    else:
        bindung = _bindung(akteur, projekt)
    d = (
        WerkbankChatDelegation.objects.select_related("projekt", "bindung", "aussteller")
        .filter(pk=delegation_id, projekt=projekt)
        .first()
    )
    if d is None or (bindung is not None and d.bindung_id != bindung.pk):
        raise dienst.KeinZugriff("Diese W1-Erlaubnis gehört nicht zu Chat und Projekt.")
    stand = _stand_pruefen(d, revision)
    if aktion not in AKTIONEN or (not mensch and aktion not in stand.aktionen):
        raise dienst.KeinZugriff(f"Die W1-Erlaubnis enthält die Aktion „{aktion}“ nicht.")
    if arbeitsort is not None:
        erlaubt = [z for z in stand.ziele if z["arbeitsort_id"] == arbeitsort.pk]
        if not erlaubt:
            raise dienst.KeinZugriff("Der Arbeitsort liegt außerhalb der W1-Erlaubnis.")
        ort = Arbeitsort.objects.filter(
            pk=arbeitsort.pk, projekt=projekt, aktiv=True, bestaetigt=True
        ).first()
        if ort is None or not any(_ort_fassung(ort) == z["arbeitsort_sha256"] for z in erlaubt):
            raise dienst.Konflikt("Der Arbeitsort hat sich seit der W1-Erlaubnis geändert.")
    return d, stand


def freigaben_pruefen(akteur, projekt, *, delegation_id=None):
    """Veraltete C01-Freigaben entwerten; eine laufende Wirkung nur zum Halt auffordern."""
    with transaction.atomic():
        auftraege = Auftrag.objects.select_for_update().filter(
            projekt=projekt, chat_delegation__isnull=False
        )
        quellenhash = None
        if delegation_id is not None:
            auftraege = auftraege.filter(chat_delegation_id=delegation_id)
        for a in auftraege.select_related("projekt"):
            aktive = list(a.ausfuehrungen.filter(zustand__in=AUSFUEHRUNG_AKTIV))
            if a.zustand != Auftrag.FREIGEGEBEN and not aktive:
                continue
            d = WerkbankChatDelegation.objects.select_related(
                "projekt", "bindung", "aussteller"
            ).get(pk=a.chat_delegation_id)
            try:
                stand = _stand_pruefen(d, a.chat_delegation_revision)
                if "freigeben" not in stand.aktionen:
                    raise dienst.KeinZugriff("Das Freigaberecht fehlt.")
                z = WerkbankZuweisung.objects.filter(auftrag=a).first()
                if z is None:
                    raise dienst.Konflikt("Das zugewiesene Ziel fehlt.")
                ziel = ziel_pruefen(stand, z.projektbindung_id, bereit=True)
                if ziel.arbeitsort_id != a.aktuelle_revision.arbeitsort_id:
                    raise dienst.Konflikt("Der geplante Arbeitsort passt nicht zum Werkbankziel.")
                if a.zustand == Auftrag.FREIGEGEBEN:
                    if quellenhash is None:
                        quellenhash = werkbank.delegierter_quellenhash(projekt.pk)
                    if z.kontext_sha256 != quellenhash:
                        raise dienst.Konflikt(
                            "Die gebundenen Projektquellen haben sich geändert. "
                            "Den aktuellen Stand neu freigeben."
                        )
            except dienst.DienstFehler as fehler:
                grund = str(fehler)
                if aktive:
                    for ausf in aktive:
                        werkbank._teilmodule()[1].halt_setzen(
                            akteur, ausf, "kontext_veraltet", grund
                        )
                else:
                    a.zustand = Auftrag.ENTWURF
                    a.freigegeben_am = None
                    a.save(update_fields=["zustand", "freigegeben_am", "aktualisiert_am"])
                    werkbank.freigabe_aufgehoben(a)
                    dienst._ereignis(
                        akteur,
                        projekt=a.projekt,
                        objekt_typ="Auftrag",
                        objekt_kennung=a.nummer,
                        ereignis="W1-Freigabe erloschen",
                        revision=str(a.revision_nr),
                        anlass=grund,
                    )


def zugang_geaendert(akteur, bindung):
    ids = set(
        WerkbankChatDelegation.objects.filter(bindung=bindung).values_list("projekt_id", flat=True)
    )
    ids.update(
        WerkbankProjektbindung.objects.filter(instanz__bindung=bindung).values_list(
            "projekt_id", flat=True
        )
    )
    for projekt in Projekt.objects.filter(pk__in=ids):
        freigaben_pruefen(akteur, projekt)


def ziel_pruefen(stand, ziel_id=None, *, bereit=False):
    if ziel_id is None:
        ziel_id = stand.standard_ziel
        if ziel_id is None and len(stand.ziele) == 1:
            ziel_id = stand.ziele[0]["id"]
    if type(ziel_id) is not int:
        raise dienst.Konflikt(
            "Mehrere Werkbankziele sind möglich. Bitte ein erlaubtes Ziel auswählen."
        )
    erwartet = next((z for z in stand.ziele if z["id"] == ziel_id), None)
    if erwartet is None:
        raise dienst.KeinZugriff("Dieses Werkbankziel ist nicht delegiert.")
    pb = (
        WerkbankProjektbindung.objects.select_related("instanz__bindung", "arbeitsort", "projekt")
        .filter(pk=ziel_id)
        .first()
    )
    if (
        pb is None
        or pb.projekt_id != stand.delegation.projekt_id
        or not werkbank.projekt_erlaubt(pb.instanz, pb.projekt)
    ):
        raise dienst.KeinZugriff("Das Werkbankziel ist nicht mehr zugelassen.")
    jetzt = _zielstand(pb)
    if jetzt != erwartet:
        raise dienst.Konflikt(
            "Die Zielbindung hat sich seit der W1-Erlaubnis geändert. Erlaubnis neu bestätigen."
        )
    if pb.arbeitsort is None or not pb.arbeitsort.aktiv or not pb.arbeitsort.bestaetigt:
        raise dienst.KeinZugriff("Der Zielarbeitsort ist nicht mehr bestätigt und aktiv.")
    if (
        pb.instanz.bindung is None
        or not pb.instanz.bindung.aktiv
        or pb.instanz.bindung.art != ChatBindung.ART_WERKBANK
    ):
        raise dienst.KeinZugriff("Der Zugang der Zielwerkbank ist nicht mehr aktiv.")
    if bereit and (not pb.bereit or not pb.repo_fingerabdruck):
        raise dienst.Konflikt(
            "Die Werkbank hat noch keine belastbare Bereitschaft für dieses Ziel gemeldet."
        )
    return pb


def auftrag_pruefen(akteur, auftrag, aktion, *, revision=None, arbeitsort=None):
    if auftrag.chat_delegation_id is None:
        return None
    if revision is None and akteur.bindung is None:
        revision = auftrag.chat_delegation_revision
    if type(revision) is not int or revision != auftrag.chat_delegation_revision:
        raise dienst.Konflikt("Die Auftragsbindung passt nicht zur angeforderten Erlaubnisfassung.")
    return pruefen(
        akteur,
        auftrag.projekt,
        delegation_id=auftrag.chat_delegation_id,
        revision=revision,
        aktion=aktion,
        arbeitsort=arbeitsort,
        owner_erlaubt=True,
    )


def _reserviert_pruefen(roh):
    if (
        not isinstance(roh, dict)
        or set(roh) != set(BETRAEGE)
        or any(type(roh[name]) is not int or not 0 <= roh[name] <= MAX_GANZ for name in BETRAEGE)
    ):
        raise dienst.Konflikt(
            "Der gespeicherte Budgetstand ist widersprüchlich; Abgleich erforderlich."
        )
    return dict(roh)


def budget_reservieren(akteur, auftrag, *, revision):
    """Recht und Summen unter derselben Schreibsperre frisch lesen, nie DTO-Summen übernehmen."""
    with transaction.atomic():
        a = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if a.chat_delegation_id is None:
            raise dienst.KeinZugriff("Dieser Auftrag hat keine W1-Erlaubnis.")
        d, stand = auftrag_pruefen(akteur, a, "freigeben", revision=revision)
        budget = budget_pruefen(a.chat_budget)
        if budget["waehrung"] != stand.budget["waehrung"]:
            raise dienst.KeinZugriff(
                "Auftragsbudget und Erlaubnis haben verschiedene Währungen; "
                "kein automatischer Wechselkurs."
            )
        reserviert = _reserviert_pruefen(d.reserviert)
        if not a.chat_budget_reserviert:
            for name in BETRAEGE:
                if (
                    budget[name] > stand.budget[name]
                    or reserviert[name] + budget[name] > stand.budget[name]
                ):
                    raise dienst.KeinZugriff(
                        "Das Gesamtbudget der W1-Erlaubnis reicht für diesen Auftrag nicht aus."
                    )
                reserviert[name] += budget[name]
            d.reserviert = reserviert
            d.save(update_fields=["reserviert"])
            a.chat_budget_reserviert = True
            a.save(update_fields=["chat_budget_reserviert"])
        auftrag.chat_budget_reserviert = a.chat_budget_reserviert


def budget_vor_start_freigeben(akteur, auftrag):
    """Nur wenn es noch nie eine Ausführung gab; Status allein beweist kein Ende."""
    with transaction.atomic():
        _owner(akteur)
        a = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if a.chat_budget_reserviert:
            if WerkbankAusfuehrung.objects.filter(auftrag=a).exists():
                raise dienst.Konflikt(
                    "Budget nach einer Ausführung erst mit ihrem "
                    "belastbaren Endnachweis abgleichen."
                )
            d = WerkbankChatDelegation.objects.select_for_update().get(pk=a.chat_delegation_id)
            reserviert = _reserviert_pruefen(d.reserviert)
            budget = budget_pruefen(a.chat_budget)
            for name in BETRAEGE:
                reserviert[name] -= budget[name]
                if reserviert[name] < 0:
                    raise dienst.Konflikt(
                        "Der gespeicherte Budgetstand ist widersprüchlich; Abgleich erforderlich."
                    )
            d.reserviert = reserviert
            d.save(update_fields=["reserviert"])
            a.chat_budget_reserviert = False
            a.save(update_fields=["chat_budget_reserviert"])
        auftrag.chat_budget_reserviert = a.chat_budget_reserviert


def sichten(akteur, projekt):
    bindung = _bindung(akteur, projekt)
    result = []
    for d in WerkbankChatDelegation.objects.filter(bindung=bindung, projekt=projekt).order_by("id"):
        stand = d.fassungen.get(revision=d.revision)
        result.append(
            {
                "vertrag": VERTRAG,
                "delegation_id": d.pk,
                "revision": d.revision,
                "projekt": projekt.kennung,
                "aktionen": stand.aktionen,
                "ziele": stand.ziele,
                "standard_ziel": stand.standard_ziel,
                "budget": stand.budget,
                "reserviert": d.reserviert,
                "gueltig_ab": stand.gueltig_ab.isoformat(),
                "gueltig_bis": stand.gueltig_bis.isoformat(),
                "widerrufen": d.widerrufen_am is not None,
            }
        )
    return result


def auftrag_anlegen(
    akteur,
    projekt,
    *,
    delegation_id,
    revision,
    budget,
    ziel_id=None,
    zuweisen=True,
    freigeben=False,
    planung=None,
    **daten,
):
    """W1-Fachauftrag atomar anlegen, optional zuweisen/freigeben; kein v1-Laufstart."""
    if type(zuweisen) is not bool or type(freigeben) is not bool:
        raise dienst.DienstFehler(
            "Zuweisen und Freigeben müssen ausdrücklich wahr oder falsch sein."
        )
    if freigeben and not zuweisen:
        raise dienst.DienstFehler("Eine W1-Freigabe braucht ein zugewiesenes Ziel.")
    erlaubt = {
        "titel",
        "beschreibung",
        "modul",
        "prioritaet",
        "erzwingen",
        "typ",
        "meilenstein",
        "faellig",
    }
    if set(daten) - erlaubt:
        raise dienst.DienstFehler("Unbekannte Felder im W1-Auftrag.")
    with transaction.atomic():
        _, stand = pruefen(
            akteur,
            projekt,
            delegation_id=delegation_id,
            revision=revision,
            aktion="auftrag_anlegen",
        )
        ziel = ziel_pruefen(stand, ziel_id, bereit=freigeben)
        budget = budget_pruefen(budget)
        if budget["waehrung"] != stand.budget["waehrung"] or any(
            budget[k] > stand.budget[k] for k in BETRAEGE
        ):
            raise dienst.KeinZugriff("Das Auftragsbudget liegt außerhalb der W1-Erlaubnis.")
        planung = dict(planung or {})
        if planung.get("arbeitsort") is not None and planung["arbeitsort"].pk != ziel.arbeitsort_id:
            raise dienst.KeinZugriff("Der geplante Arbeitsort passt nicht zum Werkbankziel.")
        planung["arbeitsort"] = ziel.arbeitsort
        a = dienst.wunsch_erfassen(
            akteur,
            projekt,
            planung=planung,
            w1_delegation_id=delegation_id,
            w1_revision=revision,
            w1_budget=budget,
            **daten,
        )
        if zuweisen:
            werkbank.zuweisen(akteur, a, ziel, freigeben=freigeben, w1_revision=revision)
        a.refresh_from_db()
        return a


@dienst._ohne_aktive_werkbank("W1-Auftrag ändern", w1_delegiert=True)
def auftrag_aktion(
    akteur, auftrag, *, delegation_id, revision, auftrag_revision, aktion, ziel_id=None, werte=None
):
    """Neue W1-Aktion bleibt am ursprünglichen Recht und am erwarteten Fachstand gebunden."""
    if aktion not in ("zuweisen", "freigeben", "auftrag_aendern"):
        raise dienst.DienstFehler("Diese W1-Auftragsaktion ist nicht verfügbar.")
    with transaction.atomic():
        a = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if type(delegation_id) is not int or a.chat_delegation_id != delegation_id:
            raise dienst.KeinZugriff("Der Auftrag gehört nicht zu dieser W1-Erlaubnis.")
        if type(auftrag_revision) is not int or a.revision_nr != auftrag_revision:
            raise dienst.Konflikt("Der Fachauftrag hat inzwischen eine andere Revision.")
        _, stand = auftrag_pruefen(akteur, a, aktion, revision=revision)
        if aktion == "zuweisen":
            werkbank.zuweisen(akteur, a, ziel_pruefen(stand, ziel_id), w1_revision=revision)
        elif aktion == "freigeben":
            dienst.auftrag_freigeben(akteur, a, w1_revision=revision)
        else:
            rev = a.aktuelle_revision
            daten = {
                "titel": a.titel,
                "prioritaet": a.prioritaet,
                "modul": a.modul,
                **{
                    k: getattr(rev, k)
                    for k in (
                        "beschreibung",
                        "ziel",
                        "nicht_ziele",
                        "arbeitsort",
                        "schreibumfang",
                        "quellen",
                        "abnahmekriterien",
                    )
                },
            }
            daten.update(werte or {})
            dienst.auftrag_bearbeiten(akteur, a, w1_revision=revision, **daten)
        a.refresh_from_db()
        return a

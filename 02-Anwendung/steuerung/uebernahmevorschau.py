"""Lesende Übernahmeprüfung für eine andere Chat-Bindung.

Gültige PC-Zugänge (Freds Codex und Claude Desktop) prüfen in jedem Projekt für jeden Zugang; das
ist rein lesend und gibt kein Recht (Freds Vorgabe vom 23.11.2026: beide sehen alles). Andere
Zugänge brauchen weiter ein projektgebundenes ``UebernahmePruefrecht``."""

from django.utils import timezone

from . import dienst
from .models import Auftrag, ChatBindung, UebernahmePruefrecht

KEIN_RECHT = "Für diese Zielbindung und dieses Projekt fehlt das Prüfrecht."


def _zielbindung(akteur, projekt, zielbindung_id):
    if akteur.bindung.art == ChatBindung.ART_PC:
        ziel = (
            ChatBindung.objects.filter(pk=zielbindung_id)
            .exclude(art=ChatBindung.ART_WERKBANK)
            .first()
        )
        if ziel is None:
            raise dienst.KeinZugriff("Nicht gefunden.")
        return ziel
    recht = (
        UebernahmePruefrecht.objects.filter(
            projekt=projekt, pruefbindung=akteur.bindung, zielbindung_id=zielbindung_id
        )
        .select_related("zielbindung")
        .first()
    )
    if recht is None:
        raise dienst.KeinZugriff(KEIN_RECHT)
    return recht.zielbindung


def pruefen(akteur, projekt, auftrag_id, revision_nr, zielbindung_id, unbeaufsichtigt):
    pruefzeit = timezone.now()
    if akteur.bindung is None or not dienst._chatbindung_im_projekt_gilt(akteur.bindung, projekt):
        raise dienst.KeinZugriff(KEIN_RECHT)
    ziel = _zielbindung(akteur, projekt, zielbindung_id)
    auftrag = (
        Auftrag.objects.select_related("projekt").filter(pk=auftrag_id, projekt=projekt).first()
    )
    if auftrag is None:
        raise dienst.KeinZugriff("Nicht gefunden.")

    if not dienst._chatbindung_im_projekt_gilt(ziel, projekt):
        gruende = [
            (
                "ZIELBINDUNG_UNGUELTIG",
                "Der Zielzugang ist für dieses Projekt nicht gültig oder abgelaufen.",
            )
        ]
    else:
        gruende = dienst._uebernahme_hindernisse(
            dienst.Akteur.aus_bindung(ziel, projekt),
            auftrag,
            revision_nr,
            unbeaufsichtigt=unbeaufsichtigt,
            pruefzeit=pruefzeit,
            nur_projekt=True,
        )
    return {
        "ok": True,
        "auftrag": auftrag.nummer,
        "projekt": projekt.kennung,
        "zielbindung_id": zielbindung_id,
        "revision_angefragt": revision_nr,
        "revision_geprueft": auftrag.revision_nr,
        "pruefzeitpunkt": pruefzeit.isoformat(),
        "uebernehmbar": not gruende,
        "sperrgruende": [{"code": code, "grund": grund} for code, grund in gruende],
    }

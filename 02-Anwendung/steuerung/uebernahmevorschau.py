"""Projektgebundene Leseerlaubnisse für die Übernahmeprüfung einer anderen Chat-Bindung."""

from django.utils import timezone

from . import dienst
from .models import Auftrag, UebernahmePruefrecht


def pruefen(akteur, projekt, auftrag_id, revision_nr, zielbindung_id, unbeaufsichtigt):
    pruefzeit = timezone.now()
    if akteur.bindung is None or not dienst._chatbindung_im_projekt_gilt(akteur.bindung, projekt):
        raise dienst.KeinZugriff("Für diese Zielbindung und dieses Projekt fehlt das Prüfrecht.")
    recht = (
        UebernahmePruefrecht.objects.filter(
            projekt=projekt, pruefbindung=akteur.bindung, zielbindung_id=zielbindung_id
        )
        .select_related("zielbindung")
        .first()
    )
    if recht is None:
        raise dienst.KeinZugriff("Für diese Zielbindung und dieses Projekt fehlt das Prüfrecht.")
    auftrag = (
        Auftrag.objects.select_related("projekt").filter(pk=auftrag_id, projekt=projekt).first()
    )
    if auftrag is None:
        raise dienst.KeinZugriff("Nicht gefunden.")

    ziel = recht.zielbindung
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

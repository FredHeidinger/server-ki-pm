"""E1-Befund (26.11.2026): W1-Freigabe auf einem frischen Werkbankziel und Erlaubnis über Commits.

Erwartungen aus dem echten Lauf mit TestAlpha, vor dem Code geschrieben:
1. Eine Werkbank, die gerade abfragt (Kontakt innerhalb von 90 s), ist für die Freigabe bereit, auch
   wenn sie für dieses Ziel noch nie einen Auftrag übernommen hat. Ohne Kontakt und ohne frühere
   Meldung bleibt die Freigabe gesperrt.
2. Der Repository-Fingerabdruck der Werkbank ist der aktuelle Commit (``git:<HEAD>``). Ein neuer
   Commit ist kein Zielwechsel: Die W1-Erlaubnis bleibt gültig, freigegebene Aufträge bleiben
   freigegeben.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import dienst, werkbank
from steuerung.models import Auftrag

pytestmark = pytest.mark.django_db
CAP = {"waehrung": "USD", "geld_mikro": 5_000_000, "zeit_ms": 600_000, "aufrufe": 10}
KLEIN = {"waehrung": "USD", "geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1}
PLANUNG = {
    "ziel": "Monatsübersicht",
    "nicht_ziele": "kein Export",
    "schreibumfang": "kassenbuch/**",
    "abnahmekriterien": "Summen stimmen",
}


def _frisch(w_pbindung, kontakt):
    """Ein Ziel, für das die Werkbank noch nie übernommen hat (wie testalpha auf CT100)."""
    w_pbindung.bereit = False
    w_pbindung.repo_fingerabdruck = ""
    w_pbindung.save()
    inst = w_pbindung.instanz
    inst.letzter_kontakt = kontakt
    inst.save(update_fields=["letzter_kontakt"])
    return w_pbindung


def _erlaubnis(akteur, projekt, bindung, ziel):
    return werkbank.delegation_erteilen(
        akteur,
        projekt,
        bindung,
        aktionen=["auftrag_anlegen", "freigeben", "zuweisen", "starten", "integrieren"],
        ziele=[ziel],
        budget=CAP,
        gueltig_bis=timezone.now() + timedelta(days=1),
        standard_ziel=ziel,
    )


def _anlegen(projekt, bindung, d, titel="Monatsübersicht"):
    return werkbank.delegation_auftrag_anlegen(
        dienst.Akteur.aus_bindung(bindung),
        projekt,
        delegation_id=d.pk,
        revision=d.revision,
        budget=KLEIN,
        titel=titel,
        beschreibung="K-03",
        planung={**PLANUNG, "schreibumfang": f"{titel.lower()}/**"},
        freigeben=True,
    )


def test_erreichbare_werkbank_ist_fuer_ein_frisches_ziel_bereit(
    akteur, projekt, bindung, w_pbindung
):
    ziel = _frisch(w_pbindung, timezone.now() - timedelta(seconds=5))
    d = _erlaubnis(akteur, projekt, bindung, ziel)
    a = _anlegen(projekt, bindung, d)
    assert a.zustand == Auftrag.FREIGEGEBEN


@pytest.mark.parametrize("vor_sekunden", [None, 91])
def test_ohne_meldung_und_ohne_aktuellen_kontakt_bleibt_die_freigabe_gesperrt(
    akteur, projekt, bindung, w_pbindung, vor_sekunden
):
    kontakt = None if vor_sekunden is None else timezone.now() - timedelta(seconds=vor_sekunden)
    ziel = _frisch(w_pbindung, kontakt)
    d = _erlaubnis(akteur, projekt, bindung, ziel)
    with pytest.raises(dienst.Konflikt, match="Bereitschaft"):
        _anlegen(projekt, bindung, d)
    assert not Auftrag.objects.filter(chat_delegation=d).exists()


def test_neuer_commit_entwertet_die_erlaubnis_nicht(akteur, projekt, bindung, w_pbindung):
    w_pbindung.bereit = True
    w_pbindung.repo_fingerabdruck = "git:aaaaaaaaaaaa"
    w_pbindung.save()
    d = _erlaubnis(akteur, projekt, bindung, w_pbindung)
    erster = _anlegen(projekt, bindung, d, titel="Erster")
    # Die Werkbank hat gebaut und meldet beim nächsten Übernehmen den neuen Commit.
    w_pbindung.repo_fingerabdruck = "git:bbbbbbbbbbbb"
    w_pbindung.save()
    werkbank.delegation_freigaben_pruefen(akteur, projekt, delegation_id=d.pk)
    erster.refresh_from_db()
    assert erster.zustand == Auftrag.FREIGEGEBEN
    assert _anlegen(projekt, bindung, d, titel="Zweiter").zustand == Auftrag.FREIGEGEBEN


def test_freigabe_bleibt_wenn_die_werkbank_spaeter_nicht_abfragt(
    akteur, projekt, bindung, w_pbindung
):
    """Gegenprüfung: Bereitschaft ist Voraussetzung für die Freigabe, nicht für ihr Fortbestehen.
    Ein fremdes Ereignis (hier die Nachprüfung) bei gerade stiller Werkbank hebt sie nicht auf."""
    ziel = _frisch(w_pbindung, timezone.now() - timedelta(seconds=5))
    d = _erlaubnis(akteur, projekt, bindung, ziel)
    a = _anlegen(projekt, bindung, d)
    inst = ziel.instanz
    inst.letzter_kontakt = timezone.now() - timedelta(hours=2)
    inst.save(update_fields=["letzter_kontakt"])
    werkbank.delegation_freigaben_pruefen(akteur, projekt, delegation_id=d.pk)
    a.refresh_from_db()
    assert a.zustand == Auftrag.FREIGEGEBEN

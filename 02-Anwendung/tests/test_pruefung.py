"""Ergebnis schließt ab: bestanden → erledigt, fehlgeschlagen → blockiert, ohne Tests → erledigt
mit Vermerk. Keine Nutzerabnahme."""

import pytest

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Meldung, Pruefung

pytestmark = pytest.mark.django_db


def _uebernommen(freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    return chat


def test_ergebnis_braucht_dateien_aber_keine_pruefungen(projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    with pytest.raises(dienst.DienstFehler, match="Datei"):
        dienst.ergebnis_einreichen(
            chat, freigegeben, 2, zusammenfassung="x", dateien="", pruefungen="p"
        )
    with pytest.raises(dienst.DienstFehler, match="Zusammenfassung"):
        dienst.ergebnis_einreichen(chat, freigegeben, 2, zusammenfassung=" ", dateien="a@1")
    assert freigegeben.zustand == Auftrag.IN_ARBEIT


def test_bestandene_pruefungen_erledigen_ohne_abnahme(projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    e = dienst.ergebnis_einreichen(
        chat,
        freigegeben,
        2,
        zusammenfassung="Hinweistext umgesetzt.",
        dateien="src/auth/login.py@9c2e7b1",
        pruefungen="pytest tests/auth: 12 bestanden\nruff: sauber",
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT and freigegeben.ist_endzustand
    assert freigegeben.abgenommene_revision == 2 and not freigegeben.hat_bearbeiter
    assert freigegeben.letzter_bearbeiter_bindung == bindung
    assert e.pruefungsliste == ["pytest tests/auth: 12 bestanden", "ruff: sauber"]
    assert not e.ohne_tests and not Pruefung.objects.exists()
    assert Ereignis.objects.filter(
        objekt_kennung=freigegeben.nummer, ereignis="Ergebnis eingereicht, erledigt"
    ).exists()
    # Erledigt ist abgeschlossen: kein Einreichen, keine Vorschläge, nicht mehr offen.
    with pytest.raises(dienst.KeinZugriff):
        dienst.ergebnis_einreichen(chat, freigegeben, 2, zusammenfassung="x", dateien="a@1")
    with pytest.raises(dienst.Konflikt, match="abgeschlossen"):
        dienst.vorschlag_einreichen(chat, freigegeben, 2, "noch eine Idee")
    assert list(dienst.auftraege_fuer_bindung(bindung, projekt)) == []


def test_fehlgeschlagene_pruefung_blockiert_mit_grund(owner, projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    e = dienst.ergebnis_einreichen(
        chat,
        freigegeben,
        2,
        zusammenfassung="fast fertig",
        dateien="a@1",
        pruefungen="ruff: sauber",
        fehlgeschlagen="pytest: 2 fehlgeschlagen",
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.BLOCKIERT and freigegeben.bearbeiter_bindung == bindung
    assert e.fehlgeschlagenliste == ["pytest: 2 fehlgeschlagen"] and not e.ohne_tests
    grund = Meldung.objects.get(auftrag=freigegeben, art=Meldung.FEHLER)
    assert grund.text == "Prüfung fehlgeschlagen: pytest: 2 fehlgeschlagen" and not grund.historisch
    wartet = [x for x in dienst.posteingang(owner) if x["art"] == "blockade"]
    assert [x["auftrag"] for x in wartet] == [freigegeben]
    assert "Prüfung fehlgeschlagen" in wartet[0]["text"]
    # Der Bearbeiter bessert nach und reicht erneut ein; jetzt erledigt.
    dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="behoben", dateien="a@2", pruefungen="pytest: grün"
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT
    assert not [x for x in dienst.posteingang(owner) if x["art"] == "blockade"]


def test_ohne_pruefungen_erledigt_mit_vermerk(projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    e = dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="Doku angepasst", dateien="README.md@1"
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT and e.ohne_tests
    assert Ereignis.objects.filter(
        objekt_kennung=freigegeben.nummer,
        ereignis="Ergebnis eingereicht, erledigt ohne automatische Tests",
    ).exists()


def test_abnahme_wird_nicht_mehr_erreicht(akteur, projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    e = dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="fertig", dateien="a@1", pruefungen="p ok"
    )
    with pytest.raises(dienst.Konflikt, match="wartet nicht auf Prüfung"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[("ERFUELLT", ""), ("ERFUELLT", "")], entscheidung="ABGENOMMEN"
        )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT
    erreichbar = {z for z, _ in Auftrag.ZUSTAENDE}
    assert erreichbar == {
        "ENTWURF",
        "FREIGEGEBEN",
        "IN_ARBEIT",
        "BLOCKIERT",
        "ERLEDIGT",
        "ABGEBROCHEN",
    }


def test_pruefbericht_eines_anderen_chats_bleibt_hinweis(akteur, projekt, freigegeben, bindung):
    chat = _uebernommen(freigegeben, bindung)
    with pytest.raises(dienst.KeinZugriff, match="eigenen Auftrag"):
        dienst.pruefbericht_einreichen(chat, freigegeben, 2, "sieht gut aus")
    pruefchat, _ = dienst.bindung_anlegen(akteur, projekt, name="Prüfchat")
    m = dienst.pruefbericht_einreichen(
        dienst.Akteur.aus_bindung(pruefchat), freigegeben, 2, "Tests grün"
    )
    assert m.art == "PRUEFBERICHT"
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT

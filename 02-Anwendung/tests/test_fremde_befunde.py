import pytest

from steuerung import dienst
from steuerung.models import Auftrag, Modul
from tests.conftest import arbeitspaket
from tests.test_api import api
from tests.test_ordnersperren import melden

pytestmark = pytest.mark.django_db


@pytest.fixture
def ergebnisfall(akteur, projekt, bindung):
    modul = Modul.objects.create(projekt=projekt, kennung="standards", name="Standards")
    erste = arbeitspaket(akteur, projekt, "Erste Facharbeit", "erste/**")
    zweite = arbeitspaket(akteur, projekt, "Zweite Facharbeit", "zweite/**")
    # Das fachlich zuständige Modul beider Aufträge ist Kern.
    for auftrag in (erste, zweite):
        auftrag.modul = projekt.module.get(kennung="kern")
        auftrag.save(update_fields=["modul"])
        dienst.auftrag_freigeben(akteur, auftrag)
        auftrag.refresh_from_db()
    return api(bindung.klartext), modul, erste, zweite


def ergebnis(client, auftrag, **daten):
    return melden(
        client,
        auftrag,
        "api_ergebnis",
        zusammenfassung="Facharbeit fertig",
        dateien=["datei.py@abc"],
        pruefungen=["Fachprüfung bestanden"],
        **daten,
    )


def test_fremde_befunde_sammeln_im_eigentuemer_ohne_auftrag_zu_blockieren(ergebnisfall):
    client, modul, erste, zweite = ergebnisfall
    befund = {"text": "Doppelte Importstruktur", "modul": "standards", "beleg": "check:17"}
    neu = {"text": "Veralteter Prüfschlüssel", "modul": "standards", "beleg": "check:42"}
    for auftrag, befunde in ((erste, [befund]), (zweite, [befund, neu])):
        assert melden(client, auftrag, "api_uebernehmen").status_code == 200
        antwort = ergebnis(client, auftrag, fremde_befunde=befunde)
        assert antwort.status_code == 200 and antwort.json()["auftrag"]["zustand"] == "ERLEDIGT"
        (issue,) = modul.auftraege.filter(typ=Auftrag.TYP_ISSUE)
        assert issue.zustand == Auftrag.ENTWURF
        assert antwort.json()["ergebnis"]["fremde_befunde"] == [
            b | {"issue": issue.nummer} for b in befunde
        ]
    assert issue.meldungen.filter(text__contains="check:17").count() == 1
    assert issue.meldungen.filter(text__contains="check:42").count() == 1


@pytest.mark.parametrize("kennung", ["kern", "unbekannt"])
def test_eigenes_oder_unbekanntes_modul_ist_kein_fremdbefund(ergebnisfall, kennung):
    client, modul, auftrag, _ = ergebnisfall
    assert melden(client, auftrag, "api_uebernehmen").status_code == 200
    antwort = ergebnis(
        client, auftrag, fremde_befunde=[{"text": "Fehler", "modul": kennung, "beleg": "x:3"}]
    )
    assert antwort.status_code == 400 and "fremde_befunde" in antwort.json()["fehler"]
    assert not auftrag.ergebnisse.exists() and not modul.auftraege.exists()
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.IN_ARBEIT


def test_eigener_fehlgeschlagener_test_blockiert_trotz_fremdbefund(ergebnisfall):
    client, modul, auftrag, _ = ergebnisfall
    assert melden(client, auftrag, "api_uebernehmen").status_code == 200
    antwort = ergebnis(
        client,
        auftrag,
        fremde_befunde=[{"text": "Separater Fehler", "modul": "standards", "beleg": "x:3"}],
        fehlgeschlagen=["Fachprüfung fehlgeschlagen"],
    )
    assert antwort.status_code == 200 and antwort.json()["auftrag"]["zustand"] == "BLOCKIERT"
    assert modul.auftraege.filter(typ=Auftrag.TYP_ISSUE).count() == 1

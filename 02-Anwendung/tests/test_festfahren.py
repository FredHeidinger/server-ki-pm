import pytest

from steuerung import dienst
from steuerung.models import Auftrag
from tests.test_api import api
from tests.test_ordnersperren import melden

pytestmark = pytest.mark.django_db


def fortschritt(client, auftrag, kriterien):
    return melden(
        client,
        auftrag,
        "api_fortschritt",
        text="Stand der laufenden Sitzung",
        kriterien_erfuellt=kriterien,
    )


def ende(client, auftrag):
    return melden(client, auftrag, "api_sitzung_beendet")


def test_drei_sitzungen_ohne_zuwachs_warten_bis_fred_antwortet(freigegeben, bindung, akteur):
    a = freigegeben
    client = api(bindung.klartext)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    for nr in range(3):
        stand = fortschritt(client, a, [])
        assert stand.status_code == 200, stand.content
        assert ende(client, a).json()["vermerkt"] is True
        assert ende(client, a).json()["vermerkt"] is False
        a.refresh_from_db()
        assert a.zustand == (Auftrag.BLOCKIERT if nr == 2 else Auftrag.IN_ARBEIT)
    assert fortschritt(client, a, [1]).status_code == 409
    ergebnis = melden(client, a, "api_ergebnis", zusammenfassung="Fertig?", dateien=["datei@abc"])
    assert ergebnis.status_code == 409
    assert a.meldungen.filter(text__icontains="neu schneiden oder abbrechen").count() == 1
    assert melden(client, a, "api_antworten", text="Trotzdem weiter").status_code == 403
    _, pc = dienst.bindung_anlegen(akteur, None, name="Fred am PC")
    antwort = melden(
        api(pc), a, "api_antworten", text="Im kleineren Umfang weiter", fred_sagte="Mach weiter."
    )
    assert antwort.status_code == 200, antwort.content
    assert fortschritt(client, a, [1]).status_code == 200
    assert ende(client, a).json()["vermerkt"] is True
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT


def test_messbarer_zuwachs_unterbricht_die_serie(freigegeben, bindung):
    a = freigegeben
    client = api(bindung.klartext)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    for stand in ([], [], [1], [1], [1], [1, 2]):
        antwort = fortschritt(client, a, stand)
        assert antwort.status_code == 200
        assert antwort.json()["auftrag"]["kriterien_erfuellt"] == stand
        assert ende(client, a).json()["vermerkt"] is True
        a.refresh_from_db()
        assert a.zustand == Auftrag.IN_ARBEIT


@pytest.mark.parametrize("ungueltig", [[0], [3], [True], ["1"], [1, 1], "1"])
def test_fortschritt_weist_ungueltige_kriterien_ohne_meldung_ab(freigegeben, bindung, ungueltig):
    a = freigegeben
    client = api(bindung.klartext)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    vorher = a.meldungen.count()
    antwort = fortschritt(client, a, ungueltig)
    assert antwort.status_code == 400 and "kriterien_erfuellt" in antwort.json()["fehler"]
    assert a.meldungen.count() == vorher

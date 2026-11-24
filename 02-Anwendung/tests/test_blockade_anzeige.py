"""KIPM-32: „Blockiert“ getrennt anzeigen. Ein Auftrag, der nur auf eine technische Lieferung
wartet, hängt nicht an Fred („wartet auf Lieferung von …“, nicht in „Wartet auf dich“); eine
Rückfrage, ein Hindernis oder ein Fehler braucht ihn („braucht Antwort“). Übersicht,
„Wartet auf dich“, kipm_auftraege und Sitzungsstart zeigen dieselbe Unterscheidung."""

import pytest
from django.test import Client
from django.urls import reverse

from steuerung import dienst, lieferungen
from steuerung.models import Auftrag
from tests.conftest import arbeitspaket
from tests.test_api import api
from tests.test_lieferungen import _liefern, _warten

pytestmark = pytest.mark.django_db


@pytest.fixture
def paar(akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    auftraege = []
    for name in ("quelle", "verbraucher"):
        a = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, a)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        auftraege.append(a)
    return chat, *auftraege


def _api_eintrag(bindung, auftrag):
    d = api(bindung.klartext).get("/api/auftraege/").json()
    return next(e for e in d["auftraege"] if e["nummer"] == auftrag.nummer)


def _sitzungsstart(bindung):
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    return c.get("/api/sitzungsstart/").json()


def _board(owner_client, projekt, **get):
    url = reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    return owner_client.get(url, get).content.decode()


def test_wartet_auf_lieferung_haengt_nicht_an_fred(paar, projekt, bindung, owner_client):
    chat, anbieter, verbraucher = paar
    _warten(chat, verbraucher, anbieter)
    verbraucher.refresh_from_db()
    assert verbraucher.zustand == Auftrag.BLOCKIERT

    erwartet = {
        "art": "lieferung",
        "text": f"wartet auf Lieferung von {anbieter.nummer} (In Arbeit)",
        "von": [{"auftrag": anbieter.nummer, "zustand": "In Arbeit"}],
    }
    assert lieferungen.blockade(verbraucher) == erwartet
    assert verbraucher not in dienst.wartet_auf_dich(projekt)
    assert _api_eintrag(bindung, verbraucher)["blockade"] == erwartet

    start = _sitzungsstart(bindung)
    eintrag = next(e for e in start["offen"] if e["nummer"] == verbraucher.nummer)
    assert eintrag["blockade"] == erwartet
    assert verbraucher.nummer not in {w["nummer"] for w in start["wartet"]}

    assert f"Wartet auf Lieferung von {anbieter.nummer} (In Arbeit)" in _board(
        owner_client, projekt
    )
    assert f"wartet auf Lieferung von {anbieter.nummer} (In Arbeit)" in _board(
        owner_client, projekt, ansicht="liste"
    )


def test_rueckfrage_braucht_antwort(paar, projekt, bindung, owner_client):
    chat, _, verbraucher = paar
    dienst.rueckfrage_melden(chat, verbraucher, verbraucher.revision_nr, "Welche Farbe?")
    verbraucher.refresh_from_db()

    erwartet = {"art": "antwort", "text": "braucht Antwort", "von": []}
    assert lieferungen.blockade(verbraucher) == erwartet
    assert verbraucher in dienst.wartet_auf_dich(projekt)
    assert _api_eintrag(bindung, verbraucher)["blockade"] == erwartet

    start = _sitzungsstart(bindung)
    eintrag = next(e for e in start["offen"] if e["nummer"] == verbraucher.nummer)
    assert eintrag["blockade"] == erwartet
    assert verbraucher.nummer in {w["nummer"] for w in start["wartet"]}

    assert "Braucht deine Antwort." in _board(owner_client, projekt)
    assert "braucht Antwort" in _board(owner_client, projekt, ansicht="liste")


def test_hindernis_ohne_lieferbedarf_braucht_antwort(paar, projekt):
    chat, _, verbraucher = paar
    dienst.hindernis_melden(chat, verbraucher, verbraucher.revision_nr, "Zugang fehlt")
    verbraucher.refresh_from_db()
    assert lieferungen.blockade(verbraucher)["art"] == "antwort"
    assert verbraucher in dienst.wartet_auf_dich(projekt)


def test_mischfall_lieferung_erfuellt_antwort_offen(paar, projekt, bindung):
    chat, anbieter, verbraucher = paar
    _warten(chat, verbraucher, anbieter)
    _liefern(chat, anbieter)
    verbraucher.refresh_from_db()
    assert verbraucher.zustand == Auftrag.IN_ARBEIT  # Lieferung hat ihn geweckt
    dienst.rueckfrage_melden(chat, verbraucher, verbraucher.revision_nr, "Noch eine Frage")
    verbraucher.refresh_from_db()

    assert lieferungen.blockade(verbraucher)["art"] == "antwort"
    assert verbraucher in dienst.wartet_auf_dich(projekt)
    assert _api_eintrag(bindung, verbraucher)["blockade"]["art"] == "antwort"


def test_nicht_blockiert_hat_keine_blockade(paar, bindung):
    _, anbieter, _ = paar
    assert lieferungen.blockade(anbieter) is None
    assert _api_eintrag(bindung, anbieter)["blockade"] is None

"""Konzept 21.1: Führende Stelle pro Weg, ohne doppelte Auftragsführung."""

import json

import pytest

from steuerung import akte, mcp
from tests import test_mcp
from tests.test_mcp import _rpc, _werkzeug

pytestmark = pytest.mark.django_db
pc_zugang = test_mcp.pc_zugang  # Fixture wiederverwenden

KERNWORTE = (
    "nur in KI-PM",
    "fred_sagte",
    "keine parallelen Auftragsdateien",
    "Fachakte",
    "technische Pakete",
    "W2",
    "W3",
)


def test_anweisungen_enthalten_die_regel():
    for wort in KERNWORTE:
        assert wort in mcp.ANWEISUNGEN, wort
    for art in ("Pakete", "Aufgaben", "Issues"):
        assert art in mcp.ANWEISUNGEN, art


def test_regel_kommt_beim_verbindungsaufbau_an(client, pc_zugang):
    antwort = _rpc(client, pc_zugang.klartext, "initialize", {"protocolVersion": "2025-06-18"})
    anweisungen = antwort.json()["result"]["instructions"]
    assert all(w in anweisungen for w in KERNWORTE)


def test_projektkontext_nennt_die_regel(client, bindung, projekt):
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_projektkontext", fred_sagte=None)
    assert not fehler
    text = json.dumps(daten, ensure_ascii=False)
    for wort in KERNWORTE:
        assert wort in text, wort


def test_regel_bleibt_auch_bei_knappem_budget(akteur, projekt):
    k = akte.kontext_erzeugen(akteur, projekt, budget=1500, _speichern=False)
    assert "Arbeitsregel" in k.manifest["abschnitte"]
    assert "Arbeitsregel" not in k.manifest["ausgelassen"]
    assert all(w in "\n".join(k.teile) for w in KERNWORTE)

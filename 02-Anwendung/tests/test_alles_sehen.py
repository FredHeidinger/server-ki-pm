"""Freds Vorgabe vom 23.11.2026: Codex (organisiert) und Claude (arbeitet ab) sehen über ihre
PC-Zugänge alles, was in KI-PM vorgeht. Jeder gültige PC-Zugang prüft lesend für jeden anderen
gültigen Zugang, ob dieser einen Auftrag übernehmen könnte, und sieht alle Vollmachten. Ändern darf
dadurch niemand mehr als vorher; Projektschlüssel bleiben auf ihr Projekt und das Einzelrecht
beschränkt."""

import json
from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import auftragsaktionen, dienst
from steuerung.models import Aktionsvorschlag, Auftrag, Meldung
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.fixture
def codex(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="ChatGPT Desktop (Codex)")
    b.klartext = schluessel
    return b


@pytest.fixture
def claude(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    b.klartext = schluessel
    return b


def _pruefen(client, wer, auftrag, ziel):
    return _werkzeug(
        client,
        wer.klartext,
        "kipm_uebernahme_pruefen",
        projekt=auftrag.projekt.kennung,
        auftrag=auftrag.nummer,
        revision=auftrag.revision_nr,
        zielbindung_id=ziel.pk,
        unbeaufsichtigt=True,
        fred_sagte=None,
    )


def test_codex_prueft_fuer_claude_ohne_einzelrecht_in_jedem_projekt(
    client, akteur, projekt, projekt_b, codex, claude
):
    for p in (projekt, projekt_b):
        a = arbeitspaket(akteur, p, titel=f"frei in {p.kennung}", umfang="frei/**")
        dienst.auftrag_freigeben(akteur, a)
        a.refresh_from_db()
        fehler, daten = _pruefen(client, codex, a, claude)
        assert not fehler, daten
        assert daten["uebernehmbar"] is True and daten["sperrgruende"] == []
        a.refresh_from_db()
        assert a.zustand == Auftrag.FREIGEGEBEN and not a.hat_bearbeiter  # nur gelesen


def test_gesperrter_auftrag_nennt_die_gruende(client, akteur, projekt, codex, claude):
    a = arbeitspaket(akteur, projekt, titel="liegt bei Claude", umfang="belegt/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(claude, projekt=projekt), a, a.revision_nr)
    a.refresh_from_db()
    fehler, daten = _pruefen(client, codex, a, claude)
    assert not fehler, daten
    assert daten["uebernehmbar"] is False
    assert "BEREITS_UEBERNOMMEN" in {g["code"] for g in daten["sperrgruende"]}


def test_claude_prueft_ebenso_fuer_codex(client, akteur, projekt, codex, claude):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    fehler, daten = _pruefen(client, claude, a, codex)
    assert not fehler and daten["uebernehmbar"] is True


def test_projektschluessel_braucht_weiter_das_einzelrecht(client, akteur, projekt, bindung, claude):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    fehler, daten = _pruefen(client, bindung, a, claude)
    assert fehler and "fehlt das Prüfrecht" in daten["fehler"]


def test_unbekannter_zielzugang_wird_nicht_verraten(client, akteur, projekt, codex):
    a = arbeitspaket(akteur, projekt)
    fehler, daten = _werkzeug(
        client,
        codex.klartext,
        "kipm_uebernahme_pruefen",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
        zielbindung_id=99999,
        unbeaufsichtigt=True,
        fred_sagte=None,
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."


def test_projekt_nennt_pc_zugaengen_alle_zugaenge_ohne_schluessel(
    client, projekt, bindung, codex, claude
):
    fehler, daten = _werkzeug(client, codex.klartext, "kipm_projekt", projekt=projekt.kennung)
    assert not fehler, daten
    zugaenge = {z["name"]: z for z in daten["zugaenge"]}
    assert zugaenge["Claude Desktop"]["id"] == claude.pk
    assert zugaenge["ChatGPT Desktop (Codex)"]["id"] == codex.pk
    assert zugaenge["Chat A"]["art"] == "projekt"
    roh = json.dumps(daten)
    for b in (codex, claude, bindung):
        assert b.klartext not in roh
    fehler, eigen = _werkzeug(client, bindung.klartext, "kipm_projekt")
    assert not fehler and "zugaenge" not in eigen


def test_alle_vollmachten_fuer_pc_zugaenge_sichtbar(client, akteur, projekt, codex, claude):
    vm = auftragsaktionen.vollmacht_erteilen(
        akteur,
        projekt,
        aktion=Aktionsvorschlag.WIEDERAUFNAHME,
        ausfuehrende_bindung=claude,
        bearbeiter_bindung=claude,
        meldungsarten=[Meldung.FEHLER],
        gueltig_bis=timezone.now() + timedelta(days=3),
    )
    fehler, daten = _werkzeug(client, codex.klartext, "kipm_aktionen", projekt=projekt.kennung)
    assert not fehler, daten
    eintrag = next(v for v in daten["vollmachten"] if v["nummer"] == vm.nummer)
    assert eintrag["ausfuehrender_zugang"] == "Claude Desktop"

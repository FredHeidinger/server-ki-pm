"""Regressionen aus der unabhängigen Gegenprüfung der Projektakte."""

from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import akte, dienst
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def test_link_ohne_freigabe_im_kontext_archiviert_nicht(akteur, projekt):
    u = akte.link_anlegen(akteur, projekt, titel="Ablage", uri="https://cloud.example/s/abc?x=1")
    assert "cloud.example/s/abc" in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    akte.unterlage_archivieren(akteur, u, True)
    assert "cloud.example" not in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text


def test_teilung_haelt_datenbloecke_geschlossen(akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_bearbeiten(
        akteur,
        a,
        titel=a.titel,
        beschreibung="",
        ziel="Zeile Ziel.\n" * 3000,
        nicht_ziele="keine",
        arbeitsort=projekt.arbeitsorte.first(),
        schreibumfang="src/**",
        quellen="",
        abnahmekriterien="K",
        prioritaet="mittel",
        modul=None,
    )
    a.refresh_from_db()
    k = akte.kontext_erzeugen(akteur, projekt, auftrag=a, budget=12_000)
    assert len(k.teile) >= 3
    for teil in k.teile:
        assert teil.count("```") % 2 == 0, teil[-200:]
        assert len(teil) <= 12_000 + 700
    assert k.vollstaendig_text.count("Zeile Ziel.") == 3000


def test_kontextteil_bleibt_gleich_trotz_tageswechsel(akteur, projekt, monkeypatch):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    k = akte.kontext_erzeugen(akteur, projekt, auftrag=a)
    spaeter = timezone.now() + timedelta(days=3)
    monkeypatch.setattr(timezone, "now", lambda: spaeter)
    k2, _ = akte.kontext_teil(akteur, projekt, k.kontext_id, 1)
    assert k2.hash == k.hash

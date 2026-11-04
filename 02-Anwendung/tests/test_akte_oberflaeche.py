"""Hauptformulare der Projektakte ohne JavaScript bedienen (Teil von P21) und Nutzertexte
nie als HTML ausgeben."""

import pytest
from django.urls import reverse

from steuerung import akte
from steuerung.models import (
    Unterlage,
)

pytestmark = pytest.mark.django_db


def _u(name, projekt, **weitere):
    return reverse(name, kwargs={"kennung": projekt.kennung, **weitere})


def test_profil(owner_client, projekt):
    owner_client.post(
        _u("akte_profil", projekt),
        {
            "revision": 0,
            "zweck": "Sagt, was läuft.",
            "zielgruppe": "Fred",
            "nicht_ziele": "",
            "phase": "Aufbau",
            "entwicklungsstufe": "Erste Nutzung",
        },
    )
    projekt.refresh_from_db()
    assert projekt.profil_revision == 1 and projekt.phase == "Aufbau"
    auswahl = owner_client.get(reverse("projekte") + "?q=läuft").content.decode()
    assert projekt.name in auswahl and "Aufbau" in auswahl


def test_unterlagen_formulare(owner_client, akteur, projekt):
    owner_client.post(
        _u("unterlage_neu", projekt) + "?art=link",
        {
            "art": "link",
            "titel": "Spezifikation",
            "beschreibung": "",
            "uri": "https://example.org/spec",
            "quellversion": "",
        },
    )
    u = Unterlage.objects.get()
    detail = owner_client.get(_u("unterlage_detail", projekt, nummer=u.nummer)).content.decode()
    assert "veränderliche Quelle" in detail
    aktion = lambda name: _u("unterlage_aktion", projekt, nummer=u.nummer, aktion=name)  # noqa: E731
    owner_client.post(
        aktion("link"),
        {"uri": "https://example.org/spec", "quellversion": "2026-11-01", "erwartete_nr": 1},
    )
    # A-5: Rolle und KI-Freigabe gibt es in der Oberfläche nicht mehr.
    assert (
        owner_client.post(aktion("rolle"), {"version": 2, "rolle": "grundlage"}).status_code == 404
    )
    assert owner_client.post(aktion("ki"), {"frei": "1"}).status_code == 404
    assert owner_client.post(aktion("zuordnen"), {"version": 2}).status_code == 404
    assert owner_client.post(aktion("pruefen"), {"version": 2}).status_code == 404
    u.refresh_from_db()
    assert u.aktuelle_nr == 2 and not u.ki_freigegeben
    alt = owner_client.get(_u("unterlage_detail", projekt, nummer=u.nummer) + "?fassung=1")
    assert "Ältere Fassung 1" in alt.content.decode()
    owner_client.post(aktion("archiv"), {"archiv": "1"})
    u.refresh_from_db()
    assert u.archiviert
    assert "Spezifikation" not in owner_client.get(_u("akte_wissen", projekt)).content.decode()
    assert (
        "Spezifikation"
        in owner_client.get(_u("akte_wissen", projekt) + "?archiv=1").content.decode()
    )


def test_nutzertexte_der_akte_bleiben_text(owner_client, akteur, projekt):
    boese = "<script>alert(1)</script>"
    akte.text_anlegen(akteur, projekt, titel=boese, text=boese)
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=0, zweck=boese)
    for url in (
        _u("projekt_detail", projekt),
        _u("akte_wissen", projekt),
        _u("unterlage_detail", projekt, nummer=1),
        _u("projekt_verlauf", projekt),
    ):
        seite = owner_client.get(url).content.decode()
        assert boese not in seite and "&lt;script&gt;" in seite, url

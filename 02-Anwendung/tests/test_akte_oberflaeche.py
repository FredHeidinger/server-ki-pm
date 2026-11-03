"""Hauptformulare der Projektakte ohne JavaScript bedienen (Teil von P21) und Nutzertexte
nie als HTML ausgeben."""

import pytest
from django.urls import reverse

from steuerung import akte, github
from steuerung.models import (
    Installation,
    Meilenstein,
    Release,
    RepoVerbindung,
    Umgebung,
    Unterlage,
)
from tests.conftest import arbeitspaket
from tests.test_github import Attrappe

pytestmark = pytest.mark.django_db


def _u(name, projekt, **weitere):
    return reverse(name, kwargs={"kennung": projekt.kennung, **weitere})


def test_betrieb_formulare(owner_client, akteur, projekt):
    owner_client.post(
        _u("umgebung_neu", projekt),
        {"u-name": "Testserver", "u-art": "test", "u-beschreibung": "CT 100", "u-url": ""},
    )
    u = Umgebung.objects.get()
    antwort = owner_client.post(
        _u("release_neu", projekt),
        {
            "kennung": "1.0.0",
            "titel": "Erstes",
            "beschreibung": "",
            "komponenten_eintrag": ["Anwendung | fred/app | 9c2e7b1", "Daten | | " + "a" * 64, ""],
        },
    )
    assert antwort.status_code == 302
    r = Release.objects.get()
    komponenten = {k.komponente: (k.commit, k.artefakt_hash) for k in r.komponenten.all()}
    assert komponenten == {"Anwendung": ("9c2e7b1", ""), "Daten": ("", "a" * 64)}
    owner_client.post(
        _u("release_zustand", projekt, pk=r.pk), {"zustand": "veroeffentlicht", "revision": 1}
    )
    r.refresh_from_db()
    assert r.zustand == "veroeffentlicht" and not Installation.objects.exists()
    owner_client.post(
        _u("installation_melden", projekt),
        {
            "i-umgebung": u.pk,
            "i-komponente": "Anwendung",
            "i-version": "9c2e7b1",
            "i-release": r.pk,
            "i-installiert_am": "",
            "i-herkunft": "Proxmox-Zeile",
        },
    )
    i = Installation.objects.get()
    assert i.zustand == "gemeldet"
    owner_client.post(_u("installation_bewerten", projekt, pk=i.pk, wahl="bestaetigen"))
    i.refresh_from_db()
    assert i.zustand == "bestaetigt"
    seite = owner_client.get(_u("projekt_detail", projekt)).content.decode()
    assert "Testserver" in seite and "9c2e7b1" in seite and "bestätigt" in seite


def test_meilenstein_und_profil(owner_client, akteur, projekt):
    f = akte.funktion_anlegen(akteur, projekt, titel="Export")
    owner_client.post(
        _u("meilenstein_neu", projekt),
        {"m-name": "Erste Nutzung", "m-ziel": "", "m-zielversion": "1.0", "m-funktionen": [f.pk]},
    )
    m = Meilenstein.objects.get()
    assert list(m.funktionen.all()) == [f]
    owner_client.post(
        _u("meilenstein_zustand", projekt, pk=m.pk), {"zustand": "erreicht", "revision": 1}
    )
    m.refresh_from_db()
    assert m.zustand == "erreicht"
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
    owner_client.post(aktion("rolle"), {"version": 2, "rolle": "grundlage"})
    owner_client.post(aktion("ki"), {"frei": "1"})
    a = arbeitspaket(akteur, projekt)
    owner_client.post(aktion("zuordnen"), {"version": 2, "ziel": f"a:{a.pk}", "zweck": "Vorgabe"})
    u.refresh_from_db()
    assert u.aktuelle_nr == 2 and u.ki_freigegeben and akte.geltende_grundlage(u).nummer == 2
    assert a.unterlagen.get().version.nummer == 2
    owner_client.post(aktion("archiv"), {"archiv": "1"})
    u.refresh_from_db()
    assert u.archiviert
    assert "Spezifikation" not in owner_client.get(_u("akte_wissen", projekt)).content.decode()
    assert (
        "Spezifikation"
        in owner_client.get(_u("akte_wissen", projekt) + "?archiv=1").content.decode()
    )


def test_github_verbinden_und_aktualisieren_ueber_oberflaeche(owner_client, projekt):
    gh = Attrappe()
    github.adapter_setzen(gh)
    try:
        einstellungen = owner_client.get(_u("projekt_einstellungen", projekt)).content.decode()
        assert "Repository verbinden" in einstellungen
        owner_client.post(
            _u("repo_verbinden", projekt),
            {
                "r-name": "fred/test",
                "r-schreibend": "on",
                "r-aktionen": ["issue_anlegen"],
                "r-pfade": "docs/**",
            },
        )
        v = RepoVerbindung.objects.get()
        antwort = owner_client.post(
            _u("repo_aktion", projekt, pk=v.pk, aktion="aktualisieren"), follow=True
        )
        assert "GitHub aktualisiert" in antwort.content.decode()
        betrieb = owner_client.get(_u("akte_betrieb", projekt)).content.decode()
        assert "fred/test" in betrieb and "Alt" in betrieb and "zuletzt erfolgreich" in betrieb
        owner_client.post(_u("repo_aktion", projekt, pk=v.pk, aktion="widerrufen"))
        v.refresh_from_db()
        assert not v.aktiv
    finally:
        github.adapter_setzen(None)
    hinweis = owner_client.get(_u("akte_betrieb", projekt)).content.decode()
    assert "nicht eingerichtet" in hinweis


def test_nutzertexte_der_akte_bleiben_text(owner_client, akteur, projekt):
    boese = "<script>alert(1)</script>"
    f = akte.funktion_anlegen(akteur, projekt, titel=boese, nutzen=boese)
    akte.text_anlegen(akteur, projekt, titel=boese, text=boese)
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=0, zweck=boese)
    for url in (
        _u("projekt_detail", projekt),
        _u("akte_funktionen", projekt),
        _u("funktion_detail", projekt, nummer=f.nummer),
        _u("akte_wissen", projekt),
        _u("unterlage_detail", projekt, nummer=1),
        _u("akte_kontext", projekt),
        _u("akte_suche", projekt) + "?q=script",
        _u("projekt_verlauf", projekt),
    ):
        seite = owner_client.get(url).content.decode()
        assert boese not in seite and "&lt;script&gt;" in seite, url

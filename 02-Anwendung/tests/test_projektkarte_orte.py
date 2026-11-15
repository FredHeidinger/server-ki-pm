"""Projektkarte in der Projektübersicht: Arbeitsort und Repository statt Kurzbeschreibung.

Freds Wunsch (15.11.2026): „in den kästchen die notendigsten infos eben zb arbeitsort und
repository anzeigen mit grüner schrift wenn verbunden und rot wenn nicht verbunden […], die
kurzbeschreibung brauche ich dort nicht“; Festlegung A B B:
- Arbeitsort grün „bestätigt“ nach Freds Häkchen, rot „Vorschlag“ oder „kein Arbeitsort“.
- Repository grün „verbunden“ nur, wenn der GitHub-Leseschlüssel es lesen kann, gelb
  „eingetragen, nicht gelesen“, rot „kein Repository“.
- Mehrere Orte als Zählzeile („2 von 3 bestätigt“), bei genau einem Ort der Ordnername.
"""

import pytest
from django.urls import reverse

from steuerung import abgleich, akte, dienst, github
from steuerung.models import Arbeitsort, GitHubZugang
from tests.github_attrappe import Attrappe

pytestmark = pytest.mark.django_db

# Zusammengesetzt, damit die Spiegelprüfung kein Geheimnis-Literal sieht.
SCHLUESSEL = "github" + "_pat_" + "A1b2C3d4" * 8


@pytest.fixture
def gh():
    attrappe = Attrappe()
    github.adapter_setzen(attrappe)
    yield attrappe
    github.adapter_setzen(None)


def _karte(client, name="Synthetisches A") -> str:
    html = client.get(reverse("projekte")).content.decode()
    teil = html.split(f'projektwahl-name">{name}</strong>')[1]
    return teil.split("</a>")[0]


def _zeile(karte: str, was: str) -> str:
    return karte.split(f'data-zeile="{was}"')[1].split("</span></span>")[0]


def test_karte_zeigt_keine_kurzbeschreibung(owner_client, akteur, projekt):
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=None, zweck="Geheimer Zweck")
    dienst.projekt_registrieren(akteur, kennung="leer", name="Leer", beschreibung="Lange Rede")
    html = owner_client.get(reverse("projekte")).content.decode()
    for weg in ("Geheimer Zweck", "Lange Rede", "Zweck noch nicht erfasst.", "projektwahl-zweck"):
        assert weg not in html, weg


def test_ein_bestaetigter_ort_zeigt_ordnername_gruen(owner_client, projekt):
    Arbeitsort.objects.filter(projekt=projekt).update(pfad=r"W:\Projekte\SynthA")
    zeile = _zeile(_karte(owner_client), "arbeitsort")
    assert "Arbeitsort" in zeile and "SynthA" in zeile and "bestätigt" in zeile
    assert "ort-zustand gut" in zeile


def test_vorschlag_ist_rot(owner_client, projekt):
    Arbeitsort.objects.filter(projekt=projekt).update(bestaetigt=False)
    zeile = _zeile(_karte(owner_client), "arbeitsort")
    assert "Vorschlag" in zeile and "ort-zustand schlecht" in zeile


def test_ohne_arbeitsort_rot_und_ohne_repository_rot(owner_client, akteur):
    dienst.projekt_registrieren(akteur, kennung="nackt", name="Nackt")
    karte = _karte(owner_client, "Nackt")
    ort, repo = _zeile(karte, "arbeitsort"), _zeile(karte, "repository")
    assert "kein Arbeitsort" in ort and "ort-zustand schlecht" in ort
    assert "kein Repository" in repo and "ort-zustand schlecht" in repo


def test_archivierter_ort_zaehlt_nicht(owner_client, projekt):
    Arbeitsort.objects.filter(projekt=projekt).update(aktiv=False)
    assert "kein Arbeitsort" in _zeile(_karte(owner_client), "arbeitsort")


def test_repository_ohne_github_zugang_ist_gelb(owner_client, projekt):
    Arbeitsort.objects.filter(projekt=projekt).update(repository="https://github.com/fred/test")
    zeile = _zeile(_karte(owner_client), "repository")
    assert "eingetragen, nicht gelesen" in zeile and "ort-zustand mittel" in zeile


def test_repository_lesbar_ist_gruen(owner_client, akteur, projekt, gh):
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    Arbeitsort.objects.filter(projekt=projekt).update(
        repository="https://github.com/Fred/Test.git/"
    )
    zeile = _zeile(_karte(owner_client), "repository")
    assert "verbunden" in zeile and "ort-zustand gut" in zeile
    assert "fred/test" not in zeile.lower()  # Festlegung B: Zustand, kein Repository-Name


def test_repository_nicht_im_lesezugang_ist_gelb(owner_client, akteur, projekt, gh):
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    Arbeitsort.objects.filter(projekt=projekt).update(repository="https://github.com/fred/fremd")
    assert "eingetragen, nicht gelesen" in _zeile(_karte(owner_client), "repository")


def test_abgelehnter_schluessel_liest_nichts(owner_client, akteur, projekt, gh):
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    GitHubZugang.objects.update(abgelehnt=True)
    Arbeitsort.objects.filter(projekt=projekt).update(repository="https://github.com/fred/test")
    assert "eingetragen, nicht gelesen" in _zeile(_karte(owner_client), "repository")


def test_mehrere_orte_als_zaehlzeile(owner_client, akteur, projekt, gh):
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    Arbeitsort.objects.filter(projekt=projekt).update(repository="https://github.com/fred/test")
    dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="zwei", name="Zwei", repository="https://github.com/fred/fremd"
    )
    dienst.arbeitsort_anlegen(akteur, projekt, kennung="drei", name="Drei")
    Arbeitsort.objects.filter(projekt=projekt, kennung="drei").update(bestaetigt=False)
    karte = _karte(owner_client)
    ort, repo = _zeile(karte, "arbeitsort"), _zeile(karte, "repository")
    assert "Arbeitsorte" in ort and "2 von 3 bestätigt" in ort and "ort-zustand schlecht" in ort
    assert "Repositorys" in repo and "1 von 3 verbunden" in repo and "ort-zustand mittel" in repo


def test_mehrere_orte_alle_gut_ist_gruen(owner_client, akteur, projekt, gh):
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    Arbeitsort.objects.filter(projekt=projekt).update(repository="https://github.com/fred/test")
    dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="zwei", name="Zwei", repository="https://github.com/fred/zweites"
    )
    karte = _karte(owner_client)
    ort, repo = _zeile(karte, "arbeitsort"), _zeile(karte, "repository")
    assert "2 von 2 bestätigt" in ort and "ort-zustand gut" in ort
    assert "2 von 2 verbunden" in repo and "ort-zustand gut" in repo


def test_repo_name_aus_adresse():
    name = abgleich.repo_name_aus_adresse
    assert name("https://github.com/FredHeidinger/AI-Live") == "fredheidinger/ai-live"
    assert name("https://github.com/a/b.git") == "a/b"
    assert name("https://www.github.com/a/b/") == "a/b"
    assert name("https://gitlab.com/a/b") == ""
    assert name("") == ""

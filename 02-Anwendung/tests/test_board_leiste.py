"""Modulauswahl als Leiste über dem Board und eigene Spalte für offene Issues."""

import pytest
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Auftrag, Modul
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def _url(projekt, zusatz=""):
    return reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung}) + zusatz


def _seite(client, url):
    antwort = client.get(url)
    assert antwort.status_code == 200
    return antwort.content.decode("utf-8")


def _spalte(html, name):
    teil = html.split(f'id="spalte-{name}"')[1]
    return teil.split("</section>")[0]


def _neu(akteur, projekt, titel, *, frei=False, issue=False, modul=None):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel}/**")
    felder = {}
    if issue:
        felder["typ"] = Auftrag.TYP_ISSUE
    if modul:
        felder["modul"] = modul
    Auftrag.objects.filter(pk=a.pk).update(**felder)
    if frei:
        dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    return a


@pytest.fixture
def module(akteur, projekt):
    kern = dienst.modul_anlegen(akteur, projekt, kennung="alpha", name="Alpha")
    karten = dienst.modul_anlegen(
        akteur, projekt, kennung="alphaunter", name="Alphaunter", eltern=kern
    )
    web = dienst.modul_anlegen(akteur, projekt, kennung="beta", name="Beta")
    return {"kern": kern, "karten": karten, "web": web}


# --- Spalte Issues ------------------------------------------------------------------------------


def test_spalten_in_fester_reihenfolge(owner_client, projekt):
    html = _seite(owner_client, _url(projekt))
    namen = ("issues", "entwurf", "freigegeben", "arbeit", "blockiert", "erledigt")
    stellen = [html.index(f'id="spalte-{n}"') for n in namen]
    assert stellen == sorted(stellen)


def test_offene_issues_nur_in_ihrer_spalte(owner_client, akteur, projekt):
    _neu(akteur, projekt, "Mein Absturz", issue=True)
    _neu(akteur, projekt, "Ein Auftrag")
    html = _seite(owner_client, _url(projekt))
    assert "Mein Absturz" in _spalte(html, "issues") and "Ein Auftrag" not in _spalte(
        html, "issues"
    )
    assert "Ein Auftrag" in _spalte(html, "entwurf") and "Mein Absturz" not in _spalte(
        html, "entwurf"
    )


def test_issue_nennt_seinen_zustand_und_behaelt_den_knopf(owner_client, akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    a = _neu(akteur, projekt, "Hängt", issue=True, frei=True)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.rueckfrage_melden(chat, a, a.revision_nr, "Welche Sprache?")
    spalte = _spalte(_seite(owner_client, _url(projekt)), "issues")
    assert "Blockiert" in spalte and '<summary class="knopf klein">Antworten</summary>' in spalte


def test_erledigtes_issue_wandert_nach_erledigt(owner_client, akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    a = _neu(akteur, projekt, "Behoben", issue=True, frei=True)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.ergebnis_einreichen(
        chat, a, a.revision_nr, zusammenfassung="Ok", dateien="a@b", pruefungen="p: ok"
    )
    html = _seite(owner_client, _url(projekt))
    assert "Behoben" in _spalte(html, "erledigt") and "Behoben" not in _spalte(html, "issues")


def test_entscheidungen_stehen_nicht_im_board(owner_client, akteur, projekt):
    akte.entscheidung_fragen(akteur, projekt, frage="Welche Farbe nehmen wir?")
    html = _seite(owner_client, _url(projekt))
    assert "Welche Farbe nehmen wir?" not in html.split('class="board"')[1]


def test_filter_auftraege_und_issues_gilt_weiter(owner_client, akteur, projekt):
    _neu(akteur, projekt, "Mein Absturz", issue=True)
    _neu(akteur, projekt, "Ein Auftrag")
    nur_auftraege = _seite(owner_client, _url(projekt, "?art=auftrag"))
    assert "Ein Auftrag" in nur_auftraege and "Mein Absturz" not in nur_auftraege
    nur_issues = _seite(owner_client, _url(projekt, "?art=issue"))
    assert "Mein Absturz" in nur_issues and "Ein Auftrag" not in nur_issues


# --- Modulleiste --------------------------------------------------------------------------------


def test_leiste_steht_ueber_dem_board(owner_client, projekt, module):
    html = _seite(owner_client, _url(projekt))
    assert html.index('class="modulleiste"') < html.index('class="board"')
    assert "baumspalte" not in html and "mit-baum" not in html and "baum-schmal" not in html


def test_ohne_module_keine_leiste(owner_client, projekt_b):
    assert "modulleiste" not in _seite(owner_client, _url(projekt_b))


def test_leiste_nennt_alle_module_und_zaehlt_verstaendlich(owner_client, akteur, projekt, module):
    _neu(akteur, projekt, "In Kern", modul=module["kern"])
    _neu(akteur, projekt, "In Kern als Issue", issue=True, modul=module["kern"])
    html = _seite(owner_client, _url(projekt))
    leiste = html.split('class="modulleiste"')[1].split("</nav>")[0]
    for wort in ("Alle Module", "Alpha", "Beta", "Ohne Modul"):
        assert wort in leiste
    assert 'title="1 Auftrag, 1 Issue"' in leiste
    assert "<abbr" not in leiste


def test_auswahl_ist_markiert_und_filtert(owner_client, akteur, projekt, module):
    _neu(akteur, projekt, "In Kern", modul=module["kern"])
    _neu(akteur, projekt, "In Web", modul=module["web"])
    html = _seite(owner_client, _url(projekt, "?modul=beta"))
    assert "In Web" in html and "In Kern" not in html
    leiste = html.split('class="modulleiste"')[1].split("</nav>")[0]
    assert leiste.count('aria-current="true"') == 1
    aktuell = leiste.split('aria-current="true"')[1].split("</a>")[0]
    assert "Beta" in aktuell


def test_untermodule_erscheinen_erst_nach_der_wahl(owner_client, projekt, module):
    assert ">Alphaunter<" not in _seite(owner_client, _url(projekt)).split("</nav>")[-2]
    html = _seite(owner_client, _url(projekt, "?modul=alpha"))
    leiste = html.split('class="modulleiste"')[1].split("</nav>")[0]
    assert "Alphaunter" in leiste
    tief = _seite(owner_client, _url(projekt, "?modul=alphaunter"))
    leiste = tief.split('class="modulleiste"')[1].split("</nav>")[0]
    assert "Alphaunter" in leiste and leiste.count('aria-current="true"') == 1


def test_modul_filter_umfasst_untermodule(owner_client, akteur, projekt, module):
    _neu(akteur, projekt, "Tief unten", modul=module["karten"])
    assert "Tief unten" in _seite(owner_client, _url(projekt, "?modul=alpha"))
    assert isinstance(module["karten"], Modul)


def test_leiste_behaelt_den_art_filter(owner_client, projekt, module):
    html = _seite(owner_client, _url(projekt, "?art=issue"))
    leiste = html.split('class="modulleiste"')[1].split("</nav>")[0]
    assert "art=issue" in leiste and "modul=alpha" in leiste

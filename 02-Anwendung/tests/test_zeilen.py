"""Zeilen statt Kästchen: Aufträge untereinander, nach Zustand gruppiert; Startseite als Zeilen."""

import re

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def _url(projekt):
    return reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})


def _neu(akteur, projekt, titel, *, frei=False, typ=None):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel}/**")
    if typ:
        Auftrag.objects.filter(pk=a.pk).update(typ=typ)
    if frei:
        dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    return a


def _seite(client, url):
    antwort = client.get(url)
    assert antwort.status_code == 200
    return antwort.content.decode("utf-8")


@pytest.fixture
def gemischt(akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    entwurf = _neu(akteur, projekt, "Mein Entwurf")
    frei = _neu(akteur, projekt, "Schon frei", frei=True)
    laeuft = _neu(akteur, projekt, "Läuft gerade", frei=True)
    dienst.auftrag_uebernehmen(chat, laeuft, laeuft.revision_nr)
    block = _neu(akteur, projekt, "Hängt fest", frei=True)
    dienst.auftrag_uebernehmen(chat, block, block.revision_nr)
    dienst.rueckfrage_melden(chat, block, block.revision_nr, "Welche Sprache?")
    fertig = _neu(akteur, projekt, "Ist fertig", frei=True)
    dienst.auftrag_uebernehmen(chat, fertig, fertig.revision_nr)
    dienst.ergebnis_einreichen(
        chat, fertig, fertig.revision_nr, zusammenfassung="Ok", dateien="a@b", pruefungen="p: ok"
    )
    weg = _neu(akteur, projekt, "Verworfen")
    dienst.auftrag_abbrechen(akteur, weg, "Nicht mehr nötig")
    return {
        "entwurf": entwurf,
        "frei": frei,
        "laeuft": laeuft,
        "block": block,
        "fertig": fertig,
        "weg": weg,
    }


def test_keine_kaesten_und_keine_umschaltung(owner_client, projekt, gemischt):
    html = _seite(owner_client, _url(projekt))
    assert 'class="board"' not in html and 'class="karte' not in html and "spalte-" not in html
    assert ">Board<" not in html and ">Liste<" not in html
    assert html.count('<li class="zeile"') == 6  # fünf Gruppen und der zugeklappte Abgebrochene


def test_gruppen_stehen_in_fester_reihenfolge(owner_client, projekt, gemischt):
    html = _seite(owner_client, _url(projekt))
    stellen = [
        html.index(f'id="gruppe-{k}"')
        for k in ("blockiert", "arbeit", "frei", "entwurf", "erledigt")
    ]
    assert stellen == sorted(stellen)
    assert re.search(r'id="gruppe-blockiert"[^<]*>Blockiert <span class="anzahl">1</span>', html)


def test_zeile_nennt_nummer_titel_und_zeigt_den_knopf(owner_client, projekt, gemischt):
    html = _seite(owner_client, _url(projekt))
    frei = html.split('id="gruppe-frei"')[1].split('id="gruppe-entwurf"')[0]
    assert gemischt["frei"].nummer in frei and "Schon frei" in frei
    block = html.split('id="gruppe-blockiert"')[1].split('id="gruppe-arbeit"')[0]
    assert "Hängt fest" in block and '<summary class="knopf klein">Antworten</summary>' in block
    entwurf = html.split('id="gruppe-entwurf"')[1].split('id="gruppe-erledigt"')[0]
    assert '<button type="submit" class="knopf klein">Freigeben</button>' in entwurf


def test_zeile_fuehrt_zum_auftrag_und_issue_als_wort(owner_client, akteur, projekt):
    a = _neu(akteur, projekt, "Ein Fehler", typ=Auftrag.TYP_ISSUE)
    html = _seite(owner_client, _url(projekt))
    assert reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}) in html
    assert '<span class="issue">Issue</span>' in html


def test_abgebrochene_sind_zugeklappt_erreichbar(owner_client, projekt, gemischt):
    html = _seite(owner_client, _url(projekt))
    assert "Verworfen" in html.split('id="gruppe-abgebrochen"')[1]
    assert "<details" in html.split('id="gruppe-abgebrochen"')[0].rsplit("<section", 1)[-1] or (
        '<details class="zeilengruppe" id="gruppe-abgebrochen"' in html
    )


def test_erledigte_nur_die_letzten_zehn_mit_weg_zu_allen(owner_client, akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    for i in range(12):
        a = _neu(akteur, projekt, f"Fertig {i:02}", frei=True)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        dienst.ergebnis_einreichen(
            chat, a, a.revision_nr, zusammenfassung="Ok", dateien="a@b", pruefungen="p: ok"
        )
    html = _seite(owner_client, _url(projekt))
    teil = html.split('id="gruppe-erledigt"')[1]
    assert "Fertig 11" in teil and "Fertig 02" in teil and "Fertig 01" not in teil
    assert "Alle 12 erledigten" in teil and "zustand=ERLEDIGT" in teil
    alle = _seite(owner_client, _url(projekt) + "?zustand=ERLEDIGT")
    assert "Fertig 00" in alle and "Fertig 11" in alle


def test_filter_gelten_auch_in_zeilen(owner_client, akteur, projekt, gemischt):
    _neu(akteur, projekt, "Ein Fehler", typ=Auftrag.TYP_ISSUE)
    nur_frei = _seite(owner_client, _url(projekt) + "?zustand=FREIGEGEBEN")
    assert "Schon frei" in nur_frei and "Hängt fest" not in nur_frei
    issues = _seite(owner_client, _url(projekt) + "?art=issue")
    assert "Ein Fehler" in issues and "Schon frei" not in issues
    offen = _seite(owner_client, _url(projekt) + "?zustand=offen")
    assert "Schon frei" in offen and "Ist fertig" not in offen and "Verworfen" not in offen
    niemand = _seite(owner_client, _url(projekt) + "?bearbeiter=niemand")
    assert "Schon frei" in niemand and "Läuft gerade" not in niemand
    ungeplant = _seite(owner_client, _url(projekt) + "?reifegrad=geplant")
    assert "Mein Entwurf" in ungeplant and "Schon frei" not in ungeplant


def test_leere_auswahl_sagt_das(owner_client, projekt):
    html = _seite(owner_client, _url(projekt))
    assert "Nichts für diese Auswahl" in html and 'id="gruppe-' not in html


def test_alter_link_auf_die_liste_geht_weiter(owner_client, projekt, gemischt):
    assert "Schon frei" in _seite(owner_client, _url(projekt) + "?ansicht=liste")


def test_auftragsseite_fuehrt_zurueck_zu_den_auftraegen(owner_client, gemischt):
    a = gemischt["frei"]
    html = _seite(owner_client, reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}))
    assert "Zurück zu den Aufträgen" in html and "Zurück zum Board" not in html


def test_startseite_zeigt_zeilen_mit_kuerzel(owner_client, projekt, projekt_b):
    html = _seite(owner_client, reverse("projekte"))
    assert html.count('class="projektzeile"') == 2 and "projektwahl-karte" not in html
    assert f'<span class="kuerzel projektzeile-kuerzel">{projekt.kuerzel}</span>' in html
    assert "offen" in html and "wartet auf dich" in html


def test_wartet_auf_dich_steht_in_einer_kopfzeile(owner_client, gemischt, projekt):
    html = _seite(owner_client, reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    eintrag = html.split('class="eingang"')[1].split("</ul>")[0]
    kopf = eintrag.split('class="eintragkopf"')[1].split("</div>")[0]
    assert "Blockiert" in kopf and "Hängt fest" in kopf

"""Auftragsansicht: Seite und Panel gleich aufgebaut, oben der nächste Schritt mit einer
Hauptaktion, seltene Aktionen eingeklappt, Reiter als Anker (ohne JavaScript bedienbar)."""

from django.urls import reverse

from steuerung import dienst
from tests.conftest import arbeitspaket


def _seite(client, auftrag, panel=False):
    name = "auftrag_panel" if panel else "auftrag_detail"
    return client.get(reverse(name, kwargs={"auftrag_id": auftrag.pk})).content.decode()


def test_seite_und_panel_haben_denselben_aufbau(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    for html in (_seite(owner_client, a), _seite(owner_client, a, panel=True)):
        assert "Nächster Schritt: Bereit zur Freigabe" in html
        assert html.index("Nächster Schritt") < html.index('data-reiter="auftrag"')
        for teil in ("auftrag", "arbeit", "verlauf"):
            assert f'href="#a{a.pk}-{teil}"' in html and f'id="a{a.pk}-{teil}"' in html
        assert '<details class="weitere-aktionen">' in html and "Abbrechen" in html
    seite = _seite(owner_client, a)
    assert "Arbeitsaufträge</a> › " + a.nummer in seite


def test_naechster_schritt_je_zustand(owner_client, akteur, projekt, zweiter_mensch):
    wunsch = dienst.wunsch_erfassen(akteur, projekt, titel="Nur Wunsch", beschreibung="b")
    html = _seite(owner_client, wunsch)
    assert "Nächster Schritt: Planen" in html and "Freigeben – nicht möglich" in html
    a = arbeitspaket(akteur, projekt, titel="Zweiter", umfang="docs/**", pruefer=zweiter_mensch)
    dienst.auftrag_freigeben(akteur, a)
    html = _seite(owner_client, a)
    assert "wartet auf Übernahme" in html and "Ich bearbeite das selbst" in html
    # Freigabe zurücknehmen ist selten: eingeklappt unter „Weitere Aktionen“.
    assert html.index("Weitere Aktionen") < html.index("Freigabe zurücknehmen")

"""Modulbaum (Verbesserungsliste 2c): Untermodule beliebig tief, Schutz gegen Kreise, Zahlen je
Knoten einschließlich Untermodule, Filter in den Aufträgen, Pflege in Oberfläche und
über MCP; dazu Projektauswahl mit festen Plätzen (2a) und Bereiche nur einmal (2b)."""

from pathlib import Path

import pytest
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Modul
from tests.test_mcp import _werkzeug


@pytest.fixture
def baum(akteur, projekt):
    """synth-a: Kern (aus der Fixture) und Oberfläche > Projektauswahl > Karten."""
    ober = dienst.modul_anlegen(akteur, projekt, kennung="oberflaeche", name="Oberfläche")
    wahl = dienst.modul_anlegen(akteur, projekt, kennung="wahl", name="Projektauswahl", eltern=ober)
    karten = dienst.modul_anlegen(akteur, projekt, kennung="karten", name="Karten", eltern=wahl)
    dienst.wunsch_erfassen(akteur, projekt, titel="Karten bauen", beschreibung="b", modul=wahl)
    dienst.wunsch_erfassen(
        akteur, projekt, titel="Karte bricht um", beschreibung="b", modul=karten, typ="issue"
    )
    dienst.wunsch_erfassen(
        akteur,
        projekt,
        titel="Kern-Auftrag",
        beschreibung="b",
        modul=projekt.module.get(kennung="kern"),
    )
    akte.text_anlegen(akteur, projekt, titel="Kartenentwurf", text="x", modul=ober)
    return {"ober": ober, "wahl": wahl, "karten": karten}


def _knoten(daten, kennung):
    return next(k for k in daten["flach"] if k.modul.kennung == kennung)


def test_zahlen_je_knoten_schliessen_untermodule_ein(projekt, baum):
    daten = dienst.modulbaum(projekt)
    assert [k.modul.kennung for k in daten["knoten"]] == ["kern", "oberflaeche"]
    ober = _knoten(daten, "oberflaeche")
    assert ober.zahlen == {"auftraege": 1, "issues": 1, "unterlagen": 1}
    assert _knoten(daten, "karten").tiefe == 3
    assert _knoten(daten, "karten").zahlen == {"auftraege": 0, "issues": 1, "unterlagen": 0}
    assert daten["projektweit"]["auftraege"] == 0
    assert daten["gesamt"] == {"auftraege": 2, "issues": 1, "unterlagen": 1}


def test_filter_umfasst_untermodule(owner_client, projekt, baum):
    auftraege = reverse("projekt_auftraege", kwargs={"kennung": "synth-a"})
    board = owner_client.get(auftraege + "?modul=wahl").content.decode()
    assert 'aria-current="true"' in board and "Nach Modul filtern" in board
    assert "Karten bauen" in board and "Karte bricht um" in board and "Kern-Auftrag" not in board
    liste = owner_client.get(auftraege + "?modul=karten&ansicht=liste").content.decode()
    assert "Karte bricht um" in liste and "Karten bauen" not in liste
    # Unbekannte Kennung filtert nichts weg und wählt nichts.
    alles = owner_client.get(auftraege + "?modul=gibtsnicht").content.decode()
    assert "Kern-Auftrag" in alles and "Karten bauen" in alles


def test_kein_kreis_und_kein_fremdes_elternmodul(akteur, projekt, projekt_b, baum):
    with pytest.raises(dienst.Konflikt):
        dienst.modul_aendern(akteur, baum["ober"], eltern=baum["karten"])
    with pytest.raises(dienst.Konflikt):
        dienst.modul_aendern(akteur, baum["wahl"], eltern=baum["wahl"])
    fremd = dienst.modul_anlegen(akteur, projekt_b, kennung="b1", name="B1")
    with pytest.raises(dienst.KeinZugriff):
        dienst.modul_aendern(akteur, baum["wahl"], eltern=fremd)
    with pytest.raises(dienst.KeinZugriff):
        dienst.modul_anlegen(akteur, projekt, kennung="x", name="X", eltern=fremd)
    baum["ober"].refresh_from_db()
    assert baum["ober"].eltern is None


def test_umhaengen_archivieren_zurueckholen(akteur, projekt, baum):
    kern = projekt.module.get(kennung="kern")
    wahl = dienst.modul_aendern(akteur, baum["wahl"], eltern=kern, erwartete_revision=1)
    assert wahl.eltern == kern and wahl.revision == 2
    with pytest.raises(dienst.Konflikt):
        dienst.modul_aendern(akteur, wahl, name="Alt", erwartete_revision=1)
    with pytest.raises(dienst.Konflikt):
        dienst.modul_archivieren(akteur, wahl, aktiv=False)  # Karten ist noch aktiv
    dienst.modul_archivieren(akteur, baum["karten"], aktiv=False)
    dienst.modul_archivieren(akteur, wahl, aktiv=False)
    with pytest.raises(dienst.Konflikt):
        dienst.modul_archivieren(akteur, baum["karten"], aktiv=True)  # Elternmodul archiviert
    # Archivierte Untermodule zählen beim aktiven Vorfahren weiter mit.
    assert _knoten(dienst.modulbaum(projekt), "kern").zahlen["issues"] == 1


def test_einstellungen_pflegen_ohne_javascript(owner_client, projekt, baum):
    seite = owner_client.get(
        reverse("projekt_einstellungen", kwargs={"kennung": "synth-a"})
    ).content.decode()
    assert "+ Untermodul" in seite and "Hängt unter" in seite
    assert "Archivieren – nicht möglich" in seite  # Oberfläche hat aktive Untermodule
    owner_client.post(
        reverse("modul_neu", kwargs={"kennung": "synth-a"}),
        {"modul-name": "Kachelraster", "eltern": "karten"},
    )
    neu = Modul.objects.get(name="Kachelraster")
    assert neu.eltern == baum["karten"]
    owner_client.post(
        reverse("modul_aendern", kwargs={"kennung": "synth-a", "modul": "kachelraster"}),
        {"name": "Raster", "eltern": "", "revision": neu.revision},
    )
    neu.refresh_from_db()
    assert (neu.name, neu.eltern, neu.kennung) == ("Raster", None, "kachelraster")
    # Kreis über die Oberfläche: abgewiesen, Meldung statt Fehlerseite.
    antwort = owner_client.post(
        reverse("modul_aendern", kwargs={"kennung": "synth-a", "modul": "oberflaeche"}),
        {"name": "Oberfläche", "eltern": "karten", "revision": baum["ober"].revision},
        follow=True,
    )
    assert "Kreis" in antwort.content.decode()


def test_einstellungen_fremdes_elternmodul_nicht_gefunden(owner_client, akteur, projekt_b, baum):
    dienst.modul_anlegen(akteur, projekt_b, kennung="fremd", name="Fremd")
    antwort = owner_client.post(
        reverse("modul_neu", kwargs={"kennung": "synth-a"}),
        {"modul-name": "Unter Fremd", "eltern": "fremd"},
    )
    assert antwort.status_code == 404
    assert not Modul.objects.filter(name="Unter Fremd").exists()


def test_mcp_module_anlegen_umbenennen_umhaengen_archivieren(client, akteur, projekt, baum):
    pc, schluessel = dienst.bindung_anlegen(akteur, None, name="Assistent")
    fehler, daten = _werkzeug(
        client, schluessel, "kipm_modul_anlegen", projekt="synth-a", name="Export", eltern="kern"
    )
    assert not fehler, daten
    export = Modul.objects.get(kennung="export")
    assert export.eltern.kennung == "kern"
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_modul_aendern",
        projekt="synth-a",
        modul="export",
        revision=export.revision,
        name="CSV-Export",
        eltern="",
    )
    assert not fehler, daten
    assert daten["modul"] == {
        "kennung": "export",
        "name": "CSV-Export",
        "eltern": None,
        "revision": 2,
        "aktiv": True,
    }
    fehler, daten = _werkzeug(
        client, schluessel, "kipm_modul_aendern", projekt="synth-a", modul="export", revision=1
    )
    assert not fehler  # nichts zu ändern
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_modul_aendern",
        projekt="synth-a",
        modul="export",
        revision=1,
        aktiv=False,
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_modul_aendern",
        projekt="synth-a",
        modul="oberflaeche",
        revision=1,
        eltern="karten",
    )
    assert fehler and "Kreis" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_modul_aendern",
        projekt="synth-a",
        modul="export",
        revision=2,
        aktiv=False,
    )
    assert not fehler and daten["modul"]["aktiv"] is False
    projekt_info = _werkzeug(client, schluessel, "kipm_projekt", projekt="synth-a")[1]
    wahl = next(m for m in projekt_info["projekt"]["module"] if m["kennung"] == "wahl")
    assert wahl["eltern"] == "oberflaeche"


def test_projektchat_darf_module_nicht_pflegen(client, bindung, baum):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_modul_aendern", modul="wahl", revision=1, name="X"
    )
    assert fehler and "Owner oder sein KI-Assistent" in daten["fehler"]
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_modul_anlegen", name="Y")
    assert fehler
    assert not Modul.objects.filter(name__in=("X", "Y")).exists()


def test_projektauswahl_feste_plaetze_und_bereiche_einmal(owner_client, akteur, projekt):
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=None, phase="Aufbau", zweck="Z")
    dienst.projekt_registrieren(akteur, kennung="ohne-zweck", name="Ohne Zweck")
    seite = owner_client.get(reverse("projekte")).content.decode()
    assert seite.count('class="projektwahl-karte"') == 2
    assert seite.count('class="kuerzel projektwahl-kuerzel"') == 2
    assert "Phase –" in seite and '<span class="chip projektwahl-phase">Aufbau</span>' in seite
    assert "Zweck noch nicht erfasst." in seite
    stil = Path(__file__).parents[1] / "steuerung" / "static" / "steuerung" / "stil.css"
    assert "@media (min-width: 901px) { .reiter { display: none; } }" in stil.read_text(
        encoding="utf-8"
    )

"""Der schlanke Stand (01-Planung/ARBEITSABLAUF.md): Weggefallenes ist weg, Tabellen und Daten früherer
Stufen bleiben, früher hochgeladene Dateien bleiben lesbar, GitHub kennt nur Lesen."""

import hashlib
from pathlib import Path

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.urls import NoReverseMatch, reverse

from steuerung import ablage, akte, dienst, github, mcp
from steuerung.models import (
    Abrufstand,
    Besuchsmarke,
    ExterneAktion,
    ExternesObjekt,
    Funktion,
    Installation,
    Kriteriumsbewertung,
    Nachweis,
    Pruefung,
    Release,
    ReleaseKomponente,
    Uebernahmebestaetigung,
    Umgebung,
    Unterlage,
    Unterlagenversion,
    Unterlagenzuordnung,
)
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name",
    [
        "meine",
        "auftraege",
        "pruefung",
        "entscheidungen",
        "entscheidung_neu",
        "verlauf",
        "modul_neu_seite",
        "akte_funktionen",
        "funktion_neu",
        "akte_betrieb",
        "release_neu",
        "installation_melden",
        "nachweis_neu",
        "hochladen",
        "akte_suche",
        "akte_kontext",
        "akte_gesehen",
        "umgebung_neu",
        "repo_verbinden",
        "github_aktion_neu",
        "api_funktion_vorschlagen",
        "api_nachweis_melden",
        "api_installation_melden",
        "api_github_vorschlag",
        "api_stand_melden",
        "api_modulstand",
        "api_profil_vorschlagen",
        "api_umgebung_vorschlagen",
        "api_release_vorschlagen",
        "api_entscheidung_vorschlagen",
        "api_vorschlag",
        "api_pruefbericht",
    ],
)
def test_entfernte_wege_gibt_es_nicht(name):
    for kwargs in ({}, {"kennung": "x"}, {"auftrag_id": 1}):
        with pytest.raises(NoReverseMatch):
            reverse(name, kwargs=kwargs)


def test_mcp_kennt_nur_die_schlanken_werkzeuge():
    namen = {w[0] for w in mcp.WERKZEUGE}
    assert namen == {
        "kipm_projekte",
        "kipm_projekt",
        "kipm_projekt_anlegen",
        "kipm_projekt_archivieren",
        "kipm_verschiebeziele",
        "kipm_verschieben_pruefen",
        "kipm_verschieben",
        "kipm_modul_anlegen",
        "kipm_modul_aendern",
        "kipm_arbeitsort_vorschlagen",
        "kipm_arbeitsort_aendern",
        "kipm_profil_aendern",
        "kipm_text_anlegen",
        "kipm_text_aendern",
        "kipm_auftraege",
        "kipm_auftrag_anlegen",
        "kipm_lesen",
        "kipm_planen",
        "kipm_antworten",
        "kipm_lieferbedarf_festlegen",
        "kipm_uebernehmen",
        "kipm_fortschritt",
        "kipm_rueckfrage",
        "kipm_hindernis",
        "kipm_fehler",
        "kipm_ergebnis",
        "kipm_warten_auf_lieferung",
        "kipm_lieferung_melden",
        "kipm_projektkontext",
        "kipm_projektakte",
        "kipm_verlauf",
        "kipm_unterlage_lesen",
        "kipm_entscheidungen",
        "kipm_entscheidung_eintragen",
        "kipm_entscheidung_aendern",
        "kipm_entscheidung_fragen",
        "kipm_meilensteine",
        "kipm_meilenstein_anlegen",
        "kipm_meilenstein_aendern",
    }
    for wort in ("kipm_stand_melden", "Funktionen", "Nachweise", "Installationen meldest"):
        assert wort not in mcp.ANWEISUNGEN


def test_jedes_werkzeug_der_liste_laesst_sich_aufrufen(client, projekt, bindung):
    """Kein Werkzeug zeigt auf einen entfernten Endpunkt: unbekannte Namen enden nie als Absturz."""
    for name, *_ in mcp.WERKZEUGE:
        fehler, daten = _werkzeug(client, bindung.klartext, name, auftrag="A-999", revision=1)
        assert fehler in (True, False), name
        assert "Unbekanntes Werkzeug" not in str(daten.get("fehler", "")), name
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_stand_melden", auftrag="A-1")
    assert fehler and "Unbekanntes Werkzeug" in daten["fehler"]


def test_melder_und_extraktion_sind_entfernt():
    wurzel = Path(__file__).resolve().parents[2]
    assert not (wurzel / "03-Server" / "skripte" / "kipm_melden.py").exists()
    assert not (wurzel / "02-Anwendung" / "steuerung" / "extrakt.py").exists()


def test_github_transport_hat_keine_schreibende_methode():
    oeffentlich = {n for n in vars(github.GitHubLesen) if not n.startswith("_")}
    assert oeffentlich == {
        "konto",
        "repositorys",
        "repository",
        "neuestes_release",
        "tags",
        "bereitstellungen",
        "bereitstellung_zustand",
    }
    assert not hasattr(github, "GitHubApp") and not hasattr(github, "app_jwt")


def test_tabellen_frueherer_stufen_bleiben():
    namen = {m._meta.model_name for m in apps.get_app_config("steuerung").get_models()}
    for modell in (
        Funktion,
        Nachweis,
        Umgebung,
        Release,
        ReleaseKomponente,
        Installation,
        Pruefung,
        Kriteriumsbewertung,
        Uebernahmebestaetigung,
        Unterlagenzuordnung,
        Abrufstand,
        ExternesObjekt,
        ExterneAktion,
        Besuchsmarke,
    ):
        assert modell._meta.model_name in namen
        assert modell.objects.count() == 0  # abfragbar: Spalten und Tabelle passen zusammen


def test_alte_daten_stoeren_seiten_und_kontext_nicht(owner_client, akteur, projekt, bindung):
    Funktion.objects.create(projekt=projekt, nummer=1, titel="Frühere Funktion")
    Umgebung.objects.create(projekt=projekt, kennung="test", name="Frühere Umgebung", art="test")
    for name in ("projekt_detail", "projekt_einstellungen", "akte_wissen", "projekt_verlauf"):
        seite = owner_client.get(reverse(name, kwargs={"kennung": projekt.kennung}))
        assert seite.status_code == 200
        text = seite.content.decode()
        assert "Frühere Funktion" not in text and "Frühere Umgebung" not in text
    kontext = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert "Frühere Funktion" not in kontext
    assert Funktion.objects.count() == 1 and Umgebung.objects.count() == 1


@pytest.fixture
def fruehere_datei(projekt, settings, tmp_path):
    """Früher hochgeladene, geprüfte Datei: nur noch als Bestand, nicht mehr anlegbar."""
    settings.KIPM_ABLAGE_DIR = tmp_path
    (tmp_path / "blobs").mkdir()
    blob = "a" * 32
    inhalt = b"alter Inhalt"
    (tmp_path / "blobs" / blob).write_bytes(inhalt)
    u = Unterlage.objects.create(
        projekt=projekt, nummer=1, titel="Altes PDF", art=Unterlage.DATEI, aktuelle_nr=1
    )
    v = Unterlagenversion.objects.create(
        unterlage=u,
        nummer=1,
        blob=blob,
        sha256=hashlib.sha256(inhalt).hexdigest(),
        groesse=len(inhalt),
        originalname="alt.pdf",
        inhaltstyp="application/pdf",
        pruefung=Unterlagenversion.SAUBER,
        autor_name="Fred",
    )
    return u, v


def test_fruehere_datei_bleibt_lesbar_und_pruefbar(owner_client, projekt, fruehere_datei):
    u, v = fruehere_datei
    kwargs = {"kennung": projekt.kennung, "nummer": u.nummer}
    wissen = owner_client.get(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))
    assert "Früher hochgeladene Dateien" in wissen.content.decode()
    detail = owner_client.get(reverse("unterlage_detail", kwargs=kwargs)).content.decode()
    assert "Herunterladen" in detail
    antwort = owner_client.get(reverse("unterlage_datei", kwargs=kwargs | {"vnr": v.nummer}))
    assert antwort.status_code == 200 and b"".join(antwort.streaming_content) == b"alter Inhalt"
    assert "attachment" in antwort["Content-Disposition"]
    call_command("ablage_pruefen")
    ablage.blob_pfad(v.blob).unlink()
    with pytest.raises(CommandError, match="fehlt"):
        call_command("ablage_pruefen")


def test_ungepruefte_fruehere_datei_bleibt_gesperrt(owner_client, projekt, fruehere_datei):
    u, v = fruehere_datei
    Unterlagenversion.objects.filter(pk=v.pk).update(pruefung=Unterlagenversion.AUSSTEHEND)
    antwort = owner_client.get(
        reverse(
            "unterlage_datei",
            kwargs={"kennung": projekt.kennung, "nummer": u.nummer, "vnr": v.nummer},
        )
    )
    assert antwort.status_code == 409


def test_kein_upload_ueber_die_oberflaeche(owner_client, projekt):
    url = reverse("unterlage_neu", kwargs={"kennung": projekt.kennung})
    seite = owner_client.get(url + "?art=datei").content.decode()
    assert 'type="file"' not in seite and "Neuer Text" in seite


def test_freigabe_braucht_keinen_pruefer(akteur, projekt):
    a = dienst.auftrag_anlegen(
        akteur,
        projekt,
        titel="Ohne Prüfer",
        planung={
            "ziel": "Ziel",
            "nicht_ziele": "keine",
            "arbeitsort": projekt.arbeitsorte.first(),
            "schreibumfang": "a/**",
            "abnahmekriterien": "Kriterium",
        },
        freigeben=True,
    )
    assert a.zustand == "FREIGEGEBEN" and a.pruefer is None

"""Umbau 3/6 (A-5): Wissen mit Entscheidungen, Texten und Links, ohne KI-Freigabe und Grundlage."""

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Entscheidung, Ereignis, Objektversion
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def _wissen_fuer(akteur, projekt, marke):
    """Je Projekt eine Entscheidung, ein Text (zwei Fassungen) und ein Link mit eigener Marke."""
    e = akte.entscheidung_eintragen(
        akteur, projekt, entscheidung=f"Entscheidung {marke}", warum=f"Grund {marke}"
    )
    u = akte.text_anlegen(akteur, projekt, titel=f"Text {marke}", text=f"Fassung eins {marke}")
    akte.text_neue_version(akteur, u, text=f"Fassung zwei {marke}", erwartete_nr=1)
    link = akte.link_anlegen(
        akteur, projekt, titel=f"Link {marke}", uri=f"https://example.org/{marke}"
    )
    return e, u, link


def test_kontext_enthaelt_alles_wissen_ohne_freigabe(akteur, projekt):
    e, u, _ = _wissen_fuer(akteur, projekt, "alpha")
    assert not u.ki_freigegeben  # keine Freigabe gesetzt, trotzdem im Kontext
    text = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert "Entscheidung alpha" in text and "Grund alpha" in text and e.nummer in text
    assert "Fassung zwei alpha" in text and "Fassung eins alpha" not in text  # neueste Fassung
    assert "https://example.org/alpha" in text
    assert "Grundlage" not in text and "nicht für KI freigegeben" not in text


def test_kontext_nie_mit_wissen_eines_fremden_projekts(
    client, akteur, projekt, projekt_b, bindung, pc_zugang
):
    _wissen_fuer(akteur, projekt, "alpha")
    _wissen_fuer(akteur, projekt_b, "beta")
    akte.profil_bearbeiten(akteur, projekt_b, erwartete_revision=0, beschreibung="Geheim beta")
    texte = [akte.kontext_erzeugen(akteur, projekt).vollstaendig_text]
    for schluessel, zusatz in (
        (bindung.klartext, {}),
        (pc_zugang.klartext, {"projekt": "synth-a"}),
    ):
        fehler, daten = _werkzeug(client, schluessel, "kipm_projektkontext", **zusatz)
        assert not fehler
        texte.append(daten["text"])
        fehler, daten = _werkzeug(client, schluessel, "kipm_entscheidungen", **zusatz)
        assert not fehler and [x["entscheidung"] for x in daten["entscheidungen"]] == [
            "Entscheidung alpha"
        ]
    for text in texte:
        assert "alpha" in text
        assert "beta" not in text  # weder Entscheidung, Text, Link noch Beschreibung von B


def test_projektschluessel_liest_und_aendert_kein_fremdes_wissen(
    client, akteur, projekt, projekt_b, bindung
):
    e_b, u_b, _ = _wissen_fuer(akteur, projekt_b, "beta")
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_entscheidung_aendern", entscheidung=e_b.nummer, stand=1
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_unterlage_lesen", unterlage=u_b.kennung, projekt="synth-b"
    )
    assert fehler or "beta" not in str(daten)
    e_b.refresh_from_db()
    assert e_b.stand == 1 and e_b.zustand == Entscheidung.ENTSCHIEDEN


def test_chat_traegt_entscheidung_direkt_ein_und_aendert_sie(client, akteur, projekt, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_eintragen",
        entscheidung="Es gibt keine Nutzerabnahme.",
        warum="Technische Prüfung genügt.",
        anfrage_kennung="e-1",
    )
    assert not fehler and daten["entscheidung"]["zustand"] == "ENTSCHIEDEN"
    assert daten["entscheidung"]["seit"] and daten["entscheidung"]["warum"]
    nummer = daten["entscheidung"]["nummer"]
    # Wiederholung mit derselben Kennung wirkt nur einmal.
    _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_eintragen",
        entscheidung="Es gibt keine Nutzerabnahme.",
        anfrage_kennung="e-1",
    )
    assert Entscheidung.objects.count() == 1
    eintrag = Ereignis.objects.get(objekt_kennung=nummer, ereignis="Entscheidung eingetragen")
    assert eintrag.projekt == projekt and eintrag.akteur_name == "Chat A"
    assert eintrag.version is not None
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_entscheidung_aendern", entscheidung=nummer, stand=9
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_aendern",
        entscheidung=nummer,
        stand=1,
        warum="Fred hat es so festgelegt.",
    )
    assert not fehler and daten["entscheidung"]["stand"] == 2
    assert daten["entscheidung"]["entscheidung"] == "Es gibt keine Nutzerabnahme."
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_aendern",
        entscheidung=nummer,
        stand=2,
        zurueckziehen=True,
    )
    assert not fehler and daten["entscheidung"]["zustand"] == "ZURUECKGEZOGEN"
    assert "keine Nutzerabnahme" not in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    arten = list(
        Ereignis.objects.filter(objekt_kennung=nummer, projekt=projekt).values_list(
            "ereignis", flat=True
        )
    )
    assert arten.count("Entscheidung geändert") == 1 and "Entscheidung zurückgezogen" in arten
    assert Objektversion.objects.filter(objekt_typ="Entscheidung").count() == 3


def test_leere_entscheidung_abgewiesen(client, bindung):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_entscheidung_eintragen", entscheidung=" "
    )
    assert fehler and "nicht leer" in daten["fehler"]
    assert not Entscheidung.objects.exists()


def test_chat_pflegt_beschreibung_direkt(client, akteur, projekt, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_profil_aendern",
        revision=0,
        beschreibung="Projektmanager für Freds Projekte.",
    )
    assert not fehler and daten["profil_revision"] == 1
    projekt.refresh_from_db()
    assert (
        projekt.beschreibung == "Projektmanager für Freds Projekte." and not projekt.profil_entwurf
    )
    eintrag = Ereignis.objects.get(objekt_typ="Projektprofil", projekt=projekt)
    assert eintrag.akteur_name == "Chat A" and "beschreibung" in eintrag.anlass
    assert (
        "Projektmanager für Freds Projekte."
        in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    )
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_profil_aendern", revision=0, beschreibung="Veraltet"
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]


def test_beschreibung_nur_vom_owner_von_hand(zweiter_mensch, projekt):
    mensch = dienst.Akteur.aus_benutzer(zweiter_mensch)
    with pytest.raises(dienst.KeinZugriff):
        akte.profil_bearbeiten(mensch, projekt, erwartete_revision=0, beschreibung="x")
    with pytest.raises(dienst.KeinZugriff):
        akte.entscheidung_eintragen(mensch, projekt, entscheidung="x")


def test_mcp_werkzeuge_ohne_freigabe_und_bestaetigung(client, bindung):
    antwort = client.post(
        reverse("mcp_link", kwargs={"schluessel": bindung.klartext}),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        content_type="application/json",
    )
    namen = {w["name"] for w in antwort.json()["result"]["tools"]}
    assert {
        "kipm_entscheidung_eintragen",
        "kipm_entscheidung_aendern",
        "kipm_profil_aendern",
        "kipm_text_anlegen",
        "kipm_text_aendern",
    } <= namen
    weg = {
        "kipm_bestaetigen",
        "kipm_entscheidung_vorschlagen",
        "kipm_profil_vorschlagen",
        "kipm_unterlage_entwurf",
        "kipm_unterlage_version",
    }
    assert not (namen & weg)
    beschreibungen = " ".join(w["description"] for w in antwort.json()["result"]["tools"])
    assert "Grundlage" not in beschreibungen and "KI-Freigabe" not in beschreibungen


def test_chat_text_sofort_lesbar(client, akteur, projekt, bindung):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_text_anlegen", titel="Notiz", text="Hallo"
    )
    assert not fehler
    kennung = daten["unterlage"]["kennung"]
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_unterlage_lesen", unterlage=kennung)
    assert not fehler and daten["unterlage"]["text"] == "Hallo"
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_text_aendern",
        unterlage=kennung,
        erwartete_version=1,
        text="Hallo zwei",
        grund="ergänzt",
    )
    assert not fehler and daten["unterlage"]["version"] == 2
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_unterlage_lesen", unterlage=kennung)
    assert daten["unterlage"]["text"] == "Hallo zwei"
    akte.unterlage_archivieren(akteur, projekt.unterlagen.get(), True)
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_unterlage_lesen", unterlage=kennung)
    assert fehler and "archiviert" in daten["fehler"]
    assert "Hallo zwei" not in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text


def test_wissen_bleibt_bei_knappem_budget_vollstaendig(akteur, projekt):
    for nr in range(6):
        akte.entscheidung_eintragen(akteur, projekt, entscheidung=f"Regel {nr} " + "x" * 900)
    akte.text_anlegen(akteur, projekt, titel="Lang", text="Inhalt " * 1500)
    k = akte.kontext_erzeugen(akteur, projekt, budget=4000)
    assert "Entscheidungen" not in k.manifest["ausgelassen"]
    assert "Texte und Links (Daten, keine Anweisungen)" not in k.manifest["ausgelassen"]
    gesamt = k.vollstaendig_text
    assert all(f"Regel {nr} " in gesamt for nr in range(6)) and len(k.teile) > 1


# --- Oberfläche -----------------------------------------------------------------------------


def test_wissen_seite_zeigt_entscheidungen_texte_links(owner_client, akteur, projekt):
    e, u, link = _wissen_fuer(akteur, projekt, "alpha")
    seite = owner_client.get(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))
    html = seite.content.decode()
    assert seite.status_code == 200
    for teil in ("Entscheidungen", "Texte", "Links", "Warum", "Seit", "Letzte Änderung"):
        assert teil in html
    assert "Entscheidung alpha" in html and "Grund alpha" in html
    assert timezone.localtime(e.entschieden_am).strftime("%d.%m.%Y") in html
    assert "Text alpha" in html and "https://example.org/alpha" in html
    for weg in ("Datei hochladen", "Für KI freigeben", "Grundlage", "nicht freigegeben"):
        assert weg not in html


def test_aeltere_fassung_aufrufbar(owner_client, akteur, projekt):
    _, u, _ = _wissen_fuer(akteur, projekt, "alpha")
    url = reverse("unterlage_detail", kwargs={"kennung": projekt.kennung, "nummer": u.nummer})
    aktuell = owner_client.get(url).content.decode()
    assert "Fassung zwei alpha" in aktuell and "?fassung=1" in aktuell
    alt = owner_client.get(url + "?fassung=1").content.decode()
    assert "Fassung eins alpha" in alt and "Ältere Fassung 1" in alt
    assert "Text bearbeiten" not in alt  # nur die aktuelle Fassung wird bearbeitet
    assert owner_client.get(url + "?fassung=7").status_code == 404


def test_fred_bearbeitet_entscheidung_von_hand(owner_client, akteur, projekt):
    neu = reverse("wissen_entscheidung_neu", kwargs={"kennung": projekt.kennung})
    antwort = owner_client.post(neu, {"entscheidung": "Upload fällt weg.", "warum": "Nur Texte."})
    assert antwort.status_code == 302
    e = Entscheidung.objects.get()
    assert e.zustand == Entscheidung.ENTSCHIEDEN and e.warum == "Nur Texte."
    url = reverse("wissen_entscheidung", kwargs={"kennung": projekt.kennung, "nummer": e.pk})
    formular = owner_client.get(url).content.decode()
    assert "Upload fällt weg." in formular and 'name="stand" value="1"' in formular
    assert 'for="id_stand"' not in formular  # verstecktes Feld ohne Beschriftung
    owner_client.post(url, {"entscheidung": "Upload fällt weg.", "warum": "Neu", "stand": 1})
    e.refresh_from_db()
    assert e.stand == 2 and e.warum == "Neu"
    # Veralteter Stand: Konflikt statt stiller Überschreibung
    antwort = owner_client.post(url, {"entscheidung": "Anders", "warum": "", "stand": 1})
    assert "inzwischen geändert" in antwort.content.decode()
    e.refresh_from_db()
    assert e.text == "Upload fällt weg."
    owner_client.post(url, {"zurueckziehen": "1", "stand": 2})
    e.refresh_from_db()
    assert e.zustand == Entscheidung.ZURUECKGEZOGEN
    assert owner_client.get(url).status_code == 404
    assert Ereignis.objects.filter(objekt_kennung=e.nummer, projekt=projekt).count() == 3


def test_fremde_entscheidung_nicht_in_der_oberflaeche(owner_client, akteur, projekt, projekt_b):
    e_b = akte.entscheidung_eintragen(akteur, projekt_b, entscheidung="Nur B")
    url = reverse("wissen_entscheidung", kwargs={"kennung": projekt.kennung, "nummer": e_b.pk})
    assert owner_client.get(url).status_code == 404
    wissen = owner_client.get(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))
    assert "Nur B" not in wissen.content.decode()


def test_beschreibung_von_hand_bearbeitbar(owner_client, projekt):
    url = reverse("akte_profil", kwargs={"kennung": projekt.kennung})
    assert 'name="beschreibung"' in owner_client.get(url).content.decode()
    owner_client.post(url, {"revision": 0, "beschreibung": "Von Hand"})
    projekt.refresh_from_db()
    assert projekt.beschreibung == "Von Hand"
    assert Ereignis.objects.filter(objekt_typ="Projektprofil", projekt=projekt).exists()

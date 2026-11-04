"""A-17: KI-PM fordert von jedem Chat Freds Worte, bevor etwas angelegt oder geändert wird."""

import json

import pytest
from django.apps import apps
from django.core import serializers
from django.test import Client
from django.urls import get_resolver, reverse

from steuerung import api, dienst, mcp
from steuerung.models import AnfrageKennung, Auftrag, ChatBindung, Ereignis, Modul
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db

WORTE = "Ja, trag das so ein."

# Jedes Werkzeug steht in genau einer Liste; ein neues Werkzeug ohne Einordnung lässt
# test_jedes_werkzeug_ist_eingeordnet scheitern.
LESEN = {
    "kipm_projekte",
    "kipm_projekt",
    "kipm_auftraege",
    "kipm_lesen",
    "kipm_projektkontext",
    "kipm_projektakte",
    "kipm_verlauf",
    "kipm_unterlage_lesen",
    "kipm_entscheidungen",
    "kipm_meilensteine",
}
ARBEIT = {
    "kipm_uebernehmen",
    "kipm_fortschritt",
    "kipm_rueckfrage",
    "kipm_hindernis",
    "kipm_fehler",
    "kipm_ergebnis",
}
# Legt nichts fest; erst Freds Antwort macht daraus eine Entscheidung (tests/test_offene_entscheidungen.py).
FRAGE = {"kipm_entscheidung_fragen"}
AENDERND = {
    "kipm_projekt_anlegen",
    "kipm_modul_anlegen",
    "kipm_modul_aendern",
    "kipm_arbeitsort_vorschlagen",
    "kipm_arbeitsort_aendern",
    "kipm_profil_aendern",
    "kipm_text_anlegen",
    "kipm_text_aendern",
    "kipm_auftrag_anlegen",
    "kipm_planen",
    "kipm_antworten",
    "kipm_entscheidung_eintragen",
    "kipm_entscheidung_aendern",
    "kipm_meilenstein_anlegen",
    "kipm_meilenstein_aendern",
}


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


@pytest.fixture
def bestand(client, akteur, projekt, bindung, pc_zugang):
    """Für jedes ändernde Werkzeug ein gültiger Aufruf: Er scheitert nur am fehlenden Feld."""

    def rufe(werkzeug, **argumente):
        fehler, daten = _werkzeug(
            client, pc_zugang.klartext, werkzeug, projekt=projekt.kennung, **argumente
        )
        assert not fehler, daten
        return daten

    # Aufträge zuerst: Der Vorschlag darunter wäre sonst ihr unbestätigter Arbeitsort.
    entwurf = arbeitspaket(akteur, projekt, titel="Geplant", umfang="geplant/**")
    blockiert = arbeitspaket(akteur, projekt, titel="Mit Frage", umfang="frage/**")
    dienst.auftrag_freigeben(akteur, blockiert)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, blockiert, blockiert.revision_nr)
    dienst.rueckfrage_melden(chat, blockiert, blockiert.revision_nr, "Welche Sprache?")
    rufe("kipm_modul_anlegen", name="Vorhanden", kennung="vorhanden")
    modul = Modul.objects.get(projekt=projekt, kennung="vorhanden")
    ort = rufe("kipm_arbeitsort_vorschlagen", name="Neuer Ort")["arbeitsort"]
    text = rufe("kipm_text_anlegen", titel="Notiz", text="Inhalt")["unterlage"]
    entscheidung = rufe("kipm_entscheidung_eintragen", entscheidung="Es gilt.")["entscheidung"]
    meilenstein = rufe("kipm_meilenstein_anlegen", name="Erste Version")["meilenstein"]
    projekt.refresh_from_db()
    return {
        "kipm_projekt_anlegen": {"name": "Ganz neu"},
        "kipm_modul_anlegen": {"name": "Zweites"},
        "kipm_modul_aendern": {
            "modul": modul.kennung,
            "revision": modul.revision,
            "name": "Umbenannt",
        },
        "kipm_arbeitsort_vorschlagen": {"name": "Zweiter Ort"},
        "kipm_arbeitsort_aendern": {
            "arbeitsort": ort["kennung"],
            "revision": ort["revision"],
            "name": "Anderer Name",
        },
        "kipm_profil_aendern": {"revision": projekt.profil_revision, "zweck": "Neuer Zweck"},
        "kipm_text_anlegen": {"titel": "Zweite Notiz", "text": "Inhalt"},
        "kipm_text_aendern": {
            "unterlage": text["kennung"],
            "erwartete_version": text["version"],
            "text": "Neu",
            "grund": "Korrektur",
        },
        "kipm_auftrag_anlegen": {"titel": "Neuer Auftrag", "text": "Beschreibung"},
        "kipm_planen": {"auftrag": entwurf.nummer, "ziel": "Neues Ziel"},
        "kipm_antworten": {"auftrag": blockiert.nummer, "text": "Deutsch"},
        "kipm_entscheidung_eintragen": {"entscheidung": "Noch etwas gilt."},
        "kipm_entscheidung_aendern": {
            "entscheidung": entscheidung["nummer"],
            "stand": entscheidung["stand"],
            "text": "Es gilt anders.",
        },
        "kipm_meilenstein_anlegen": {"name": "Zweite Version"},
        "kipm_meilenstein_aendern": {
            "meilenstein": meilenstein["nummer"],
            "revision": meilenstein["revision"],
            "name": "Umbenannter Meilenstein",
        },
    }


def _abbild() -> dict:
    """Alle fachlichen Zeilen. Nur die Bindung merkt sich Ordner und Pfad des letzten Aufrufs."""
    return {
        m._meta.label: serializers.serialize("json", m.objects.all())
        for m in apps.get_app_config("steuerung").get_models()
        if m is not ChatBindung
    }


# --- Jedes Werkzeug ist eingeordnet, jedes ändernde verlangt das Feld ----------------------


def test_jedes_werkzeug_ist_eingeordnet():
    katalog = {w["name"] for w in mcp.werkzeugliste()}
    assert katalog == LESEN | ARBEIT | FRAGE | AENDERND
    gruppen = (LESEN, ARBEIT, FRAGE, AENDERND)
    assert sum(len(g) for g in gruppen) == len(katalog), "ein Werkzeug steht doppelt"


def test_katalog_verlangt_das_feld_genau_bei_aendernden():
    for w in mcp.werkzeugliste():
        schema = w["inputSchema"]
        if w["name"] in AENDERND:
            assert "fred_sagte" in schema["required"], w["name"]
            assert "fred_sagte" in schema["properties"], w["name"]
        else:
            assert "fred_sagte" not in schema["required"], w["name"]
            assert "fred_sagte" not in schema["properties"], w["name"]


def test_jede_aendernde_route_ist_eingeordnet():
    """Auch wer die Schnittstelle ohne MCP ruft, kommt nicht vorbei; eine neue Route ohne
    Einordnung lässt diesen Test scheitern."""
    routen = {
        p.callback.__name__: p.callback
        for p in get_resolver("steuerung.urls").url_patterns
        if getattr(p.callback, "aendert", False)
    }
    ohne = {name for name, sicht in routen.items() if not getattr(sicht, "fred_noetig", False)}
    assert ohne == {
        "uebernehmen",
        "fortschritt",
        "hindernis",
        "rueckfrage",
        "fehler",
        "ergebnis",
        "entscheidung_fragen",
    }
    assert len(routen) == len(AENDERND) + len(ARBEIT) + len(FRAGE)


@pytest.mark.parametrize("werkzeug", sorted(AENDERND))
def test_ohne_das_feld_wird_nichts_geaendert(client, pc_zugang, projekt, bestand, werkzeug):
    vorher = _abbild()
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        werkzeug,
        projekt=projekt.kennung,
        fred_sagte=None,
        **bestand[werkzeug],
    )
    assert fehler and "Frag Fred zuerst" in str(daten), daten
    assert _abbild() == vorher


@pytest.mark.parametrize("werkzeug", sorted(AENDERND))
def test_mit_dem_feld_gilt_es_und_die_worte_stehen_im_verlauf(
    client, pc_zugang, projekt, bestand, werkzeug
):
    """Gegenprobe: Derselbe Aufruf gelingt mit dem Feld, sonst bewiese der Test oben nichts."""
    letztes = Ereignis.objects.order_by("-id").first().pk
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        werkzeug,
        projekt=projekt.kennung,
        fred_sagte=WORTE,
        **bestand[werkzeug],
    )
    assert not fehler, daten
    neu = Ereignis.objects.filter(pk__gt=letztes)
    assert neu.exists(), werkzeug
    assert all(f"Fred sagte: „{WORTE}“" in e.anlass for e in neu), [e.anlass for e in neu]


@pytest.mark.parametrize("wert", ["", "   ", "\n\t", 5, True, ["Ja"]])
def test_leer_oder_kein_text_gilt_als_fehlend(client, pc_zugang, projekt, wert):
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Nicht anlegen",
        text="b",
        fred_sagte=wert,
    )
    assert fehler and "Frag Fred zuerst" in str(daten)
    assert not Auftrag.objects.filter(titel="Nicht anlegen").exists()


def test_zu_lange_worte_werden_abgewiesen(client, pc_zugang, projekt):
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Nicht anlegen",
        text="b",
        fred_sagte="x" * (api.FRED_SAGTE_LAENGE + 1),
    )
    assert fehler and "zu lang" in str(daten)
    assert not Auftrag.objects.filter(titel="Nicht anlegen").exists()


def test_projektschluessel_braucht_das_feld_ebenso(client, projekt, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_eintragen",
        entscheidung="Das nicht.",
        fred_sagte=None,
    )
    assert fehler and "Frag Fred zuerst" in str(daten)


def test_direkter_aufruf_ohne_mcp_wird_abgewiesen(projekt, bindung):
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    antwort = c.post(
        "/api/auftraege/anlegen/",
        data=json.dumps({"titel": "Nicht anlegen", "beschreibung": "b", "anfrage_kennung": "d1"}),
        content_type="application/json",
    )
    assert antwort.status_code == 400 and "Frag Fred zuerst" in antwort.json()["fehler"]
    assert not Auftrag.objects.filter(titel="Nicht anlegen").exists()


def test_schluessel_in_den_worten_landet_nicht_im_verlauf(client, projekt, bindung):
    schluessel = "ghp_" + "a" * 30
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_eintragen",
        entscheidung="Es gilt.",
        fred_sagte=f"Ja, mein Schlüssel ist {schluessel}",
    )
    assert not fehler, daten
    ereignis = Ereignis.objects.order_by("-id").first()
    assert schluessel not in ereignis.anlass and "[Token entfernt]" in ereignis.anlass


# --- Das Feld stört nichts anderes ---------------------------------------------------------


def test_feld_allein_hebt_die_freigabe_nicht_auf(client, freigegeben, pc_zugang):
    """planen wertet jedes unbekannte Feld als Planänderung; dieses darf keines sein."""
    revision = freigegeben.revision_nr
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        projekt=freigegeben.projekt.kennung,
        auftrag=freigegeben.nummer,
        fred_sagte=WORTE,
    )
    assert not fehler, daten
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.FREIGEGEBEN and freigegeben.revision_nr == revision


def test_termin_mit_feld_hebt_die_freigabe_nicht_auf(client, freigegeben, bindung):
    revision = freigegeben.revision_nr
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_planen",
        auftrag=freigegeben.nummer,
        faellig="2026-12-24",
        fred_sagte=WORTE,
    )
    assert not fehler, daten
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.FREIGEGEBEN and freigegeben.revision_nr == revision
    assert (
        f"Fred sagte: „{WORTE}“"
        in Ereignis.objects.get(ereignis="Fällig am 24.12.2026 gesetzt").anlass
    )


def test_ablehnung_verbraucht_die_kennung_nicht(client, pc_zugang, projekt):
    """Der Chat fragt nach der Ablehnung und sendet denselben Aufruf mit Freds Worten erneut."""
    argumente = {
        "projekt": projekt.kennung,
        "titel": "Einmal",
        "text": "b",
        "anfrage_kennung": "lauf-1",
    }
    fehler, _ = _werkzeug(
        client, pc_zugang.klartext, "kipm_auftrag_anlegen", fred_sagte=None, **argumente
    )
    assert fehler and not AnfrageKennung.objects.filter(kennung="lauf-1").exists()
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_auftrag_anlegen", fred_sagte=WORTE, **argumente
    )
    assert not fehler, daten
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_auftrag_anlegen", fred_sagte=WORTE, **argumente
    )
    assert not fehler and daten["wiederholt"] is True
    assert Auftrag.objects.filter(titel="Einmal").count() == 1


# --- Ausnahmen -----------------------------------------------------------------------------


def test_lesen_braucht_das_feld_nicht(client, pc_zugang, projekt, freigegeben):
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_text_anlegen",
        projekt=projekt.kennung,
        titel="Notiz",
        text="Inhalt",
        fred_sagte=WORTE,
    )
    assert not fehler, daten
    argumente = {
        "kipm_lesen": {"auftrag": freigegeben.nummer},
        "kipm_unterlage_lesen": {"unterlage": daten["unterlage"]["kennung"]},
    }
    for werkzeug in sorted(LESEN):
        zusatz = argumente.get(werkzeug, {})
        fehler, daten = _werkzeug(
            client,
            pc_zugang.klartext,
            werkzeug,
            projekt=projekt.kennung,
            fred_sagte=None,
            **zusatz,
        )
        assert not fehler, (werkzeug, daten)


def test_arbeit_am_freigegebenen_auftrag_braucht_das_feld_nicht(client, freigegeben, bindung):
    a = freigegeben
    r = a.revision_nr

    def rufe(werkzeug, **argumente):
        fehler, daten = _werkzeug(
            client, bindung.klartext, werkzeug, auftrag=a.nummer, fred_sagte=None, **argumente
        )
        assert not fehler, (werkzeug, daten)

    rufe("kipm_uebernehmen", revision=r)
    rufe("kipm_fortschritt", revision=r, text="Halb fertig")
    rufe("kipm_rueckfrage", revision=r, text="Welche Sprache?")
    dienst.rueckfrage_beantworten(dienst.Akteur.aus_benutzer(_owner()), a, "Deutsch")
    rufe("kipm_hindernis", revision=r, text="Prüfserver fehlt")
    dienst.rueckfrage_beantworten(dienst.Akteur.aus_benutzer(_owner()), a, "Ist wieder da")
    rufe("kipm_fehler", revision=r, text="Abbruch")
    dienst.rueckfrage_beantworten(dienst.Akteur.aus_benutzer(_owner()), a, "Nochmal")
    rufe(
        "kipm_ergebnis",
        revision=r,
        zusammenfassung="Fertig",
        dateien=["a.py@abc1234"],
        pruefungen=["pytest: bestanden"],
    )
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT
    assert (
        not Ereignis.objects.filter(objekt_kennung=a.nummer, anlass__contains="Fred sagte")
        .exclude(ereignis__contains="Freigegeben")
        .exists()
    )


def _owner():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(is_superuser=True).first()


def test_fred_von_hand_braucht_das_feld_nicht(owner_client, projekt):
    """Die Oberfläche ist Fred selbst; dort gibt es nichts zu fragen."""
    antwort = owner_client.post(
        reverse("modul_neu", kwargs={"kennung": projekt.kennung}),
        {"modul-name": "Von Hand"},
        follow=True,
    )
    assert antwort.status_code == 200
    assert Modul.objects.filter(projekt=projekt, name="Von Hand").exists()
    assert "Fred sagte" not in str(Ereignis.objects.order_by("-id").first().anlass)


# --- Anweisungen an die Chats --------------------------------------------------------------


def test_anweisungen_fordern_das_fragen():
    text = mcp.ANWEISUNGEN
    assert "Frag Fred, bevor du" in text and "fred_sagte" in text
    assert "Erfinde sie nie" in text
    assert "direkt" not in text
    assert "trägst du Offenes" not in text


def test_kein_werkzeug_verspricht_direktes_aendern():
    for name, beschreibung, *_ in mcp.WERKZEUGE:
        if name == "kipm_modul_aendern":  # „direkt unter das Projekt“ meint den Modulbaum
            continue
        assert "direkt" not in beschreibung, name


def test_kontext_nennt_die_regel(client, bindung, projekt):
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_projektkontext", fred_sagte=None)
    assert not fehler
    assert "fred_sagte" in json.dumps(daten, ensure_ascii=False)

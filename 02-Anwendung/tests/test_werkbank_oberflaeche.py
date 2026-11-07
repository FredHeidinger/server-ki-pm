"""Werkbank-Anbindung WP8: Oberfläche (Verwaltung, Zuweisung am Auftrag, Bauplan, Aktionen)."""

import pytest
from django.urls import reverse

from steuerung import dienst, werkbank
from steuerung.models import (
    Auftrag,
    WerkbankAktion,
    WerkbankInstanz,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)
from tests.conftest import arbeitspaket
from tests.werkbank_hilfen import ereignis_nachricht, senden

pytestmark = pytest.mark.django_db


def seite(client, name, **kw):
    antwort = client.get(reverse(name, kwargs=kw))
    assert antwort.status_code == 200
    return antwort.content.decode()


def folgen(client, name, daten=None, **kw):
    antwort = client.post(reverse(name, kwargs=kw), daten or {}, follow=True)
    assert antwort.status_code == 200
    return antwort.content.decode()


def ereignis(client, ausf, instanz, art, daten=None):
    n = ereignis_nachricht(ausf, instanz, art, daten, sequenz=ausf.ereignisse.count() + 1)
    assert senden(client, instanz, "ereignisse/", n).status_code == 200
    ausf.refresh_from_db()


# --- Verwaltung ------------------------------------------------------------------------------


def test_verwaltung_legt_eine_werkbank_an_und_zeigt_den_schluessel_einmal(owner_client, projekt):
    html = seite(owner_client, "verbindungen")
    assert "KI-Werkbänke" in html and "Neue Werkbank anlegen" in html
    antwort = owner_client.post(
        reverse("werkbank_anlegen"),
        {"name": "Werkbank Büro", "projekt": [projekt.pk], "tage": "30"},
        follow=True,
    )
    html = antwort.content.decode()
    instanz = WerkbankInstanz.objects.get()
    assert instanz.name == "Werkbank Büro" and list(instanz.erlaubte_projekte.all()) == [projekt]
    assert "kipm_" in html and "/api/werkbank/v1/" in html
    zweites = seite(owner_client, "verbindungen")
    assert "werkbank-schluessel" not in zweites and "Werkbank Büro" in zweites


def test_verwaltung_prueft_name_und_projekt(owner_client, projekt):
    html = owner_client.post(
        reverse("werkbank_anlegen"), {"name": " ", "projekt": [projekt.pk]}, follow=True
    ).content.decode()
    assert "darf nicht leer sein" in html and not WerkbankInstanz.objects.exists()
    html = owner_client.post(
        reverse("werkbank_anlegen"), {"name": "X"}, follow=True
    ).content.decode()
    assert "Mindestens ein Projekt" in html


def test_verwaltung_pflegen(owner_client, akteur, projekt, projekt_b):
    inst, _ = werkbank.instanz_anlegen(akteur, name="W1", projekte=[projekt])
    folgen(owner_client, "werkbank_pflegen", {"aktiv": "0"}, instanz_id=inst.pk, wahl="aktiv")
    inst.refresh_from_db()
    assert not inst.aktiv
    folgen(
        owner_client, "werkbank_pflegen", {"projekt": [projekt_b.pk]},
        instanz_id=inst.pk, wahl="projekte",
    )  # fmt: skip
    assert list(inst.erlaubte_projekte.all()) == [projekt_b]
    html = folgen(owner_client, "werkbank_pflegen", {}, instanz_id=inst.pk, wahl="loeschen")
    assert "ist gelöscht" in html and not WerkbankInstanz.objects.exists()


def test_benutzte_werkbank_wird_widerrufen_nicht_geloescht(owner_client, akteur, projekt):
    inst, _ = werkbank.instanz_anlegen(akteur, name="W1", projekte=[projekt])
    WerkbankInstanz.objects.filter(pk=inst.pk).update(letzter_kontakt=inst.bindung.angelegt_am)
    html = folgen(owner_client, "werkbank_pflegen", {}, instanz_id=inst.pk, wahl="loeschen")
    assert "bleibt für den Verlauf" in html and WerkbankInstanz.objects.exists()
    html = folgen(owner_client, "werkbank_pflegen", {}, instanz_id=inst.pk, wahl="widerrufen")
    assert "widerrufen" in html


def test_bereitschaft_und_gruende_werden_angezeigt(owner_client, akteur, projekt, w_pbindung):
    WerkbankProjektbindung.objects.filter(pk=w_pbindung.pk).update(
        bereit=False,
        bereit_gruende=["Prüfprofil fehlt"],
        bereit_gemeldet_am=w_pbindung.instanz.bindung.angelegt_am,
    )
    html = seite(owner_client, "verbindungen")
    assert "nicht bereit" in html and "Prüfprofil fehlt" in html


def test_nicht_owner_darf_nichts_aendern(client, zweiter_mensch, akteur, projekt):
    inst, _ = werkbank.instanz_anlegen(akteur, name="W1", projekte=[projekt])
    client.force_login(zweiter_mensch)
    html = client.post(
        reverse("werkbank_pflegen", kwargs={"instanz_id": inst.pk, "wahl": "loeschen"}), follow=True
    ).content.decode()
    assert "nur der Owner" in html and WerkbankInstanz.objects.exists()


# --- Auftrag ---------------------------------------------------------------------------------


def test_auftrag_zeigt_ziel_und_freigeben_und_zuweisen(owner_client, akteur, projekt, w_pbindung):
    a = arbeitspaket(akteur, projekt)
    html = seite(owner_client, "auftrag_detail", auftrag_id=a.pk)
    assert "Ausführungsweg:" in html and "Freigeben und Werkbank zuweisen" in html
    html = folgen(
        owner_client, "auftrag_zuweisen", {"ziel": w_pbindung.pk, "freigeben": "1"}, auftrag_id=a.pk
    )
    a.refresh_from_db()
    assert a.zustand == Auftrag.FREIGEGEBEN and a.ausfuehrungsweg == Auftrag.WEG_WERKBANK
    assert "wartet auf die Werkbank" in html and "Werkbank Werkbank PC" in html
    assert "Ich bearbeite das selbst – nicht möglich" in html


def test_zuweisen_ohne_wahl_und_aufheben(owner_client, akteur, projekt, w_pbindung):
    a = arbeitspaket(akteur, projekt)
    assert "Bitte eine Werkbank wählen" in folgen(
        owner_client, "auftrag_zuweisen", {}, auftrag_id=a.pk
    )
    folgen(owner_client, "auftrag_zuweisen", {"ziel": w_pbindung.pk}, auftrag_id=a.pk)
    assert WerkbankZuweisung.objects.filter(auftrag=a).exists()
    folgen(owner_client, "auftrag_zuweisen", {"wahl": "aufheben"}, auftrag_id=a.pk)
    a.refresh_from_db()
    assert not WerkbankZuweisung.objects.exists() and a.ausfuehrungsweg == Auftrag.WEG_CHAT


def test_ohne_werkbank_zeigt_ein_auftrag_keinen_abschnitt(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    assert "Ausführungsweg" not in seite(owner_client, "auftrag_detail", auftrag_id=a.pk)


def test_laufende_ausfuehrung_bauplan_zaehlung_und_zustandswort(
    client, owner_client, w_ausfuehrung, w_instanz
):
    ereignis(
        client, w_ausfuehrung, w_instanz, "plan_bestaetigt",
        {"planfassung": "b" * 64, "pakete": [
            {"paket": "p1", "titel": "Erstes", "zustand": "offen"},
            {"paket": "p2", "titel": "Zweites", "zustand": "offen"},
        ]},
    )  # fmt: skip
    ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"}
    )
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "1 von 2 Paketen erledigt" in html and "Bauplan (2 Pakete)" in html
    assert "Erstes" in html and "Zweites" in html
    assert "Bearbeitung ändern" in html and "heißt noch nicht" in html


def test_rueckfrage_wird_beantwortet_und_bleibt_gesendet(
    client, owner_client, akteur, w_ausfuehrung, w_instanz, projekt
):
    ereignis(
        client, w_ausfuehrung, w_instanz, "rueckfrage",
        {"frage_id": "q1", "paket": "root", "text": "Variante A oder B?"},
    )  # fmt: skip
    a = w_ausfuehrung.auftrag
    html = seite(owner_client, "auftrag_detail", auftrag_id=a.pk)
    assert "Variante A oder B?" in html and "Antwort senden" in html
    assert "Werkbank wartet auf dich" in seite(
        owner_client, "projekt_detail", kennung=projekt.kennung
    )
    html = folgen(
        owner_client, "ausfuehrung_aktion",
        {"art": "antworten", "frage_id": "q1", "text": "Variante A", "erwartet": "wartet_antwort"},
        ausfuehrung_id=w_ausfuehrung.pk,
    )  # fmt: skip
    assert "Gesendet. Die Werkbank bestätigt" in html
    assert "gesendet, noch nicht angewendet" in html
    assert WerkbankAktion.objects.get().ziel["text"] == "Variante A"


def test_veraltete_oberflaeche_wird_mit_klartext_abgewiesen(
    client, owner_client, w_ausfuehrung, w_instanz
):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "rueckfrage",
        {"frage_id": "q1", "paket": "root", "text": "?"},
    )
    html = folgen(
        owner_client, "ausfuehrung_aktion",
        {"art": "antworten", "frage_id": "q1", "text": "A", "erwartet": "laeuft"},
        ausfuehrung_id=w_ausfuehrung.pk,
    )  # fmt: skip
    assert "Der Stand hat sich geändert" in html and not WerkbankAktion.objects.exists()


def test_abbruch_zeigt_angefordert_nicht_bestaetigt(client, owner_client, w_ausfuehrung, w_instanz):
    html = folgen(
        owner_client, "ausfuehrung_aktion", {"art": "abbrechen", "erwartet": "plant"},
        ausfuehrung_id=w_ausfuehrung.pk,
    )  # fmt: skip
    assert "Abbruch angefordert, noch nicht bestätigt" in html
    assert "Abbrechen – nicht möglich" in html
    ereignis(client, w_ausfuehrung, w_instanz, "abbruch_bestaetigt", {})
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "Abbruch bestätigt" in html


def test_alte_aktionen_sind_bei_laufender_werkbank_abgeschaltet(owner_client, w_ausfuehrung):
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "Läuft auf einer Werkbank" in html
    assert "In Arbeit auf einer KI-Werkbank" in html


def test_seiten_ohne_javascript_bedienbar(owner_client, w_ausfuehrung):
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    verwaltung = seite(owner_client, "verbindungen")
    assert "onclick" not in html and "onclick" not in verwaltung
    assert "<form" in html


def test_chat_auftrag_sieht_unveraendert_aus(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    html = seite(owner_client, "auftrag_detail", auftrag_id=a.pk)
    assert (
        "Ich bearbeite das selbst" in html and "nicht möglich" not in html.split("selbst")[1][:80]
    )


# --- Einrichtungswerte für die Werkbank ----------------------------------------------------------


def _angelegt(owner_client, projekt):
    owner_client.post(
        reverse("werkbank_anlegen"), {"name": "Büro", "projekt": [projekt.pk]}, follow=True
    )
    return WerkbankInstanz.objects.get()


def test_einrichtungswerte_stehen_mit_zuordnung_bei_der_werkbank(owner_client, projekt):
    instanz = _angelegt(owner_client, projekt)
    bindung = instanz.projekte.get()
    html = seite(owner_client, "verbindungen")
    assert "Einrichtungswerte" in html
    for feld in (
        "Serveradresse von KI-PM",
        "Kennung des Servers",
        "Kennung dieser Werkbank",
        "Kennung der Projektbindung",
        "Version der Projektbindung",
        "Projekt in KI-PM",
        "In KI-PM bestätigter Arbeitsort",
    ):
        assert feld in html, feld
    assert str(werkbank.server_id()) in html
    assert str(instanz.werkbank_id) in html
    assert f'id="wert-{instanz.pk}-bindung-{bindung.pk}-nummer"' in html
    assert f'id="wert-{instanz.pk}-bindung-{bindung.pk}-projekt"' in html
    assert projekt.kennung in html


def test_einrichtungswerte_zeigen_den_schluessel_nie_wieder(owner_client, projekt):
    antwort = owner_client.post(
        reverse("werkbank_anlegen"), {"name": "Büro", "projekt": [projekt.pk]}, follow=True
    )
    assert "werkbank-schluessel" in antwort.content.decode()
    spaeter = seite(owner_client, "verbindungen")
    assert "werkbank-schluessel" not in spaeter and "Einrichtungswerte" in spaeter
    assert "nur beim Anlegen" in spaeter


def test_einrichtungswerte_warnen_ohne_bestaetigten_arbeitsort(owner_client, projekt):
    projekt.arbeitsorte.update(bestaetigt=False)
    _angelegt(owner_client, projekt)
    html = seite(owner_client, "verbindungen")
    assert "Kein bestätigter Arbeitsort" in html


def test_einrichtungswerte_fehlen_bei_widerrufenem_zugang(owner_client, akteur, projekt):
    inst, _ = werkbank.instanz_anlegen(akteur, name="Alt", projekte=[projekt])
    werkbank.instanz_widerrufen(akteur, inst, "")
    html = seite(owner_client, "verbindungen")
    assert "Zugang widerrufen" in html and str(inst.werkbank_id) not in html

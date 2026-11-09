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


def test_freigegebener_chat_auftrag_sagt_warum_die_werkbank_ihn_nicht_sieht(
    owner_client, akteur, projekt, w_pbindung
):
    """Befund KW-1: im Chat freigegeben, aber nie zugewiesen; der Abgleich liefert ihn nicht."""
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    html = seite(owner_client, "auftrag_detail", auftrag_id=a.pk)
    assert "Eine Werkbank sieht diesen Auftrag erst nach der Zuweisung" in html
    assert "Werkbank zuweisen – bleibt freigegeben" in html
    assert "Nur zuweisen" not in html  # hätte die Freigabe still zurückgenommen


def test_zuweisen_eines_freigegebenen_auftrags_laesst_ihn_freigegeben(
    owner_client, akteur, projekt, w_pbindung
):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    html = folgen(
        owner_client, "auftrag_zuweisen", {"ziel": w_pbindung.pk, "freigeben": "1"}, auftrag_id=a.pk
    )
    a.refresh_from_db()
    z = WerkbankZuweisung.objects.get(auftrag=a)
    assert a.zustand == Auftrag.FREIGEGEBEN and len(z.kontext_sha256) == 64
    assert "wartet auf die Werkbank" in html
    assert "Eine Werkbank sieht diesen Auftrag erst nach der Zuweisung" not in html
    assert "Ziel wechseln – bleibt freigegeben" in html


def test_bereitschaft_vor_der_ersten_uebernahme_heisst_nicht_gemeldet(
    owner_client, akteur, projekt, w_pbindung
):
    a = arbeitspaket(akteur, projekt)
    werkbank.zuweisen(akteur, a, w_pbindung)
    html = seite(owner_client, "auftrag_detail", auftrag_id=a.pk)
    assert "(nicht bereit)" not in html and "(Bereitschaft noch nicht gemeldet)" in html
    assert "meldet sie bei der ersten Übernahme" in html
    assert "meldet sie bei der ersten Übernahme" in seite(owner_client, "verbindungen")


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
    projekt.arbeitsorte.all().delete()
    _angelegt(owner_client, projekt)
    html = seite(owner_client, "verbindungen")
    assert "Kein bestätigter Arbeitsort" in html


def test_einrichtungswerte_fehlen_bei_widerrufenem_zugang(owner_client, akteur, projekt):
    inst, _ = werkbank.instanz_anlegen(akteur, name="Alt", projekte=[projekt])
    werkbank.instanz_widerrufen(akteur, inst, "")
    html = seite(owner_client, "verbindungen")
    assert "Zugang widerrufen" in html and str(inst.werkbank_id) not in html


def test_einrichtungswerte_folgen_dem_spaeter_bestaetigten_arbeitsort(owner_client, projekt):
    projekt.arbeitsorte.update(bestaetigt=False)
    _angelegt(owner_client, projekt)
    assert "noch nicht bestätigt" in seite(owner_client, "verbindungen")
    projekt.arbeitsorte.update(bestaetigt=True, pfad=r"C:\Projekte\Spaeter")
    html = seite(owner_client, "verbindungen")
    assert r"C:\Projekte\Spaeter" in html and "noch nicht bestätigt" not in html


def test_einrichtungswerte_sagen_wenn_der_arbeitsort_nur_unbestaetigt_ist(owner_client, projekt):
    projekt.arbeitsorte.update(bestaetigt=False, pfad=r"C:\Projekte\Offen")
    _angelegt(owner_client, projekt)
    html = seite(owner_client, "verbindungen")
    assert "noch nicht bestätigt" in html


# --- Kopplungscode: ein Text statt sieben Felder ---------------------------------------------------


def _code_lesen(text):
    import base64
    import json

    assert text.startswith("kipm-kopplung/1:")
    roh = text.split(":", 1)[1]
    return json.loads(base64.urlsafe_b64decode(roh + "=" * (-len(roh) % 4)).decode("utf-8"))


def test_kopplungscode_traegt_alle_werte(akteur, projekt):
    projekt.arbeitsorte.update(pfad=r"C:\Projekte\Demo")
    inst, schluessel = werkbank.instanz_anlegen(akteur, name="W", projekte=[projekt])
    daten = _code_lesen(
        werkbank.kopplungscode(inst, schluessel, "https://kipm.invalid/api/werkbank/v1/")
    )
    bindung = inst.projekte.get()
    assert daten["adresse"] == "https://kipm.invalid/api/werkbank/v1/"
    assert daten["schluessel"] == schluessel
    assert daten["server_id"] == str(werkbank.server_id())
    assert daten["werkbank_id"] == str(inst.werkbank_id)
    assert daten["projekte"] == [
        {
            "projektbindung_id": bindung.pk,
            "projektbindung_version": bindung.version,
            "projekt": projekt.kennung,
            "arbeitsort": r"C:\Projekte\Demo",
        }
    ]


def test_kopplungscode_arbeitsort_ist_leer_ohne_bestaetigten_ort(akteur, projekt):
    projekt.arbeitsorte.update(bestaetigt=False, pfad=r"C:\X")
    inst, schluessel = werkbank.instanz_anlegen(akteur, name="W", projekte=[projekt])
    daten = _code_lesen(werkbank.kopplungscode(inst, schluessel, "https://a/"))
    assert daten["projekte"][0]["arbeitsort"] is None


def test_kopplungscode_steht_nur_beim_anlegen_da(owner_client, projekt):
    html = owner_client.post(
        reverse("werkbank_anlegen"), {"name": "Büro", "projekt": [projekt.pk]}, follow=True
    ).content.decode()
    assert 'id="werkbank-kopplungscode"' in html and "Kopplungscode" in html
    code = html.split('id="werkbank-kopplungscode">', 1)[1].split("</", 1)[0].strip()
    daten = _code_lesen(code)
    assert daten["werkbank_id"] == str(WerkbankInstanz.objects.get().werkbank_id)
    spaeter = seite(owner_client, "verbindungen")
    # Der Code mit Schlüssel erscheint nur jetzt; später stehen nur Codes je Projektbindung
    # ohne Schlüssel da (Testlauf KTL-1, P15).
    assert "werkbank-kopplungscode" not in spaeter
    for teil in spaeter.split("kipm-kopplung/1:")[1:]:
        assert _code_lesen("kipm-kopplung/1:" + teil.split("<", 1)[0].strip())["schluessel"] is None


# --- Testlauf KTL-1: Versuche im Bauplan, letzter Fehlergrund, Code je Projektbindung --------


def _bauplan(html):
    return html.split("<summary>Bauplan", 1)[1].split("</details>", 1)[0]


def _ktl1_verlauf(client, ausf, inst):
    """Wie KTL-1: ein Planpaket, zwei Versuche unter eigenen Namen, die nach einem erfolgreichen
    Vorschritt „laeuft“ melden und danach blockieren; die Werkbank meldet nur den Abschlussfehler."""
    ereignis(
        client, ausf, inst, "plan_bestaetigt",
        {"planfassung": "c" * 64, "pakete": [
            {"paket": "fahrenheit-umrechnung", "titel": "Umrechnung", "zustand": "offen"},
        ]},
    )  # fmt: skip
    for nr in (1, 2):
        ereignis(
            client, ausf, inst, "paket_status",
            {"paket": f"fahrenheit-umrechnung-A-20261109-ab12cd-v{nr}", "zustand": "laeuft",
             "versuche": nr},
        )  # fmt: skip


def test_versuche_gehoeren_zum_paket_und_blockierte_stehen_als_blockiert(
    client, owner_client, w_ausfuehrung, w_instanz
):
    _ktl1_verlauf(client, w_ausfuehrung, w_instanz)
    ereignis(
        client, w_ausfuehrung, w_instanz, "fehler",
        {"code": "ABSCHLUSS_UNVOLLSTAENDIG", "ausgang_unklar": False,
         "text": "Blattpaket fahrenheit-umrechnung ist nicht übernommen."},
    )  # fmt: skip
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "0 von 1 Paketen erledigt" in html and "Bauplan (1 Paket)" in html
    plan = _bauplan(html)
    assert "Versuch 1" in plan and "Versuch 2" in plan
    assert "fahrenheit-umrechnung-A-20261109-ab12cd-v2" in plan
    assert "blockiert" in plan and "läuft" not in plan and "laeuft" not in plan


def test_laufender_versuch_ohne_fehler_bleibt_laufend(
    client, owner_client, w_ausfuehrung, w_instanz
):
    _ktl1_verlauf(client, w_ausfuehrung, w_instanz)
    plan = _bauplan(seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id))
    # Der neueste Versuch läuft; der ältere ist durch ihn abgelöst und läuft nicht mehr.
    assert plan.count("läuft") == 2  # Planpaket und Versuch 2
    assert "abgelöst" in plan


def test_letzter_fehlergrund_steht_am_paket_auch_nach_weiterbauen(
    client, owner_client, w_ausfuehrung, w_instanz
):
    _ktl1_verlauf(client, w_ausfuehrung, w_instanz)
    ereignis(
        client, w_ausfuehrung, w_instanz, "fehler",
        {"paket": "fahrenheit-umrechnung-A-20261109-ab12cd-v2", "code": "SCHRITT_FEHLGESCHLAGEN",
         "ausgang_unklar": False, "text": "Mindestens ein Prüfbefehl ist erforderlich."},
    )  # fmt: skip
    folgen(
        owner_client, "ausfuehrung_aktion",
        {"art": "weiterbauen", "erwartet": w_ausfuehrung.zustand},
        ausfuehrung_id=w_ausfuehrung.pk,
    )  # fmt: skip
    aktion = WerkbankAktion.objects.get(art="weiterbauen")
    ereignis(
        client, w_ausfuehrung, w_instanz, "aktion_angewendet",
        {"aktion_id": str(aktion.aktion_id), "ergebnis": "angewendet"},
    )  # fmt: skip
    ereignis(
        client, w_ausfuehrung, w_instanz, "fehler",
        {"code": "ABSCHLUSS_UNVOLLSTAENDIG", "ausgang_unklar": False, "text": "Unvollständig."},
    )  # fmt: skip
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    oben = html.split("<summary>Bauplan", 1)[0]
    assert "Mindestens ein Prüfbefehl" not in oben  # der Bedarf ist geschlossen
    plan = _bauplan(html)
    assert "Letzter Fehler" in plan
    assert plan.count("Mindestens ein Prüfbefehl ist erforderlich.") == 2  # Paket und Versuch 2
    assert "SCHRITT_FEHLGESCHLAGEN" in plan


def test_kopplungscode_fuer_eine_projektbindung_ohne_schluessel(akteur, projekt, projekt_b):
    projekt_b.arbeitsorte.update(pfad=r"C:\Projekte\B", bestaetigt=True)
    inst, _ = werkbank.instanz_anlegen(akteur, name="W", projekte=[projekt, projekt_b])
    b = inst.projekte.get(projekt=projekt_b)
    daten = _code_lesen(werkbank.kopplungscode(inst, None, "https://a/", bindung=b))
    assert daten["schluessel"] is None
    assert daten["werkbank_id"] == str(inst.werkbank_id)
    assert daten["server_id"] == str(werkbank.server_id())
    assert daten["projekte"] == [
        {
            "projektbindung_id": b.pk,
            "projektbindung_version": b.version,
            "projekt": projekt_b.kennung,
            "arbeitsort": r"C:\Projekte\B",
        }
    ]


def test_neu_erlaubtes_projekt_bietet_einen_code_nur_fuer_diese_bindung(
    owner_client, akteur, projekt, projekt_b
):
    projekt_b.arbeitsorte.update(pfad=r"C:\Projekte\B", bestaetigt=True)
    inst, _ = werkbank.instanz_anlegen(akteur, name="W", projekte=[projekt])
    folgen(
        owner_client, "werkbank_pflegen", {"projekt": [projekt.pk, projekt_b.pk]},
        instanz_id=inst.pk, wahl="projekte",
    )  # fmt: skip
    html = seite(owner_client, "verbindungen")
    b = inst.projekte.get(projekt=projekt_b)
    kennung = f'id="code-{inst.pk}-bindung-{b.pk}"'
    assert kennung in html and "Kopplungscode für dieses Projekt" in html
    code = html.split(kennung + ">", 1)[1].split("</", 1)[0].strip()
    daten = _code_lesen(code)
    assert daten["schluessel"] is None and [p["projekt"] for p in daten["projekte"]] == [
        projekt_b.kennung
    ]
    # Kein Code mit Schlüssel auf der Seite: jeder gezeigte Code trägt schluessel = null.
    for teil in html.split("kipm-kopplung/1:")[1:]:
        roh = teil.split("<", 1)[0].strip()
        assert _code_lesen("kipm-kopplung/1:" + roh)["schluessel"] is None


# --- Testlauf KTL-1, B7: still verworfene Freigabe sichtbar machen ------------------------------


def _uebernahme_bitte(client, ausf, inst, **mehr):
    ereignis(
        client, ausf, inst, "freigabe_noetig",
        {"freigabe_id": "F-1", "art": "uebernahme", "pakete": ["p1-v1"], "grund": "x", **mehr},
    )  # fmt: skip


def test_uebernahmefreigabe_ohne_kandidaten_wird_angesagt(
    client, owner_client, w_ausfuehrung, w_instanz
):
    _uebernahme_bitte(client, w_ausfuehrung, w_instanz)
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "Die Werkbank hat keine Kandidaten genannt" in html


def test_uebernahmefreigabe_mit_kandidaten_ohne_warnung(
    client, owner_client, w_ausfuehrung, w_instanz
):
    _uebernahme_bitte(client, w_ausfuehrung, w_instanz, kandidaten=["k1"])
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "Die Werkbank hat keine Kandidaten genannt" not in html


def _ausstehende_freigabe(client, akteur, ausf, inst, vor_sekunden, kontakt_vor_sekunden):
    from datetime import timedelta

    from django.utils import timezone

    _uebernahme_bitte(client, ausf, inst)
    aktion = werkbank.aktion_erstellen(akteur, ausf, "uebernahme_freigeben", freigabe_id="F-1")
    jetzt = timezone.now()
    WerkbankAktion.objects.filter(pk=aktion.pk).update(
        erstellt_am=jetzt - timedelta(seconds=vor_sekunden)
    )
    WerkbankInstanz.objects.filter(pk=inst.pk).update(
        letzter_kontakt=jetzt - timedelta(seconds=kontakt_vor_sekunden)
    )


def test_unbeantwortete_aktion_trotz_abgleich_wird_gewarnt(
    client, owner_client, akteur, w_ausfuehrung, w_instanz
):
    _ausstehende_freigabe(client, akteur, w_ausfuehrung, w_instanz, 600, 5)
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "weder angewendet noch abgewiesen" in html


def test_frische_aktion_ist_noch_keine_warnung(
    client, owner_client, akteur, w_ausfuehrung, w_instanz
):
    _ausstehende_freigabe(client, akteur, w_ausfuehrung, w_instanz, 20, 5)
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "weder angewendet noch abgewiesen" not in html


def test_aktion_ohne_abgleich_seitdem_ist_keine_warnung(
    client, owner_client, akteur, w_ausfuehrung, w_instanz
):
    # Die Werkbank hat seit der Aktion nicht abgeglichen: das meldet schon die Erreichbarkeit.
    _ausstehende_freigabe(client, akteur, w_ausfuehrung, w_instanz, 600, 900)
    html = seite(owner_client, "auftrag_detail", auftrag_id=w_ausfuehrung.auftrag_id)
    assert "weder angewendet noch abgewiesen" not in html

"""KIPM-03 (KI-PM-Auftrag KIPM-26): Werkbank-Zugang – Gültigkeit sichtbar, erneuern ohne neue
Werkbank, Ablehnung mit Grund.

Paket: W:\\AIProgramming\\pakete\\KIPM-PAKETE-20261115.md. Freds Festlegung vom 15.11.2026:
Erneuern gilt nur für abgelaufene oder bald ablaufende, nicht für widerrufene Zugänge („nein,
wird bei bedarf neu erstellt“).
"""

import base64
import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import dienst, werkbank
from steuerung.models import ChatBindung, Ereignis, WerkbankInstanz, WerkbankProjektbindung
from tests.werkbank_hilfen import BASIS, KOPF

pytestmark = pytest.mark.django_db


def _seite(client, name, **kw) -> str:
    antwort = client.get(reverse(name, kwargs=kw))
    assert antwort.status_code == 200
    return antwort.content.decode()


def _karte(client, instanz) -> str:
    html = _seite(client, "verbindungen")
    return html.split(f'<p class="eintragtitel">{instanz.name}')[1].split("</li>")[0]


def _gueltig_bis(instanz, delta: timedelta) -> None:
    ChatBindung.objects.filter(pk=instanz.bindung_id).update(gueltig_bis=timezone.now() + delta)
    instanz.refresh_from_db()


def _abgleich(client, schluessel: str):
    return client.get(BASIS + "abgleich/", HTTP_AUTHORIZATION=f"Bearer {schluessel}", **KOPF)


def _code_lesen(text: str) -> dict:
    assert text.startswith("kipm-kopplung/1:")
    roh = text.split(":", 1)[1]
    return json.loads(base64.urlsafe_b64decode(roh + "=" * (-len(roh) % 4)).decode("utf-8"))


def _erneuern(client, instanz, **daten):
    return client.post(
        reverse("werkbank_pflegen", kwargs={"instanz_id": instanz.pk, "wahl": "erneuern"}),
        daten,
        follow=True,
    ).content.decode()


# --- Test 1: Gültigkeit auf der Karte, abgelaufen statt „keine Antwort“ ----------------------


def test_karte_zeigt_gueltig_bis(owner_client, w_instanz):
    bis = timezone.localtime(w_instanz.bindung.gueltig_bis).strftime("%d.%m.%Y")
    assert f"Zugang gültig bis {bis}" in _karte(owner_client, w_instanz)


def test_abgelaufen_zeigt_ablauf_statt_90_sekunden(owner_client, w_instanz):
    _gueltig_bis(w_instanz, timedelta(days=-1))
    WerkbankInstanz.objects.filter(pk=w_instanz.pk).update(
        letzter_kontakt=timezone.now() - timedelta(hours=2)
    )
    karte = _karte(owner_client, w_instanz)
    bis = timezone.localtime(w_instanz.bindung.gueltig_bis).strftime("%d.%m.%Y")
    assert f"Zugang abgelaufen am {bis} – die Werkbank wird abgewiesen" in karte
    assert "status bad" in karte.split("Zugang abgelaufen")[0].rsplit("<span", 1)[1]
    assert "keine Antwort seit mehr als 90 Sekunden" not in karte


def test_widerrufen_zeigt_datum_statt_90_sekunden(owner_client, akteur, w_instanz):
    WerkbankInstanz.objects.filter(pk=w_instanz.pk).update(
        letzter_kontakt=timezone.now() - timedelta(hours=2)
    )
    werkbank.instanz_widerrufen(akteur, w_instanz)
    karte = _karte(owner_client, w_instanz)
    heute = timezone.localdate().strftime("%d.%m.%Y")
    assert f"Zugang widerrufen am {heute}" in karte
    assert "keine Antwort seit mehr als 90 Sekunden" not in karte


# --- Test 2: 14 Tage vorher in „Wartet auf dich“ ---------------------------------------------


def test_vierzehn_tage_vorher_in_wartet_auf_dich(owner_client, w_instanz, projekt):
    _gueltig_bis(w_instanz, timedelta(days=10))
    bis = timezone.localtime(w_instanz.bindung.gueltig_bis).strftime("%d.%m.%Y")
    html = _seite(owner_client, "projekt_detail", kennung=projekt.kennung)
    wartet = html.split('id="wartet"')[1].split("</section>")[0]
    assert f"Zugang der Werkbank {w_instanz.name} läuft am {bis} ab – erneuern" in wartet
    assert reverse("verbindungen") + "#werkbank" in wartet
    karten = _seite(owner_client, "projekte")
    assert "1 wartet auf dich" in karten


def test_abgelaufen_steht_auch_in_wartet_auf_dich(owner_client, w_instanz, projekt):
    _gueltig_bis(w_instanz, timedelta(days=-2))
    bis = timezone.localtime(w_instanz.bindung.gueltig_bis).strftime("%d.%m.%Y")
    html = _seite(owner_client, "projekt_detail", kennung=projekt.kennung)
    assert f"Zugang der Werkbank {w_instanz.name} ist am {bis} abgelaufen – erneuern" in html


def test_noch_lange_gueltig_oder_widerrufen_nicht_in_wartet(
    owner_client, akteur, w_instanz, projekt
):
    _gueltig_bis(w_instanz, timedelta(days=20))
    html = _seite(owner_client, "projekt_detail", kennung=projekt.kennung)
    assert "Zugang der Werkbank" not in html
    _gueltig_bis(w_instanz, timedelta(days=3))
    werkbank.instanz_widerrufen(akteur, w_instanz)
    html = _seite(owner_client, "projekt_detail", kennung=projekt.kennung)
    assert "Zugang der Werkbank" not in html


def test_nur_in_erlaubten_projekten(owner_client, w_instanz, projekt_b):
    _gueltig_bis(w_instanz, timedelta(days=5))
    html = _seite(owner_client, "projekt_detail", kennung=projekt_b.kennung)
    assert "Zugang der Werkbank" not in html


# --- Test 3: Erneuern -----------------------------------------------------------------------


def test_erneuern_behaelt_instanz_und_bindungen(client, owner_client, w_instanz, projekt):
    _gueltig_bis(w_instanz, timedelta(days=-1))
    alter = w_instanz.klartext
    assert _abgleich(client, alter).status_code == 401
    vorher_bindungen = list(WerkbankProjektbindung.objects.filter(instanz=w_instanz).values("pk"))
    html = _erneuern(owner_client, w_instanz, tage="30", bestaetigt="1")
    code = html.split('id="werkbank-kopplungscode">', 1)[1].split("</", 1)[0].strip()
    daten = _code_lesen(code)
    neuer = daten["schluessel"]
    assert neuer and neuer != alter
    w_instanz.refresh_from_db()
    assert daten["werkbank_id"] == str(w_instanz.werkbank_id)
    assert daten["gueltig_bis"] == w_instanz.bindung.gueltig_bis.isoformat()
    rest = w_instanz.bindung.gueltig_bis - timezone.now()
    assert timedelta(days=29) < rest <= timedelta(days=30)
    assert WerkbankInstanz.objects.count() == 1
    assert list(w_instanz.erlaubte_projekte.all()) == [projekt]
    assert list(WerkbankProjektbindung.objects.filter(instanz=w_instanz).values("pk")) == (
        vorher_bindungen
    )
    assert _abgleich(client, alter).status_code == 401
    assert _abgleich(client, neuer).status_code == 200
    assert Ereignis.objects.filter(
        objekt_typ="Werkbank", objekt_kennung=w_instanz.name, ereignis="Werkbank-Zugang erneuert"
    ).exists()
    assert neuer not in _seite(owner_client, "verbindungen")  # Klartext nur einmal


def test_erneuern_braucht_bestaetigung(owner_client, w_instanz):
    vorher = w_instanz.bindung.schluessel_hash
    html = _erneuern(owner_client, w_instanz, tage="30")
    w_instanz.bindung.refresh_from_db()
    assert w_instanz.bindung.schluessel_hash == vorher
    assert "bestätigen" in html and "werkbank-kopplungscode" not in html


def test_erneuern_prueft_tage(akteur, w_instanz):
    for tage in (0, 366):
        with pytest.raises(dienst.DienstFehler, match="1 und 365"):
            werkbank.instanz_erneuern(akteur, w_instanz, gueltig_tage=tage)


def test_widerrufener_zugang_laesst_sich_nicht_erneuern(akteur, w_instanz):
    werkbank.instanz_widerrufen(akteur, w_instanz)
    w_instanz.refresh_from_db()
    with pytest.raises(dienst.DienstFehler, match="widerrufen"):
        werkbank.instanz_erneuern(akteur, w_instanz, gueltig_tage=30)


def test_kopplungscode_traegt_gueltig_bis(akteur, w_instanz):
    daten = _code_lesen(werkbank.kopplungscode(w_instanz, w_instanz.klartext, "https://a/"))
    assert daten["gueltig_bis"] == w_instanz.bindung.gueltig_bis.isoformat()
    ohne = _code_lesen(
        werkbank.kopplungscode(w_instanz, None, "https://a/", bindung=w_instanz.projekte.get())
    )
    assert ohne["schluessel"] is None and ohne["gueltig_bis"] == daten["gueltig_bis"]


# --- Test 4: Nur der Owner ------------------------------------------------------------------


def test_nur_owner_darf_erneuern(client, zweiter_mensch, akteur, w_instanz):
    vorher = w_instanz.bindung.schluessel_hash
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_erneuern(
            dienst.Akteur.aus_benutzer(zweiter_mensch), w_instanz, gueltig_tage=30
        )
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_erneuern(dienst.Akteur.aus_bindung(pc), w_instanz, gueltig_tage=30)
    client.force_login(zweiter_mensch)
    _erneuern(client, w_instanz, tage="30", bestaetigt="1")
    w_instanz.bindung.refresh_from_db()
    assert w_instanz.bindung.schluessel_hash == vorher


# --- Test 5: Erlaubtes Projekt mit beendeter Ausführung entfernen ----------------------------


def test_projekt_mit_beendeter_ausfuehrung_entfernen(
    akteur, w_instanz, w_ausfuehrung, projekt, projekt_b
):
    werkbank.instanz_projekte_setzen(akteur, w_instanz, [projekt, projekt_b])
    type(w_ausfuehrung).objects.filter(pk=w_ausfuehrung.pk).update(zustand="abgebrochen")
    werkbank.instanz_projekte_setzen(akteur, w_instanz, [projekt_b])
    assert list(w_instanz.erlaubte_projekte.all()) == [projekt_b]
    # Die Projektbindung bleibt für den Verlauf, die Ausführung hängt weiter an ihr.
    assert WerkbankProjektbindung.objects.filter(instanz=w_instanz, projekt=projekt).exists()
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.zustand == "abgebrochen"


# --- Test 6: Gegenprobe Chat-Zugänge --------------------------------------------------------


def test_chat_zugaenge_unveraendert(owner_client, akteur, bindung, w_instanz):
    vorher = ChatBindung.objects.filter(pk=bindung.pk).values().get()
    werkbank.instanz_erneuern(akteur, w_instanz, gueltig_tage=30)
    assert ChatBindung.objects.filter(pk=bindung.pk).values().get() == vorher
    assert dienst.bindung_finden(bindung.klartext) is not None
    tabelle = _seite(owner_client, "verbindungen").split('id="werkbank"')[0]
    assert "Zugang der Werkbank" not in tabelle and "Zugang erneuern" not in tabelle


# --- Zusatz (Freds Wunsch 15.11.2026): Werkbank je Projekt pausieren --------------------------
# „besser der pausierknopf ist in kipm ui also schaltet die verbindung einfach tot“


def _pause(client, instanz, projekt, wahl):
    return client.post(
        reverse("werkbank_pflegen", kwargs={"instanz_id": instanz.pk, "wahl": wahl}),
        {"projekt": projekt.pk},
        follow=True,
    ).content.decode()


def test_pausieren_schaltet_projekt_tot_und_fortsetzen_holt_es_zurueck(
    owner_client, akteur, w_instanz, projekt, projekt_b
):
    werkbank.instanz_projekte_setzen(akteur, w_instanz, [projekt, projekt_b])
    html = _pause(owner_client, w_instanz, projekt, "pausieren")
    assert not werkbank.projekt_erlaubt(w_instanz, projekt)
    assert werkbank.projekt_erlaubt(w_instanz, projekt_b)
    assert WerkbankProjektbindung.objects.filter(instanz=w_instanz, projekt=projekt).exists()
    karte = html.split(f'<p class="eintragtitel">{w_instanz.name}')[1].split("</li>")[0]
    zeile = karte.split(f"{projekt.name}: ")[1].split("</div>")[0]
    assert "pausiert" in zeile and "Fortsetzen" in zeile
    assert Ereignis.objects.filter(ereignis="Werkbank: Projekt pausiert").exists()
    _pause(owner_client, w_instanz, projekt, "fortsetzen")
    assert werkbank.projekt_erlaubt(w_instanz, projekt)
    assert Ereignis.objects.filter(ereignis="Werkbank: Projekt fortgesetzt").exists()


def test_letztes_projekt_laesst_sich_nicht_pausieren(owner_client, akteur, w_instanz, projekt):
    with pytest.raises(dienst.DienstFehler, match="Abschalten"):
        werkbank.projekt_pausieren(akteur, w_instanz, projekt, pausiert=True)
    karte = _karte(owner_client, w_instanz)
    zeile = karte.split(f"{projekt.name}: ")[1].split("</div>")[0]
    assert "disabled" in zeile and "Abschalten" in zeile  # abgeschaltet mit Grund


def test_pausieren_nicht_waehrend_einer_laufenden_ausfuehrung(
    akteur, w_instanz, w_ausfuehrung, projekt, projekt_b
):
    werkbank.instanz_projekte_setzen(akteur, w_instanz, [projekt, projekt_b])
    with pytest.raises(dienst.DienstFehler, match="läuft"):
        werkbank.projekt_pausieren(akteur, w_instanz, projekt, pausiert=True)
    assert werkbank.projekt_erlaubt(w_instanz, projekt)


def test_nur_owner_darf_pausieren(zweiter_mensch, akteur, w_instanz, projekt, projekt_b):
    werkbank.instanz_projekte_setzen(akteur, w_instanz, [projekt, projekt_b])
    with pytest.raises(dienst.DienstFehler):
        werkbank.projekt_pausieren(
            dienst.Akteur.aus_benutzer(zweiter_mensch), w_instanz, projekt, pausiert=True
        )
    assert werkbank.projekt_erlaubt(w_instanz, projekt)

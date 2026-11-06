"""Werkbank-Anbindung WP2: Instanzen und Bindungsart „werkbank“ im Dienst; Abweisung an Chat und MCP."""

import json
import uuid

import pytest
from django.urls import reverse

from steuerung import dienst, werkbank
from steuerung.models import ChatBindung, Ereignis, WerkbankInstanz

pytestmark = pytest.mark.django_db


@pytest.fixture
def zugang(akteur, projekt):
    instanz, schluessel = werkbank.instanz_anlegen(akteur, name="Werkbank PC", projekte=[projekt])
    instanz.klartext = schluessel
    return instanz


def test_server_id_ist_dauerhaft():
    assert isinstance(werkbank.server_id(), uuid.UUID)
    assert werkbank.server_id() == werkbank.server_id()


def test_anlegen_erzeugt_instanz_und_bindung_der_art_werkbank(akteur, projekt, zugang):
    b = zugang.bindung
    assert b.art == ChatBindung.ART_WERKBANK == "werkbank"
    assert b.projekt is None and b.aktiv
    assert zugang.aktiv and list(zugang.erlaubte_projekte.all()) == [projekt]
    assert zugang.klartext.startswith("kipm_") and zugang.klartext not in str(b.schluessel_hash)
    e = Ereignis.objects.filter(objekt_typ="Werkbank", ereignis="Werkbank angelegt").get()
    assert zugang.klartext not in e.anlass and e.akteur_name == akteur.name


def test_werkbank_ist_weder_owner_noch_assistent(zugang):
    a = dienst.Akteur.aus_bindung(zugang.bindung)
    assert not a.ist_owner and not a.ist_assistent
    with pytest.raises(dienst.KeinZugriff):
        dienst._planung_erforderlich(a, "Planen")


def test_nur_owner_verwaltet(akteur, projekt, zugang, bindung, pc_zugang_nicht_owner):
    for fremd in (dienst.Akteur.aus_bindung(bindung), pc_zugang_nicht_owner):
        with pytest.raises(dienst.KeinZugriff):
            werkbank.instanz_anlegen(fremd, name="X", projekte=[projekt])
        with pytest.raises(dienst.KeinZugriff):
            werkbank.instanz_projekte_setzen(fremd, zugang, [projekt])
        with pytest.raises(dienst.KeinZugriff):
            werkbank.instanz_widerrufen(fremd, zugang)
        with pytest.raises(dienst.KeinZugriff):
            werkbank.instanz_loeschen(fremd, zugang)
    assert WerkbankInstanz.objects.count() == 1


@pytest.fixture
def pc_zugang_nicht_owner(akteur):
    b, _ = dienst.bindung_anlegen(akteur, None, name="PC")
    return dienst.Akteur.aus_bindung(b)


def test_anlegen_prueft_name_projekte_und_gueltigkeit(akteur, projekt):
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_anlegen(akteur, name="  ", projekte=[projekt])
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_anlegen(akteur, name="A", projekte=[])
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_anlegen(akteur, name="A", projekte=[projekt], gueltig_tage=0)
    assert not WerkbankInstanz.objects.exists()
    assert not ChatBindung.objects.filter(art="werkbank").exists()


def test_archiviertes_projekt_nicht_erlaubbar(akteur, projekt):
    dienst.projekt_archivieren(akteur, projekt)
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_anlegen(akteur, name="A", projekte=[projekt])


def test_erlaubte_projekte_aendern(akteur, projekt, projekt_b, zugang):
    assert werkbank.projekt_erlaubt(zugang, projekt)
    assert not werkbank.projekt_erlaubt(zugang, projekt_b)
    werkbank.instanz_projekte_setzen(akteur, zugang, [projekt_b])
    assert not werkbank.projekt_erlaubt(zugang, projekt)
    assert werkbank.projekt_erlaubt(zugang, projekt_b)
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_projekte_setzen(akteur, zugang, [])
    assert Ereignis.objects.filter(ereignis="Werkbank: erlaubte Projekte geändert").count() == 1


def test_schluessel_findet_nur_aktive_instanz(akteur, zugang):
    assert werkbank.instanz_zu_schluessel(zugang.klartext) == zugang
    assert werkbank.instanz_zu_schluessel("kipm_x_y") is None
    assert werkbank.instanz_zu_schluessel("") is None
    werkbank.instanz_aktiv_setzen(akteur, zugang, False)
    assert werkbank.instanz_zu_schluessel(zugang.klartext) is None
    werkbank.instanz_aktiv_setzen(akteur, zugang, True)
    assert werkbank.instanz_zu_schluessel(zugang.klartext) == zugang


def test_widerruf_sperrt_den_schluessel(akteur, zugang):
    werkbank.instanz_widerrufen(akteur, zugang, "verloren")
    zugang.refresh_from_db()
    assert zugang.bindung.widerrufen_am is not None and not zugang.aktiv
    assert werkbank.instanz_zu_schluessel(zugang.klartext) is None
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_widerrufen(akteur, zugang)


def test_dienst_findet_werkbank_schluessel_nie_fuer_chat(zugang):
    assert dienst.bindung_finden(zugang.klartext) is None
    assert dienst.bindung_pruefen(zugang.klartext) is None
    zugang.bindung.refresh_from_db()
    assert zugang.bindung.zuletzt_aktiv is None


def test_nur_unbenutzte_instanz_loeschbar(akteur, zugang):
    assert not werkbank.instanz_benutzt(zugang)
    zugang.letzter_kontakt = zugang.bindung.angelegt_am
    zugang.save()
    assert werkbank.instanz_benutzt(zugang)
    with pytest.raises(dienst.DienstFehler):
        werkbank.instanz_loeschen(akteur, zugang)
    assert WerkbankInstanz.objects.count() == 1
    zugang.letzter_kontakt = None
    zugang.save()
    werkbank.instanz_loeschen(akteur, zugang)
    assert not WerkbankInstanz.objects.exists()
    assert not ChatBindung.objects.filter(art="werkbank").exists()
    assert Ereignis.objects.filter(ereignis="Werkbank gelöscht").exists()


def test_generisches_loeschen_der_bindung_folgt_denselben_regeln(akteur, zugang):
    WerkbankInstanz.objects.filter(pk=zugang.pk).update(letzter_kontakt=zugang.bindung.angelegt_am)
    zugang.refresh_from_db()
    with pytest.raises(dienst.DienstFehler):
        dienst.bindung_loeschen(akteur, zugang.bindung)
    assert ChatBindung.objects.filter(pk=zugang.bindung.pk).exists()
    WerkbankInstanz.objects.filter(pk=zugang.pk).update(letzter_kontakt=None)
    zugang.refresh_from_db()
    dienst.bindung_loeschen(akteur, zugang.bindung)
    assert not WerkbankInstanz.objects.exists() and not ChatBindung.objects.exists()


def test_bindung_benutzt_zaehlt_werkbank_instanz_mit(zugang):
    assert not dienst.bindung_benutzt(zugang.bindung)
    WerkbankInstanz.objects.filter(pk=zugang.pk).update(letzter_kontakt=zugang.bindung.angelegt_am)
    assert dienst.bindung_benutzt(ChatBindung.objects.get(pk=zugang.bindung.pk))


def test_verbindungsseite_zeigt_werkbank_zugang(owner_client, zugang):
    antwort = owner_client.get(reverse("verbindungen"))
    assert antwort.status_code == 200
    assert "Werkbank PC" in antwort.content.decode()
    assert zugang.klartext not in antwort.content.decode()


# --- Abweisung an Chat- und MCP-Endpunkten ----------------------------------------------------


def _mcp(client, schluessel, methode="tools/list", params=None):
    return client.post(
        reverse("mcp_link", kwargs={"schluessel": schluessel}),
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": methode, "params": params or {}}),
        content_type="application/json",
    )


def test_mcp_weist_werkbank_schluessel_verstaendlich_ab(client, zugang):
    for methode in ("initialize", "tools/list"):
        antwort = _mcp(client, zugang.klartext, methode)
        assert antwort.status_code == 403
        text = antwort.json()["error"]["message"]
        assert "Werkbank" in text and "/api/werkbank/" in text
        assert zugang.klartext not in text
    aufruf = _mcp(client, zugang.klartext, "tools/call", {"name": "kipm_projekte", "arguments": {}})
    assert aufruf.status_code == 403


def test_mcp_bearer_weist_werkbank_schluessel_ab(client, zugang):
    antwort = client.post(
        reverse("mcp"),
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {zugang.klartext}",
    )
    assert antwort.status_code == 403


@pytest.mark.parametrize(
    "name",
    ["api_projekte", "api_projekt", "api_auftraege", "api_verlauf", "api_sitzungsstart"],
)
def test_api_lesewege_weisen_werkbank_schluessel_ab(client, zugang, name):
    antwort = client.get(reverse(name), HTTP_AUTHORIZATION=f"Bearer {zugang.klartext}")
    assert antwort.status_code == 403
    daten = antwort.json()
    assert daten["ok"] is False and "Werkbank" in daten["fehler"]
    assert zugang.klartext not in daten["fehler"]


def test_api_schreibweg_weist_ab_und_aendert_nichts(client, zugang, projekt):
    vorher = Ereignis.objects.count()
    antwort = client.post(
        reverse("api_auftrag_anlegen"),
        data=json.dumps({"titel": "X", "anfrage_kennung": "k1", "fred_sagte": "Ja."}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {zugang.klartext}",
        HTTP_X_KIPM_PROJEKT=projekt.kennung,
    )
    assert antwort.status_code == 403
    assert Ereignis.objects.count() == vorher


def test_widerrufener_werkbank_schluessel_bleibt_401(client, akteur, zugang):
    werkbank.instanz_widerrufen(akteur, zugang)
    antwort = client.get(reverse("api_projekte"), HTTP_AUTHORIZATION=f"Bearer {zugang.klartext}")
    assert antwort.status_code == 401
    assert _mcp(client, zugang.klartext).status_code == 401


def test_chat_und_pc_schluessel_arbeiten_unveraendert(client, bindung, zugang):
    antwort = client.get(reverse("api_projekt"), HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}")
    assert antwort.status_code == 200 and antwort.json()["ok"] is True
    assert _mcp(client, bindung.klartext).status_code == 200

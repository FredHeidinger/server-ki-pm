"""Drei Wege KP01: gespeicherte Befugnis statt behaupteter Chatfreigabe."""

import json
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from django.db import close_old_connections, transaction
from django.test import Client
from django.utils import timezone

from steuerung import dienst, werkbank
from steuerung.models import Auftrag, ChatBindung, WerkbankChatDelegationsfassung, WerkbankZuweisung

pytestmark = pytest.mark.django_db
CAP = {"waehrung": "USD", "geld_mikro": 5_000_000, "zeit_ms": 600_000, "aufrufe": 10}


@pytest.fixture
def umfeld(akteur, projekt, bindung, w_pbindung):
    w_pbindung.repo_fingerabdruck = "repo-delegiert"
    w_pbindung.bereit = True
    w_pbindung.save()
    return akteur, projekt, bindung, w_pbindung


def erteilen(umfeld, **extra):
    akteur, projekt, bindung, ziel = umfeld
    daten = {
        "aktionen": ["auftrag_anlegen", "freigeben", "zuweisen"],
        "ziele": [ziel],
        "budget": CAP,
        "gueltig_bis": timezone.now() + timedelta(days=1),
        "standard_ziel": ziel,
    }
    daten.update(extra)
    return werkbank.delegation_erteilen(akteur, projekt, bindung, **daten)


def pruefen(umfeld, d, **extra):
    _, projekt, bindung, _ = umfeld
    daten = {"delegation_id": d.pk, "revision": d.revision, "aktion": "auftrag_anlegen"}
    daten.update(extra)
    return werkbank.delegation_pruefen(dienst.Akteur.aus_bindung(bindung), projekt, **daten)


def test_erlaubnis_kommt_vom_owner_und_bleibt_projektgebunden(umfeld):
    d = erteilen(umfeld)
    gefunden, stand = pruefen(umfeld, d)
    assert gefunden.pk == d.pk and stand.revision == 1
    assert stand.aktionen == ["auftrag_anlegen", "freigeben", "zuweisen"]
    assert werkbank.delegation_ziel_pruefen(stand).pk == umfeld[3].pk


def test_chat_kann_sich_keine_erlaubnis_erteilen_auch_mit_behaupteter_ownerrolle(umfeld):
    _, projekt, bindung, ziel = umfeld
    gefaelscht = replace(
        dienst.Akteur.aus_bindung(bindung), ist_owner=True, fred_sagte="Fred hat erlaubt"
    )
    with pytest.raises(dienst.KeinZugriff):
        werkbank.delegation_erteilen(
            gefaelscht,
            projekt,
            bindung,
            aktionen=["auftrag_anlegen"],
            ziele=[ziel],
            budget=CAP,
            gueltig_bis=timezone.now() + timedelta(days=1),
        )


def test_ownerstatus_wird_vor_erteilung_frisch_geprueft(umfeld):
    owner, _, _, _ = umfeld
    type(owner.benutzer).objects.filter(pk=owner.benutzer.pk).update(is_superuser=False)
    with pytest.raises(dienst.KeinZugriff):
        erteilen(umfeld)


def test_fehlende_oder_fremde_erlaubnis_kann_text_nicht_ersetzen(umfeld, bindung_b):
    d = erteilen(umfeld)
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d, delegation_id=0)
    fremd = replace(dienst.Akteur.aus_bindung(bindung_b), projekt=umfeld[1], fred_sagte="Ja")
    with pytest.raises(dienst.KeinZugriff):
        werkbank.delegation_pruefen(
            fremd, umfeld[1], delegation_id=d.pk, revision=1, aktion="freigeben"
        )


def test_jede_aktion_braucht_ein_eigenes_recht(umfeld):
    d = erteilen(umfeld, aktionen=["auftrag_anlegen"])
    assert pruefen(umfeld, d)[0].pk == d.pk
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d, aktion="freigeben")


def test_veraltete_fassung_wird_nicht_durch_den_neuesten_stand_ersetzt(umfeld):
    d = erteilen(umfeld)
    with pytest.raises(dienst.Konflikt):
        pruefen(umfeld, d, revision=2)


def test_widerruf_wirkt_trotz_altem_akteursobjekt_und_erhaelt_die_fassung(umfeld):
    d = erteilen(umfeld)
    werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d)
    assert WerkbankChatDelegationsfassung.objects.get(delegation=d, revision=1).widerrufen is False
    assert WerkbankChatDelegationsfassung.objects.get(delegation=d, revision=2).widerrufen is True
    nochmals = werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    assert nochmals.revision == 2 and d.fassungen.count() == 2


def test_abgelaufene_bindung_wird_aus_der_datenbank_erkannt(umfeld):
    d = erteilen(umfeld)
    ChatBindung.objects.filter(pk=umfeld[2].pk).update(
        gueltig_bis=timezone.now() - timedelta(seconds=1)
    )
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d)


def test_aussteller_verliert_ownerrecht(umfeld):
    d = erteilen(umfeld)
    type(umfeld[0].benutzer).objects.filter(pk=umfeld[0].benutzer.pk).update(is_superuser=False)
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d)


def test_archiviertes_projekt_gibt_keine_neue_befugnis(umfeld):
    d = erteilen(umfeld)
    type(umfeld[1]).objects.filter(pk=umfeld[1].pk).update(aktiv=False)
    with pytest.raises(dienst.KeinZugriff):
        pruefen(umfeld, d)


@pytest.mark.parametrize("feld,wert", [("version", 2), ("repo_fingerabdruck", "anderes-repo")])
def test_geaenderte_zielbindung_braucht_eine_neue_erlaubnis(umfeld, feld, wert):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    setattr(umfeld[3], feld, wert)
    umfeld[3].save()
    with pytest.raises(dienst.Konflikt):
        werkbank.delegation_ziel_pruefen(stand)


def test_geaenderter_arbeitsortpfad_ist_keine_metadatenfreigabe(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    ort = umfeld[3].arbeitsort
    ort.pfad = "C:/AnderesProjekt"
    ort.save()
    with pytest.raises(dienst.Konflikt):
        werkbank.delegation_ziel_pruefen(stand)


def test_geaenderte_instanz_ist_keine_berechtigung_fuer_einen_anderen_dienst(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    instanz = umfeld[3].instanz
    instanz.werkbank_id = uuid.uuid4()
    instanz.save()
    with pytest.raises(dienst.Konflikt):
        werkbank.delegation_ziel_pruefen(stand)


def test_umbenannter_arbeitsort_entwertet_die_zielrechte_nicht(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    ort = umfeld[3].arbeitsort
    ort.name = "Besserer Anzeigename"
    ort.save()
    assert werkbank.delegation_ziel_pruefen(stand).pk == umfeld[3].pk


def test_freier_zieltext_oder_fremde_zielkennung_erteilt_nichts(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    with pytest.raises(dienst.KeinZugriff):
        werkbank.delegation_ziel_pruefen(stand, 999999)


@pytest.mark.parametrize(
    "feld,wert",
    [
        ("geld_mikro", -1),
        ("geld_mikro", True),
        ("geld_mikro", 1.5),
        ("zeit_ms", 0),
        ("aufrufe", 0),
        ("waehrung", ""),
        ("waehrung", "ZZZ"),
        ("geld_mikro", 2**53),
    ],
)
def test_ungueltige_budgets_werden_nicht_umgedeutet(umfeld, feld, wert):
    with pytest.raises(dienst.DienstFehler):
        erteilen(umfeld, budget={**CAP, feld: wert})


def test_rahmen_wird_nicht_durch_mehrere_auftraege_vervielfacht(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    auftraege = [
        dienst.wunsch_erfassen(umfeld[0], umfeld[1], titel=f"Budgetfall {i}", beschreibung="Test")
        for i in range(2)
    ]
    for a in auftraege:
        a.chat_delegation = d
        a.chat_delegation_revision = 1
        a.chat_budget = {"waehrung": "USD", "geld_mikro": 3_000_000, "zeit_ms": 1000, "aufrufe": 1}
        a.save()
    with transaction.atomic():
        werkbank.delegation_budget_reservieren(
            dienst.Akteur.aus_bindung(umfeld[2]), auftraege[0], revision=1
        )
    with pytest.raises(dienst.KeinZugriff), transaction.atomic():
        werkbank.delegation_budget_reservieren(
            dienst.Akteur.aus_bindung(umfeld[2]), auftraege[1], revision=1
        )
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 3_000_000
    auftraege[1].refresh_from_db()
    assert auftraege[1].chat_budget_reserviert is False


def budgetauftrag(umfeld, d, titel="Budgetauftrag"):
    a = dienst.wunsch_erfassen(umfeld[0], umfeld[1], titel=titel, beschreibung="Budgettest")
    a.chat_delegation = d
    a.chat_delegation_revision = 1
    a.chat_budget = {"waehrung": "USD", "geld_mikro": 3_000_000, "zeit_ms": 1000, "aufrufe": 1}
    a.save()
    return a


def test_reservierung_liest_frische_auftragsdaten_und_ist_idempotent(umfeld):
    d = erteilen(umfeld)
    a = budgetauftrag(umfeld, d)
    a.chat_budget = {**CAP, "geld_mikro": 0}
    chat = dienst.Akteur.aus_bindung(umfeld[2])
    werkbank.delegation_budget_reservieren(chat, a, revision=1)
    werkbank.delegation_budget_reservieren(chat, a, revision=1)
    d.refresh_from_db()
    assert d.reserviert == {"geld_mikro": 3_000_000, "zeit_ms": 1000, "aufrufe": 1}


def test_widerruf_vor_reservierung_verhindert_wirkung(umfeld):
    d = erteilen(umfeld)
    a = budgetauftrag(umfeld, d)
    werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    with pytest.raises(dienst.KeinZugriff):
        werkbank.delegation_budget_reservieren(dienst.Akteur.aus_bindung(umfeld[2]), a, revision=1)
    d.refresh_from_db()
    a.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 0 and not a.chat_budget_reserviert


def test_budgetfreigabe_ist_nur_owner_und_wiederholbar(umfeld):
    d = erteilen(umfeld)
    a = budgetauftrag(umfeld, d)
    chat = dienst.Akteur.aus_bindung(umfeld[2])
    werkbank.delegation_budget_reservieren(chat, a, revision=1)
    with pytest.raises(dienst.KeinZugriff):
        werkbank.delegation_budget_freigeben(chat, a)
    werkbank.delegation_budget_freigeben(umfeld[0], a)
    werkbank.delegation_budget_freigeben(umfeld[0], a)
    d.refresh_from_db()
    assert d.reserviert == dict.fromkeys(("geld_mikro", "zeit_ms", "aufrufe"), 0)


@pytest.mark.django_db(transaction=True)
def test_zwei_gleichzeitige_auftraege_teilen_sich_den_rahmen(umfeld):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    d = erteilen(umfeld)
    auftraege = [budgetauftrag(umfeld, d, f"Parallelbudget {i}") for i in range(2)]
    start = Barrier(2)

    def reservieren(a):
        close_old_connections()
        try:
            chat = dienst.Akteur.aus_bindung(ChatBindung.objects.get(pk=umfeld[2].pk))
            start.wait(timeout=10)
            try:
                werkbank.delegation_budget_reservieren(chat, a, revision=1)
                return "reserviert"
            except dienst.KeinZugriff:
                return "abgelehnt"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ergebnisse = list(pool.map(reservieren, auftraege))
    assert sorted(ergebnisse) == ["abgelehnt", "reserviert"]
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 3_000_000
    assert d.auftraege.filter(chat_budget_reserviert=True).count() == 1


def test_ownerwiderruf_zwischen_vorpruefung_und_speichern_wird_erkannt(umfeld, monkeypatch):
    modul = werkbank._teilmodule()[3]
    original = modul.budget_pruefen

    def budget_mit_rollenwechsel(roh):
        type(umfeld[0].benutzer).objects.filter(pk=umfeld[0].benutzer.pk).update(is_superuser=False)
        return original(roh)

    monkeypatch.setattr(modul, "budget_pruefen", budget_mit_rollenwechsel)
    with pytest.raises(dienst.KeinZugriff):
        erteilen(umfeld)


@pytest.mark.parametrize("aktionen", [[], ["administrieren"], [{"ist_owner": True}]])
def test_unbekannte_oder_gefaelschte_aktionen_werden_geordnet_abgewiesen(umfeld, aktionen):
    with pytest.raises(dienst.DienstFehler):
        erteilen(umfeld, aktionen=aktionen)


def w1_anlegen(umfeld, d, **extra):
    daten = {
        "delegation_id": d.pk,
        "revision": 1,
        "budget": {"waehrung": "USD", "geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1},
        "titel": "Delegiert erstellen",
        "beschreibung": "Anmeldehinweis verbessern",
        "planung": {
            "ziel": "Klarer Hinweis",
            "nicht_ziele": "keine",
            "schreibumfang": "src/auth/**",
            "abnahmekriterien": "Hinweis sichtbar",
        },
    }
    daten.update(extra)
    return werkbank.delegation_auftrag_anlegen(
        dienst.Akteur.aus_bindung(umfeld[2]), umfeld[1], **daten
    )


def test_projektschluessel_kann_mit_echter_erlaubnis_atomaren_w1_auftrag_freigeben(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    assert a.zustand == Auftrag.FREIGEGEBEN
    assert a.chat_delegation_id == d.pk and a.chat_delegation_revision == 1
    assert a.chat_budget_reserviert
    assert a.zuweisung.projektbindung_id == umfeld[3].pk
    assert a.zuweisung.kontext_sha256
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 1_000_000


@pytest.mark.parametrize("aktionen", [["auftrag_anlegen"], ["auftrag_anlegen", "zuweisen"]])
def test_fehlendes_teilrecht_rollt_die_ganze_anlage_zurueck(umfeld, aktionen):
    d = erteilen(umfeld, aktionen=aktionen)
    vorher = Auftrag.objects.count()
    with pytest.raises(dienst.KeinZugriff):
        w1_anlegen(umfeld, d, freigeben=True)
    assert Auftrag.objects.count() == vorher
    assert not WerkbankZuweisung.objects.filter(auftrag__chat_delegation=d).exists()
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 0


def test_nur_anlegen_recht_ergibt_auf_wunsch_einen_entwurf(umfeld):
    d = erteilen(umfeld, aktionen=["auftrag_anlegen"])
    a = w1_anlegen(umfeld, d, zuweisen=False)
    assert a.zustand == Auftrag.ENTWURF and not a.chat_budget_reserviert
    assert not WerkbankZuweisung.objects.filter(auftrag=a).exists()


def test_w1_auftrag_wird_nicht_ueber_den_alten_v1_vertrag_angeboten_oder_gestartet(umfeld):
    from tests import werkbank_hilfen as h

    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    inst = umfeld[3].instanz
    seite = werkbank.abgleich_seite(inst)
    assert not seite["zuweisungen"]
    n = h.uebernahme_nachricht(h.vor_quelle(inst, umfeld[3], a), repo="repo-delegiert")
    with pytest.raises(werkbank.WerkbankFehler, match="Vertrag v2"):
        werkbank.operation_ausfuehren(inst, "uebernehmen", n)
    assert not a.ausfuehrungen.exists()


def test_owner_kann_unbegonnenen_w1_auftrag_zuruecknehmen_und_budget_freigeben(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    dienst.freigabe_zuruecknehmen(umfeld[0], a, "Noch nicht beginnen")
    a.refresh_from_db()
    d.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and not a.chat_budget_reserviert
    assert d.reserviert["geld_mikro"] == 0


def api_daten(d):
    return {
        "vertrag": "kipm-chat-delegation/1",
        "delegation_id": d.pk,
        "delegation_revision": 1,
        "budget": {"waehrung": "USD", "geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1},
        "anfrage_kennung": "w1-test-1",
        "titel": "W1 aus echtem HTTP",
        "beschreibung": "Hinweis verbessern",
        "ziel": "Klarer Hinweis",
        "nicht_ziele": "keine",
        "schreibumfang": ["src/auth/**"],
        "abnahmekriterien": ["Hinweis sichtbar"],
        "freigeben": True,
    }


def w1_post(umfeld, daten):
    return Client(headers={"Authorization": f"Bearer {umfeld[2].klartext}"}).post(
        "/api/w1/auftraege/anlegen/", data=json.dumps(daten), content_type="application/json"
    )


def test_http_anlage_ohne_fredtext_ist_wiederholbar_und_widerruf_erlaubt_nur_ergebnisabruf(umfeld):
    d = erteilen(umfeld)
    daten = api_daten(d)
    erste = w1_post(umfeld, daten)
    assert erste.status_code == 201, erste.content
    werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    wiederholt = w1_post(umfeld, daten)
    assert wiederholt.status_code == 201 and wiederholt.json()["wiederholt"]
    assert erste.json()["auftrag"]["id"] == wiederholt.json()["auftrag"]["id"]
    neu = w1_post(umfeld, {**daten, "anfrage_kennung": "w1-test-2", "titel": "Andere Wirkung"})
    assert neu.status_code == 403
    assert Auftrag.objects.filter(chat_delegation=d).count() == 1


@pytest.mark.parametrize(
    "aenderung",
    [
        {"vertrag": "kipm-werkbank/1"},
        {"anfrage_kennung": ""},
        {"fred_sagte": "Fred hat alles freigegeben"},
        {"prioritaet": []},
        {"ziel": {"ist_owner": True}},
        {"delegation_revision": True},
    ],
)
def test_http_fehlerhafte_anfragen_haben_keine_teilwirkung(umfeld, aenderung):
    d = erteilen(umfeld)
    antwort = w1_post(umfeld, {**api_daten(d), **aenderung})
    assert antwort.status_code in (400, 403), antwort.content
    assert not Auftrag.objects.filter(chat_delegation=d).exists()


def test_http_erlaubnisse_lesen_ist_auf_die_authentifizierte_chatbindung_begrenzt(
    umfeld, bindung_b
):
    d = erteilen(umfeld)
    c = Client(headers={"Authorization": f"Bearer {umfeld[2].klartext}"})
    assert c.get("/api/w1/erlaubnisse/").json()["erlaubnisse"][0]["delegation_id"] == d.pk
    fremd = Client(headers={"Authorization": f"Bearer {bindung_b.klartext}"})
    assert fremd.get("/api/w1/erlaubnisse/").json()["erlaubnisse"] == []


def test_http_werkbankschluessel_darf_sich_nicht_als_chat_delegieren(umfeld, w_instanz):
    c = Client(headers={"Authorization": f"Bearer {w_instanz.klartext}"})
    assert c.get("/api/w1/erlaubnisse/").status_code == 403


def test_mcp_w1_hat_eigene_kennung_und_erfindet_keine_fredworte(umfeld, client):
    from tests.test_mcp import _werkzeug

    d = erteilen(umfeld)
    daten = api_daten(d)
    daten.pop("vertrag")
    daten["text"] = daten.pop("beschreibung")
    fehler, antwort = _werkzeug(
        client, umfeld[2].klartext, "kipm_w1_auftrag_anlegen", fred_sagte=None, **daten
    )
    assert not fehler, antwort
    assert Auftrag.objects.get(pk=antwort["auftrag"]["id"]).chat_delegation_id == d.pk
    daten.pop("anfrage_kennung")
    fehler, antwort = _werkzeug(
        client, umfeld[2].klartext, "kipm_w1_auftrag_anlegen", fred_sagte=None, **daten
    )
    assert fehler and "anfrage_kennung" in str(antwort)
    assert Auftrag.objects.filter(chat_delegation=d).count() == 1


def formular_daten(umfeld):
    return {
        "w1-projekt": umfeld[1].pk,
        "w1-bindung": umfeld[2].pk,
        "w1-ziele": [umfeld[3].pk],
        "w1-standard_ziel": umfeld[3].pk,
        "w1-aktionen": ["auftrag_anlegen", "zuweisen", "freigeben"],
        "w1-waehrung": "USD",
        "w1-geld": "5.000001",
        "w1-minuten": "10",
        "w1-aufrufe": "10",
        "w1-gueltig_bis": timezone.localtime(timezone.now() + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
    }


def test_ownerformular_erteilt_und_widerruft_mit_genauem_budget_ohne_laufstart(
    umfeld, owner_client
):
    from steuerung.models import WerkbankChatDelegation

    r = owner_client.post("/verbindungen/w1/erteilen/", formular_daten(umfeld))
    assert r.status_code == 302
    d = WerkbankChatDelegation.objects.get(bindung=umfeld[2])
    assert d.fassungen.get(revision=1).budget["geld_mikro"] == 5_000_001
    seite = owner_client.get("/verbindungen/").content.decode()
    assert "W1: Chat an die Werkbank delegieren" in seite
    assert "Erlaubnis widerrufen" in seite
    assert not Auftrag.objects.filter(chat_delegation=d).exists()
    assert (
        owner_client.post(f"/verbindungen/w1/{d.pk}/widerrufen/", {"revision": 1}).status_code
        == 302
    )
    d.refresh_from_db()
    assert d.widerrufen_am is not None


def test_fehlerhaftes_ownerformular_behaelt_eingaben_und_erteilt_keine_rechte(umfeld, owner_client):
    daten = {**formular_daten(umfeld), "w1-geld": "-1"}
    r = owner_client.post("/verbindungen/w1/erteilen/", daten)
    assert r.status_code == 400
    assert "W1-Erlaubnis erteilen" in r.content.decode()
    assert not umfeld[2].werkbank_delegationen.exists()


def test_erlaubnisverwaltung_verlangt_owner_session_und_csrf(umfeld, client, zweiter_mensch):
    daten = formular_daten(umfeld)
    client.force_login(zweiter_mensch)
    assert client.post("/verbindungen/w1/erteilen/", daten).status_code == 403
    assert "Begrenzte Erlaubnis speichern" not in client.get("/verbindungen/").content.decode()
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(umfeld[0].benutzer)
    assert csrf.post("/verbindungen/w1/erteilen/", daten).status_code == 403
    assert not umfeld[2].werkbank_delegationen.exists()


def w1_aktion(umfeld, d, a, aktion, **extra):
    daten = {
        "vertrag": "kipm-chat-delegation/1",
        "delegation_id": d.pk,
        "delegation_revision": 1,
        "auftrag_revision": a.revision_nr,
        "aktion": aktion,
        "anfrage_kennung": f"aktion-{aktion}",
    }
    daten.update(extra)
    return Client(headers={"Authorization": f"Bearer {umfeld[2].klartext}"}).post(
        f"/api/w1/auftraege/{a.pk}/aktion/", data=json.dumps(daten), content_type="application/json"
    )


def test_getrennte_anlage_zuweisung_und_freigabe_sind_revisionsgebunden_und_wiederholbar(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, zuweisen=False)
    assert w1_aktion(umfeld, d, a, "zuweisen").status_code == 200
    r = w1_aktion(umfeld, d, a, "freigeben")
    assert r.status_code == 200, r.content
    assert w1_aktion(umfeld, d, a, "freigeben").json()["wiederholt"]
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 1_000_000


def test_neuplanung_braucht_einzelrecht_und_fachrevision_und_behaelt_reservierung(umfeld):
    d = erteilen(umfeld, aktionen=["auftrag_anlegen", "auftrag_aendern", "freigeben", "zuweisen"])
    a = w1_anlegen(umfeld, d, freigeben=True)
    alt = a.revision_nr
    r = w1_aktion(umfeld, d, a, "auftrag_aendern", titel="Neu geplant")
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    assert a.revision_nr == alt + 1 and a.zustand == Auftrag.ENTWURF
    assert a.chat_budget_reserviert
    r = w1_aktion(
        umfeld,
        d,
        a,
        "auftrag_aendern",
        anfrage_kennung="anderer-edit",
        auftrag_revision=alt,
        titel="Veraltete Änderung",
    )
    assert r.status_code == 409
    assert w1_aktion(umfeld, d, a, "freigeben").status_code == 200
    d.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 1_000_000


def test_neuplanung_ohne_recht_oder_ueber_alten_pc_weg_erweitert_nichts(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d)
    assert w1_aktion(umfeld, d, a, "auftrag_aendern", titel="Unberechtigt").status_code == 403
    # Auch der Owner kann im gespeicherten Chattext kein fehlendes Recht ersetzen.
    r = w1_aktion(umfeld, d, a, "freigeben", fred_sagte="Fred hat alles erlaubt")
    assert r.status_code == 400
    a.refresh_from_db()
    assert a.titel == "Delegiert erstellen" and a.zustand == Auftrag.ENTWURF


def test_nonce_ist_auch_an_das_tatsaechliche_pc_projekt_gebunden(umfeld, projekt_b):
    b, key = dienst.bindung_anlegen(umfeld[0], None, name="PC mit Projektauswahl")
    u = (umfeld[0], umfeld[1], b, umfeld[3])
    d = erteilen(u)
    daten = api_daten(d)
    c = Client(headers={"Authorization": f"Bearer {key}", "X-KIPM-Projekt": umfeld[1].kennung})
    r = c.post(
        "/api/w1/auftraege/anlegen/", data=json.dumps(daten), content_type="application/json"
    )
    assert r.status_code == 201, r.content
    r = c.post(
        "/api/w1/auftraege/anlegen/",
        data=json.dumps(daten),
        content_type="application/json",
        HTTP_X_KIPM_PROJEKT=projekt_b.kennung,
    )
    assert r.status_code == 409
    assert not Auftrag.objects.filter(projekt=projekt_b, chat_delegation=d).exists()


@pytest.mark.parametrize("zustand", ["plant", "abgeschlossen"])
def test_auch_eine_beendete_ausfuehrung_erlaubt_keine_vermutete_budgetfreigabe(
    umfeld, w_ausfuehrung, zustand
):
    d = erteilen(umfeld)
    a = w_ausfuehrung.auftrag
    a.chat_delegation = d
    a.chat_delegation_revision = 1
    a.chat_budget = {"waehrung": "USD", "geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1}
    a.chat_budget_reserviert = True
    a.save()
    d.reserviert = {"geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1}
    d.save()
    w_ausfuehrung.zustand = zustand
    w_ausfuehrung.save()
    with pytest.raises(dienst.Konflikt, match="Endnachweis"):
        werkbank.delegation_budget_freigeben(umfeld[0], a)
    d.refresh_from_db()
    a.refresh_from_db()
    assert d.reserviert["geld_mikro"] == 1_000_000 and a.chat_budget_reserviert


def test_widerruf_entwertet_ausstehende_freigabe_und_zielversion_ohne_budget_zu_erfinden(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    version = a.zuweisung.version
    werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and a.freigegeben_am is None
    assert a.zuweisung.version == version + 1 and not a.zuweisung.kontext_sha256
    assert a.chat_budget_reserviert


@pytest.mark.parametrize("aenderung", ["pfad", "werkbank", "chat"])
def test_ziel_oder_zugangsaenderung_entwertet_die_passende_freigabe(umfeld, aenderung):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    if aenderung == "pfad":
        ort = umfeld[3].arbeitsort
        dienst.arbeitsort_aendern(
            umfeld[0], ort, erwartete_revision=ort.revision, pfad="C:/NeuesNichtDelegiertesZiel"
        )
    elif aenderung == "werkbank":
        werkbank.instanz_aktiv_setzen(umfeld[0], umfeld[3].instanz, False)
    else:
        dienst.bindung_widerrufen(umfeld[0], umfeld[2])
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and not a.zuweisung.kontext_sha256


def test_anzeigename_allein_entwertet_w1_freigabe_nicht(umfeld):
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    ort = umfeld[3].arbeitsort
    dienst.arbeitsort_aendern(
        umfeld[0], ort, erwartete_revision=ort.revision, name="Neuer Anzeigename"
    )
    a.refresh_from_db()
    assert a.zustand == Auftrag.FREIGEGEBEN and a.zuweisung.kontext_sha256


@pytest.mark.parametrize("aenderung", ["neue_version", "archiviert", "langer_text", "unveraendert"])
def test_w1_freigabe_bindet_den_vollstaendigen_quellenstand(umfeld, aenderung):
    from steuerung import akte

    owner, projekt, _, _ = umfeld
    langer_text = "Absatz. " * 13_000
    u = akte.text_anlegen(owner, projekt, titel="Anforderung", text=langer_text + "A")
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    zuweisungsversion = a.zuweisung.version
    vorher = a.zuweisung.kontext_sha256
    if aenderung == "neue_version":
        akte.text_neue_version(owner, u, text=langer_text + "B", erwartete_nr=u.aktuelle_nr)
    elif aenderung == "archiviert":
        akte.unterlage_archivieren(owner, u, True)
    elif aenderung == "langer_text":
        # Simuliert eine inkonsistente restaurierte Textfassung. Die alte Transportvorschau endet vor dem letzten Zeichen.
        u.versionen.update(text=langer_text + "B")
        werkbank.delegation_freigaben_pruefen(owner, projekt)
    else:
        werkbank.delegation_freigaben_pruefen(owner, projekt)
    a.refresh_from_db()
    assert a.chat_budget_reserviert and not a.ausfuehrungen.exists()
    if aenderung == "unveraendert":
        assert a.zustand == Auftrag.FREIGEGEBEN and a.zuweisung.kontext_sha256 == vorher
        assert a.zuweisung.version == zuweisungsversion
    else:
        assert a.zustand == Auftrag.ENTWURF and not a.zuweisung.kontext_sha256
        assert a.zuweisung.version == zuweisungsversion + 1


@pytest.mark.parametrize("variante", ["geteilt", "fremd", "altes_hashformat"])
def test_quellenbindung_beachtet_projektgeltung_und_bestehende_freigaben(
    umfeld, projekt_b, variante
):
    from steuerung import akte

    owner, projekt, _, _ = umfeld
    entscheidung = akte.entscheidung_eintragen(
        owner, projekt_b, entscheidung="Berichte auf Deutsch."
    )
    if variante == "geteilt":
        entscheidung.betroffene_projekte.add(projekt)
    d = erteilen(umfeld)
    a = w1_anlegen(umfeld, d, freigeben=True)
    if variante == "altes_hashformat":
        WerkbankZuweisung.objects.filter(auftrag=a).update(
            kontext_sha256=werkbank.kontext_hash(werkbank.snapshot_bauen(a))
        )
        werkbank.delegation_freigaben_pruefen(owner, projekt)
    else:
        akte.entscheidung_aendern(
            owner,
            projekt_b,
            entscheidung,
            erwarteter_stand=entscheidung.stand,
            entscheidung="Berichte auf Englisch.",
        )
    a.refresh_from_db()
    if variante == "fremd":
        assert a.zustand == Auftrag.FREIGEGEBEN
    else:
        assert a.zustand == Auftrag.ENTWURF and not a.zuweisung.kontext_sha256
        assert w1_aktion(umfeld, d, a, "freigeben").status_code == 200
        a.refresh_from_db()
        assert a.zustand == Auftrag.FREIGEGEBEN and a.chat_budget_reserviert
        d.refresh_from_db()
        assert d.reserviert["geld_mikro"] == a.chat_budget["geld_mikro"]


@pytest.mark.parametrize(
    "aenderung", ["extrakt", "pruefung_ausstehend", "archivierung", "widerruf", "repo", "gleich"]
)
def test_w1_freigabe_bindet_lesbare_altquellen(umfeld, aenderung):
    from steuerung import akte
    from steuerung.models import Unterlage, Unterlagenversion

    owner, projekt, _, _ = umfeld
    ist_repo = aenderung == "repo"
    u = Unterlage.objects.create(
        projekt=projekt,
        nummer=900,
        titel="Handbuch",
        art=Unterlage.REPO if ist_repo else Unterlage.DATEI,
        ki_freigegeben=not ist_repo,
        aktuelle_nr=1,
    )
    v = u.versionen.create(
        nummer=1,
        autor_name="Test",
        pruefung=Unterlagenversion.SAUBER,
        extrakt=("Absatz. " * 14_000) + "A" if not ist_repo else "",
        uri="docs/handbuch.md" if ist_repo else "",
        quellversion="abc" if ist_repo else "",
    )
    a = w1_anlegen(umfeld, erteilen(umfeld), freigeben=True)
    fassung = a.zuweisung.version
    if aenderung == "extrakt":
        v.extrakt = v.extrakt[:-1] + "B"
        v.save(update_fields=["extrakt"])
    elif aenderung == "pruefung_ausstehend":
        u.versionen.create(nummer=2, autor_name="Test", pruefung=Unterlagenversion.AUSSTEHEND)
    elif aenderung == "archivierung":
        akte.unterlage_archivieren(owner, u, True)
    elif aenderung == "widerruf":
        u.ki_freigegeben = False
        u.save(update_fields=["ki_freigegeben"])
    elif ist_repo:
        v.quellversion = "def"
        v.save(update_fields=["quellversion"])
    werkbank.delegation_freigaben_pruefen(owner, projekt)
    a.refresh_from_db()
    assert a.chat_budget_reserviert and not a.ausfuehrungen.exists()
    if aenderung == "gleich":
        assert a.zustand == Auftrag.FREIGEGEBEN and a.zuweisung.version == fassung
    else:
        assert a.zustand == Auftrag.ENTWURF and not a.zuweisung.kontext_sha256
        assert a.zuweisung.version == fassung + 1


def test_w1_altquellen_ohne_leserecht_erhalten_bestehende_bindung(umfeld):
    from steuerung.models import Unterlage, Unterlagenversion

    _, projekt, _, _ = umfeld
    vorher = werkbank.delegierter_quellenhash(projekt.pk)
    u = Unterlage.objects.create(
        projekt=projekt,
        nummer=900,
        titel="Anhang",
        art=Unterlage.DATEI,
        aktuelle_nr=1,
    )
    v = u.versionen.create(nummer=1, autor_name="Test", pruefung=Unterlagenversion.SAUBER)
    assert werkbank.delegierter_quellenhash(projekt.pk) == vorher
    v.pruefung = Unterlagenversion.ABGEWIESEN
    v.save(update_fields=["pruefung"])
    u.ki_freigegeben = True
    u.save(update_fields=["ki_freigegeben"])
    assert werkbank.delegierter_quellenhash(projekt.pk) == vorher
    v.pruefung = Unterlagenversion.SAUBER
    v.save(update_fields=["pruefung"])
    assert werkbank.delegierter_quellenhash(projekt.pk) != vorher


def test_erlaubnis_schuetzt_benutzte_chatbindung_vor_loeschung(umfeld):
    erteilen(umfeld)
    assert dienst.bindung_benutzt(umfeld[2])
    with pytest.raises(dienst.DienstFehler):
        dienst.bindung_loeschen(umfeld[0], umfeld[2])
    assert ChatBindung.objects.filter(pk=umfeld[2].pk).exists()


def test_widerruf_eines_laufenden_w1_auftrags_fordert_halt_ohne_falsche_endquittung(
    umfeld, w_ausfuehrung
):
    d = erteilen(umfeld)
    a = w_ausfuehrung.auftrag
    a.chat_delegation = d
    a.chat_delegation_revision = 1
    a.save()
    werkbank.delegation_widerrufen(umfeld[0], d, erwartet_revision=1)
    w_ausfuehrung.refresh_from_db()
    a.refresh_from_db()
    assert w_ausfuehrung.zustand == "halt_angefordert"
    assert w_ausfuehrung.halt_zweck == "kontext_veraltet"
    assert w_ausfuehrung.halt_bestaetigt_am is None
    assert a.zustand == Auftrag.IN_ARBEIT


def test_mehrere_ziele_ohne_standard_werden_nicht_geraten(umfeld):
    from tests.werkbank_hilfen import bindung_sichern

    inst, _ = werkbank.instanz_anlegen(umfeld[0], name="Zweite Werkbank", projekte=[umfeld[1]])
    ziel2 = bindung_sichern(instanz=inst, projekt=umfeld[1], arbeitsort=umfeld[3].arbeitsort)
    d = erteilen(umfeld, ziele=[umfeld[3], ziel2], standard_ziel=None)
    with pytest.raises(dienst.Konflikt, match="Mehrere Werkbankziele"):
        w1_anlegen(umfeld, d)
    assert not Auftrag.objects.filter(chat_delegation=d).exists()
    assert w1_anlegen(umfeld, d, ziel_id=ziel2.pk).zuweisung.projektbindung_id == ziel2.pk


def test_alte_chatplanung_ueberspringt_keine_w1_fassungspruefung(umfeld):
    d = erteilen(umfeld, aktionen=["auftrag_anlegen", "auftrag_aendern", "zuweisen"])
    a = w1_anlegen(umfeld, d)
    rev = a.aktuelle_revision
    daten = {
        k: getattr(rev, k)
        for k in (
            "beschreibung",
            "ziel",
            "nicht_ziele",
            "arbeitsort",
            "schreibumfang",
            "quellen",
            "abnahmekriterien",
        )
    }
    with pytest.raises(dienst.Konflikt, match="Erlaubnisfassung"):
        dienst.auftrag_bearbeiten(
            dienst.Akteur.aus_bindung(umfeld[2]),
            a,
            titel="Nicht über alte Planung",
            prioritaet=a.prioritaet,
            modul=a.modul,
            **daten,
        )
    a.refresh_from_db()
    assert a.titel == "Delegiert erstellen"


@pytest.mark.parametrize("kennung", [True, 27, {}, "x" * 81])
def test_w1_wiederholungskennung_wird_nicht_aus_fremden_typen_erfunden(umfeld, kennung):
    d = erteilen(umfeld)
    r = w1_post(umfeld, {**api_daten(d), "anfrage_kennung": kennung})
    assert r.status_code == 400
    assert not Auftrag.objects.filter(chat_delegation=d).exists()


def test_ausgetauschte_zugangsidentitaet_ist_keine_stille_zielerneuerung(umfeld):
    d = erteilen(umfeld)
    _, stand = pruefen(umfeld, d)
    inst = umfeld[3].instanz
    b = inst.bindung
    b.pk = None
    b.praefix += "-anderer"
    b.save()
    inst.bindung = b
    inst.save()
    with pytest.raises(dienst.Konflikt, match="Zielbindung"):
        werkbank.delegation_ziel_pruefen(stand)

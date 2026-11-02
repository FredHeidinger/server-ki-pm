"""HTTP-Schnittstelle: Zugang, Projektgrenze, Exklusivität, Wiederholungsschutz, Widerruf."""

import json

import pytest
from django.test import Client

from steuerung import dienst
from steuerung.models import AnfrageKennung, Auftrag

pytestmark = pytest.mark.django_db


def api(schluessel: str) -> Client:
    return Client(headers={"Authorization": f"Bearer {schluessel}"} if schluessel else {})


def post(c: Client, pfad: str, **daten):
    return c.post(pfad, data=json.dumps(daten), content_type="application/json")


def test_ohne_oder_mit_falschem_schluessel(freigegeben, bindung):
    assert api("").get("/api/auftraege/").status_code == 401
    assert api("kipm_falsch_falsch").get("/api/auftraege/").status_code == 401
    assert api(bindung.klartext + "x").get("/api/auftraege/").status_code == 401
    assert api(bindung.klartext).get("/api/auftraege/").status_code == 200


def test_sitzungscookie_gilt_nicht_fuer_api(owner_client, freigegeben):
    assert owner_client.get("/api/auftraege/").status_code == 401


def test_meine_auftraege_und_lesen(freigegeben, bindung):
    c = api(bindung.klartext)
    liste = c.get("/api/auftraege/").json()
    assert liste["ok"] and [a["nummer"] for a in liste["auftraege"]] == [freigegeben.nummer]
    assert liste["auftraege"][0]["uebernehmbar"] is True
    detail = c.get(f"/api/auftraege/{freigegeben.pk}/").json()["auftrag"]
    assert detail["schreibumfang"] == ["src/auth/**"] and len(detail["abnahmekriterien"]) == 2


def test_projektgrenze_auch_bei_erratener_id(freigegeben, bindung_b):
    c = api(bindung_b.klartext)
    assert c.get(f"/api/auftraege/{freigegeben.pk}/").status_code == 404
    antwort = post(
        c, f"/api/auftraege/{freigegeben.pk}/uebernehmen/", revision=2, anfrage_kennung="k1"
    )
    assert antwort.status_code == 404
    antwort = post(
        c, f"/api/auftraege/{freigegeben.pk}/vorschlag/", revision=2, text="x", anfrage_kennung="k2"
    )
    assert antwort.status_code == 404
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.FREIGEGEBEN


def test_doppelte_uebernahme_nur_eine_gewinnt(akteur, projekt, freigegeben, bindung):
    zweite, schluessel2 = dienst.bindung_anlegen(akteur, projekt, name="Chat A2")
    pfad = f"/api/auftraege/{freigegeben.pk}/uebernehmen/"
    erste = post(api(bindung.klartext), pfad, revision=2, anfrage_kennung="u1")
    zweite_antwort = post(api(schluessel2), pfad, revision=2, anfrage_kennung="u2")
    assert erste.status_code == 200 and erste.json()["auftrag"]["bearbeiter"] == "Chat A"
    assert zweite_antwort.status_code == 409
    assert "bereits von Chat A übernommen" in zweite_antwort.json()["fehler"]


def test_wiederholte_anfrage_gleicher_inhalt_keine_zweite_wirkung(freigegeben, bindung):
    c = api(bindung.klartext)
    pfad = f"/api/auftraege/{freigegeben.pk}/fortschritt/"
    post(c, f"/api/auftraege/{freigegeben.pk}/uebernehmen/", revision=2, anfrage_kennung="u")
    a1 = post(c, pfad, revision=2, text="halb fertig", anfrage_kennung="f1")
    a2 = post(c, pfad, revision=2, text="halb fertig", anfrage_kennung="f1")
    assert a1.status_code == 200 and a2.status_code == 200 and a2.json()["wiederholt"] is True
    assert freigegeben.meldungen.filter(text="halb fertig").count() == 1
    a3 = post(c, pfad, revision=2, text="anderer Text", anfrage_kennung="f1")
    assert a3.status_code == 409 and "anderem Inhalt" in a3.json()["fehler"]
    assert AnfrageKennung.objects.count() == 2
    assert post(c, pfad, revision=2, text="x").status_code == 400  # Kennung fehlt


def test_veraltete_revision_wird_abgewiesen(freigegeben, bindung):
    c = api(bindung.klartext)
    antwort = post(
        c, f"/api/auftraege/{freigegeben.pk}/uebernehmen/", revision=1, anfrage_kennung="x"
    )
    assert antwort.status_code == 409 and "veraltet" in antwort.json()["fehler"]


def test_ergebnis_und_hindernis_ueber_api(freigegeben, bindung):
    c = api(bindung.klartext)
    post(c, f"/api/auftraege/{freigegeben.pk}/uebernehmen/", revision=2, anfrage_kennung="u")
    h = post(
        c,
        f"/api/auftraege/{freigegeben.pk}/hindernis/",
        revision=2,
        text="Datei fehlt",
        anfrage_kennung="h",
    )
    assert h.json()["auftrag"]["zustand"] == "BLOCKIERT"
    e = post(
        c,
        f"/api/auftraege/{freigegeben.pk}/ergebnis/",
        revision=2,
        zusammenfassung="fertig",
        dateien=["src/auth/login.py@abc"],
        pruefungen=["pytest: 3 ok"],
        luecken="",
        anfrage_kennung="e",
    )
    assert e.status_code == 200 and e.json()["auftrag"]["zustand"] == "ZUR_PRUEFUNG"
    fehlend = post(
        c,
        f"/api/auftraege/{freigegeben.pk}/ergebnis/",
        revision=2,
        zusammenfassung="x",
        anfrage_kennung="e2",
    )
    assert fehlend.status_code == 409  # bereits zur Prüfung


def test_keine_schnittstelle_fuer_freigabe_oder_abnahme(freigegeben, bindung):
    c = api(bindung.klartext)
    for pfad in ("freigeben", "abnehmen", "pruefen", "bearbeiten"):
        assert (
            post(c, f"/api/auftraege/{freigegeben.pk}/{pfad}/", anfrage_kennung="x").status_code
            == 404
        )
    # Oberflächen-Aktionen verlangen Sitzung, nicht Schlüssel
    assert c.post(f"/auftraege/{freigegeben.pk}/aktion/freigeben/").status_code == 302


def test_widerrufene_bindung_sofort_gesperrt(akteur, freigegeben, bindung):
    c = api(bindung.klartext)
    assert c.get("/api/auftraege/").status_code == 200
    dienst.bindung_widerrufen(akteur, bindung, "Test")
    assert c.get("/api/auftraege/").status_code == 401


def test_entscheidungen_ueber_api(akteur, projekt, projekt_b, bindung):
    c = api(bindung.klartext)
    v = post(
        c,
        "/api/entscheidungen/vorschlagen/",
        titel="Namensregel",
        text="Alles klein",
        anfrage_kennung="v",
    )
    assert v.status_code == 201 and v.json()["entscheidung"]["zustand"] == "VORSCHLAG"
    nummer = int(v.json()["entscheidung"]["nummer"][2:])
    # Chat darf nicht entscheiden; Bestätigen vor Entscheidung schlägt fehl
    b = post(c, f"/api/entscheidungen/{nummer}/bestaetigen/", anfrage_kennung="b1")
    assert b.status_code == 409
    from steuerung.models import Entscheidung

    e = Entscheidung.objects.get(pk=nummer)
    dienst.entscheidung_treffen(akteur, e)
    b = post(c, f"/api/entscheidungen/{nummer}/bestaetigen/", anfrage_kennung="b2")
    assert (
        b.status_code == 200 and b.json()["entscheidung"]["bestaetigt"][0]["projekt"] == "synth-a"
    )
    nochmal = post(c, f"/api/entscheidungen/{nummer}/bestaetigen/", anfrage_kennung="b3")
    assert nochmal.status_code == 409
    assert c.get("/api/entscheidungen/").json()["entscheidungen"][0]["stand"] == 1


def test_ungueltiges_json(freigegeben, bindung):
    c = api(bindung.klartext)
    antwort = c.post(
        f"/api/auftraege/{freigegeben.pk}/uebernehmen/",
        data="kein json",
        content_type="application/json",
    )
    assert antwort.status_code == 400


def test_chat_legt_auftrag_an_und_sieht_alle_offenen(freigegeben, bindung):
    c = api(bindung.klartext)
    neu = post(
        c,
        "/api/auftraege/anlegen/",
        titel="Logo erneuern",
        beschreibung="Altes Logo ersetzen",
        modul="kern",
        prioritaet="niedrig",
        anfrage_kennung="n1",
    )
    assert neu.status_code == 201 and neu.json()["auftrag"]["zustand"] == "ENTWURF"
    doppelt = post(
        c, "/api/auftraege/anlegen/", titel="logo erneuern", beschreibung="x", anfrage_kennung="n2"
    )
    assert (
        doppelt.status_code == 409 and neu.json()["auftrag"]["nummer"] in doppelt.json()["fehler"]
    )
    erzwungen = post(
        c,
        "/api/auftraege/anlegen/",
        titel="logo erneuern",
        beschreibung="x",
        erzwingen=True,
        anfrage_kennung="n3",
    )
    assert erzwungen.status_code == 201
    falsches_modul = post(
        c,
        "/api/auftraege/anlegen/",
        titel="Anderes",
        beschreibung="x",
        modul="gibtsnicht",
        anfrage_kennung="n4",
    )
    assert falsches_modul.status_code == 400
    alle = c.get("/api/auftraege/").json()["auftraege"]
    assert len(alle) == 3 and {a["zustand"] for a in alle} == {"FREIGEGEBEN", "ENTWURF"}
    meine = c.get("/api/auftraege/?nur_meine=1").json()["auftraege"]
    assert [a["nummer"] for a in meine] == [freigegeben.nummer]


def test_projekt_lesen(projekt, bindung):
    antwort = api(bindung.klartext).get("/api/projekt/").json()
    assert antwort["projekt"]["kennung"] == "synth-a"
    assert antwort["projekt"]["module"] == [{"kennung": "kern", "name": "Kern"}]
    assert antwort["projekt"]["arbeitsorte"][0]["kennung"] == "repo"

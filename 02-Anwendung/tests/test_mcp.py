"""MCP-Endpunkt: jedes KI-Programm verbindet sich mit einem geheimen Link; gleiche Regeln."""

import json

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Arbeitsort, Auftrag, ChatBindung, OrdnerAnfrage


def _rpc(client, schluessel, methode, params=None, rid: int | None = 1, **kopf):
    nachricht = {"jsonrpc": "2.0", "method": methode, "params": params or {}}
    if rid is not None:
        nachricht["id"] = rid
    return client.post(
        reverse("mcp_link", kwargs={"schluessel": schluessel}),
        data=json.dumps(nachricht),
        content_type="application/json",
        **kopf,
    )


def _werkzeug(client, schluessel, werkzeug, /, **argumente):
    antwort = _rpc(client, schluessel, "tools/call", {"name": werkzeug, "arguments": argumente})
    assert antwort.status_code == 200
    ergebnis = antwort.json()["result"]
    return ergebnis["isError"], json.loads(ergebnis["content"][0]["text"])


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def test_ohne_gueltigen_schluessel_abgewiesen(client, pc_zugang):
    assert client.get(reverse("mcp")).status_code == 405
    assert _rpc(client, "kipm_x_y", "initialize").status_code == 401
    pc_zugang.widerrufen_am = pc_zugang.angelegt_am
    pc_zugang.save()
    assert _rpc(client, pc_zugang.klartext, "initialize").status_code == 401


def test_fremde_herkunft_abgewiesen(client, pc_zugang):
    antwort = _rpc(client, pc_zugang.klartext, "ping", HTTP_ORIGIN="https://boese.example")
    assert antwort.status_code == 403


def test_initialize_werkzeugliste_und_benachrichtigung(client, pc_zugang):
    antwort = _rpc(client, pc_zugang.klartext, "initialize", {"protocolVersion": "2025-06-18"})
    ergebnis = antwort.json()["result"]
    assert ergebnis["protocolVersion"] == "2025-06-18" and "tools" in ergebnis["capabilities"]
    assert "ordner" in ergebnis["instructions"]
    unbekannt = _rpc(client, pc_zugang.klartext, "initialize", {"protocolVersion": "1999-01-01"})
    assert unbekannt.json()["result"]["protocolVersion"] == "2025-06-18"

    assert (
        _rpc(client, pc_zugang.klartext, "notifications/initialized", rid=None).status_code == 202
    )

    werkzeuge = _rpc(client, pc_zugang.klartext, "tools/list").json()["result"]["tools"]
    namen = {w["name"] for w in werkzeuge}
    assert {"kipm_auftraege", "kipm_uebernehmen", "kipm_ergebnis"} <= namen
    assert not {"kipm_freigeben", "kipm_abnehmen"} & namen
    assert all("projekt" in w["inputSchema"]["properties"] for w in werkzeuge)
    assert {"kipm_projekte", "kipm_planen", "kipm_antworten", "kipm_projekt_anlegen"} <= namen

    fehler = _rpc(client, pc_zugang.klartext, "gibt/es/nicht").json()
    assert fehler["error"]["code"] == -32601


def test_unbekannter_ordner_landet_im_posteingang(client, owner_client, projekt, pc_zugang):
    ordner = r"C:\Projekte\Neu\src"
    fehler, daten = _werkzeug(client, pc_zugang.klartext, "kipm_projekt", ordner=ordner)
    assert fehler and "Posteingang" in daten["fehler"]
    # Wiederholte Versuche erzeugen keinen zweiten Eintrag.
    _werkzeug(client, pc_zugang.klartext, "kipm_auftraege", ordner=ordner + "\\")
    anfrage = OrdnerAnfrage.objects.get()

    seite = owner_client.get(reverse("posteingang")).content.decode()
    assert "Neuer Arbeitsordner" in seite and "Projekt wählen" in seite

    owner_client.post(
        reverse("ordner_zuordnen", kwargs={"anfrage_id": anfrage.pk}), {"projekt": projekt.pk}
    )
    assert not OrdnerAnfrage.objects.exists()
    ort = Arbeitsort.objects.get(pfad=ordner)
    assert ort.projekt == projekt and ort.kennung == "src" and ort.rechner == pc_zugang.name

    fehler, daten = _werkzeug(client, pc_zugang.klartext, "kipm_projekt", ordner=ordner)
    assert not fehler and daten["projekt"]["kennung"] == projekt.kennung


def test_ordner_verwerfen(client, owner_client, pc_zugang):
    _werkzeug(client, pc_zugang.klartext, "kipm_projekt", ordner="D:/irgendwo")
    anfrage = OrdnerAnfrage.objects.get()
    owner_client.post(reverse("ordner_verwerfen", kwargs={"anfrage_id": anfrage.pk}))
    assert not OrdnerAnfrage.objects.exists()


def test_arbeitsablauf_ueber_mcp(client, projekt, freigegeben, pc_zugang):
    ordner = "C:/Projekte/synth-a"
    ort = projekt.arbeitsorte.first()
    ort.pfad = ordner
    ort.save()
    schluessel = pc_zugang.klartext
    nr = freigegeben.nummer

    fehler, daten = _werkzeug(client, schluessel, "kipm_auftraege", ordner=ordner)
    assert not fehler and [a["nummer"] for a in daten["auftraege"]] == [nr]

    fehler, daten = _werkzeug(client, schluessel, "kipm_lesen", ordner=ordner, auftrag=nr)
    assert not fehler and daten["auftrag"]["abnahmekriterien"]

    fehler, daten = _werkzeug(
        client, schluessel, "kipm_uebernehmen", ordner=ordner, auftrag=nr, revision=1
    )
    assert fehler and "veraltet" in daten["fehler"].lower()
    fehler, daten = _werkzeug(
        client, schluessel, "kipm_uebernehmen", ordner=ordner, auftrag=nr, revision=2
    )
    assert not fehler and daten["auftrag"]["zustand"] == Auftrag.IN_ARBEIT

    fehler, _ = _werkzeug(
        client, schluessel, "kipm_fortschritt", ordner=ordner, auftrag=nr, revision=2, text="Hälfte"
    )
    assert not fehler
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_ergebnis",
        ordner=ordner,
        auftrag=nr,
        revision=2,
        zusammenfassung="Umgesetzt",
        dateien=["src/auth/x.py@abc123"],
        pruefungen=["pytest: ok"],
    )
    assert not fehler
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ZUR_PRUEFUNG
    assert freigegeben.bearbeiter_bindung == pc_zugang

    fehler, daten = _werkzeug(client, schluessel, "kipm_lesen", ordner=ordner, auftrag="A-x")
    assert fehler and "Ungültige Nummer" in daten["fehler"]


def test_zugang_loeschen_nur_wenn_unbenutzt(owner_client, akteur, projekt, freigegeben, bindung):
    unbenutzt, _ = dienst.bindung_anlegen(akteur, projekt, name="Nie benutzt")
    seite = owner_client.get(reverse("verbindungen")).content.decode()
    assert "Löschen" in seite

    owner_client.post(reverse("bindung_loeschen", kwargs={"bindung_id": unbenutzt.pk}))
    assert not ChatBindung.objects.filter(pk=unbenutzt.pk).exists()

    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), freigegeben, 2)
    with pytest.raises(dienst.DienstFehler, match="Archiv"):
        dienst.bindung_loeschen(akteur, bindung)
    owner_client.post(reverse("bindung_widerrufen", kwargs={"bindung_id": bindung.pk}))
    seite = owner_client.get(reverse("verbindungen")).content.decode()
    assert "Archiv: 1" in seite and "bleibt für den Verlauf" in seite


def test_schluessel_auch_als_kopfzeile(client, pc_zugang):
    antwort = client.post(
        reverse("mcp"),
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {pc_zugang.klartext}",
    )
    assert antwort.status_code == 200 and antwort.json()["result"] == {}


def test_assistent_plant_issues_und_arbeitspakete(client, owner, projekt, projekt_b, pc_zugang):
    k = pc_zugang.klartext
    fehler, daten = _werkzeug(client, k, "kipm_projekte")
    assert not fehler and {p["kennung"] for p in daten["projekte"]} == {"synth-a", "synth-b"}

    fehler, daten = _werkzeug(
        client, k, "kipm_projekt_anlegen", name="Nexus Web", module=["Kern", "ui: Oberfläche"]
    )
    assert not fehler and daten["projekt"]["kennung"] == "nexus-web"
    assert [m["kennung"] for m in daten["projekt"]["module"]] == ["kern", "ui"]
    fehler, _ = _werkzeug(client, k, "kipm_modul_anlegen", projekt="nexus-web", name="Export")
    assert not fehler

    fehler, daten = _werkzeug(
        client,
        k,
        "kipm_auftrag_anlegen",
        titel="Login hängt",
        text="Spinner bleibt stehen",
        art="issue",
    )
    assert fehler and "Welches Projekt" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        k,
        "kipm_auftrag_anlegen",
        projekt="synth-a",
        titel="Login hängt",
        text="Spinner bleibt stehen",
        art="issue",
    )
    assert not fehler and daten["auftrag"]["art"] == "issue"
    nr = daten["auftrag"]["nummer"]
    assert daten["auftrag"]["fuer_freigabe_fehlt"]

    fehler, daten = _werkzeug(client, k, "kipm_auftraege", art="issue")
    assert not fehler and [a["nummer"] for a in daten["auftraege"]] == [nr]

    fehler, daten = _werkzeug(
        client,
        k,
        "kipm_planen",
        auftrag=nr,
        ziel="Login endet nach höchstens 3 s",
        nicht_ziele="keine",
        arbeitsort="repo",
        schreibumfang=["src/login/**"],
        abnahmekriterien=["Spinner verschwindet", "Fehlertext sichtbar"],
        pruefer=owner.get_username(),
    )
    assert not fehler, daten
    assert daten["auftrag"]["fuer_freigabe_fehlt"] == []
    assert daten["auftrag"]["beschreibung"] == "Spinner bleibt stehen"  # unverändert übernommen
    auftrag = Auftrag.objects.get(pk=daten["auftrag"]["id"])
    assert auftrag.zustand == Auftrag.ENTWURF and dienst.freigabefaehig(auftrag)

    akteur = dienst.Akteur.aus_bindung(pc_zugang)
    with pytest.raises(dienst.KeinZugriff):
        dienst.auftrag_freigeben(akteur, auftrag)


def test_assistent_beantwortet_rueckfrage(client, akteur, projekt, freigegeben, bindung, pc_zugang):
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), freigegeben, 2)
    dienst.rueckfrage_melden(dienst.Akteur.aus_bindung(bindung), freigegeben, 2, "Welche Sprache?")
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_antworten", auftrag=freigegeben.nummer, text="Deutsch"
    )
    assert not fehler and daten["auftrag"]["zustand"] == Auftrag.IN_ARBEIT

"""A-2: Meldevertrag kipm-stand/1, Stand je Modul, getrennte menschliche Bestätigung, neue
Unterlagenversion über MCP, Kontext je Modul und der synthetische Melder
``03-Server/skripte/kipm_melden.py``, real gegen einen laufenden Testserver ausgeführt.

Synthetisches Projekt „Delta“ mit zwei Modulen; keine privaten Projektdaten."""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Installation, Nachweis, Unterlagenzuordnung
from tests.test_mcp import _werkzeug

MELDER = Path(__file__).resolve().parents[2] / "03-Server" / "skripte" / "kipm_melden.py"


@pytest.fixture
def delta(akteur):
    p = dienst.projekt_registrieren(
        akteur,
        kennung="delta",
        name="Delta",
        module=[("oberflaeche", "Oberfläche"), ("schnittstelle", "Schnittstelle")],
    )
    akte.umgebung_anlegen(akteur, p, name="Lokal", art="lokal")
    akte.umgebung_anlegen(akteur, p, name="Test", art="test")
    return p


@pytest.fixture
def chat(akteur, delta):
    b, schluessel = dienst.bindung_anlegen(akteur, delta, name="Modulchat Delta")
    b.klartext = schluessel
    return b


@pytest.fixture
def auftrag(akteur, delta):
    a = dienst.wunsch_erfassen(
        akteur,
        delta,
        titel="Karten",
        beschreibung="b",
        modul=delta.module.get(kennung="oberflaeche"),
    )
    return a


def _melden(client, schluessel, **felder):
    return _werkzeug(client, schluessel, "kipm_stand_melden", **felder)


def _bericht(meldung_id, version, modul="oberflaeche", **weitere):
    return {
        "meldung_id": meldung_id,
        "version": version,
        "quelle": "uv run pytest",
        "modul": modul,
        "umsetzung": {"umfang": "Karten gleich hoch"},
        "tests": [{"umfang": "Oberflächentests", "ergebnis": "bestanden", "umgebung": "lokal"}],
        **weitere,
    }


def test_zwei_module_melden_uebersicht_und_neuer_chat(client, owner_client, chat, auftrag):
    fehler, daten = _melden(
        client,
        chat.klartext,
        **_bericht("m-1", "abc1111", auftrag=auftrag.nummer, revision=1),
        installation={"umgebung": "test", "komponente": "app", "version": "abc1111"},
        einschraenkungen="Nur Chrome geprüft",
        beobachtet_am="2026-11-02T10:00",
    )
    assert not fehler, daten
    assert daten["vertrag"] == "kipm-stand/1" and len(daten["nachweise"]) == 2
    fehler, daten = _melden(
        client,
        chat.klartext,
        **{
            **_bericht("m-2", "def2222", modul="schnittstelle"),
            "tests": [{"umfang": "API-Tests", "ergebnis": "teilweise", "umgebung": "test"}],
        },
    )
    assert not fehler, daten
    staende = _werkzeug(client, chat.klartext, "kipm_modulstand")[1]["staende"]
    ober = next(s for s in staende if s["modul"] == "oberflaeche")
    assert ober["umsetzung"]["version"] == "abc1111"
    assert ober["umsetzung"]["auftrag"] == auftrag.nummer and ober["umsetzung"]["revision"] == 1
    assert ober["umsetzung"]["quelle"] == "uv run pytest"
    assert datetime.fromisoformat(ober["umsetzung"]["beobachtet"]) == timezone.make_aware(
        datetime(2026, 11, 2, 10, 0)
    )
    assert ober["umsetzung"]["bestaetigt"] is None
    assert ober["einschraenkungen"] == "Nur Chrome geprüft"
    assert ober["installationen"][0]["zustand"] == "gemeldet"
    schnitt = next(s for s in staende if s["modul"] == "schnittstelle")
    assert "Test teilweise in Test (API-Tests)" in schnitt["luecken"]
    gesamt = next(s for s in staende if s["modul"] is None)
    assert "keine eigene Meldung; Untermodule haben gemeldet" in gesamt["luecken"]
    # Neuer Chat: Kontext enthält beide Module mit Commit und Quelle.
    kontext = _werkzeug(client, chat.klartext, "kipm_projektkontext")[1]
    text = kontext["text"]
    assert "Stand je Modul" in text and "abc1111" in text and "def2222" in text
    assert f"{auftrag.nummer} Rev 1" in text and "nicht bestätigt" in text
    # Oberfläche: Überblick zeigt den Stand je Modul, gemeldet und nicht bestätigt.
    seite = owner_client.get(reverse("projekt_detail", kwargs={"kennung": "delta"})).content
    seite = seite.decode()
    assert "Stand je Modul" in seite and "gemeldet, nicht bestätigt" in seite


def test_bestaetigung_getrennt_und_nur_durch_anderen_menschen(
    client, owner_client, akteur, chat, delta, zweiter_mensch
):
    _melden(client, chat.klartext, **_bericht("m-1", "abc1111"))
    n = Nachweis.objects.get(meldung="m-1", art="umsetzung")
    ki = dienst.Akteur.aus_bindung(chat, projekt=delta)
    with pytest.raises(dienst.KeinZugriff):
        akte.nachweis_bestaetigen(ki, n)
    werkzeuge = [
        w["name"]
        for w in _werkzeug.__globals__["_rpc"](client, chat.klartext, "tools/list").json()[
            "result"
        ]["tools"]
    ]
    assert not [w for w in werkzeuge if "bestaetig" in w and w != "kipm_bestaetigen"]
    # Auftragsabnahme oder Meldung bestätigt nichts; Fred bestätigt in der Oberfläche.
    owner_client.post(reverse("nachweis_bestaetigen", kwargs={"kennung": "delta", "pk": n.pk}))
    n.refresh_from_db()
    assert n.bestaetigt_von == akteur.benutzer
    with pytest.raises(dienst.Konflikt):
        akte.nachweis_bestaetigen(akteur, n)
    # Eigene Meldung eines Menschen bestätigt ein anderer Mensch.
    eigen = akte.stand_melden(
        akteur, delta, meldung_id="m-fred", version="x1", quelle="Hand", umsetzung={}
    )
    eigen_n = Nachweis.objects.get(meldung="m-fred")
    with pytest.raises(dienst.KeinZugriff):
        akte.nachweis_bestaetigen(akteur, eigen_n)
    akte.nachweis_bestaetigen(dienst.Akteur.aus_benutzer(zweiter_mensch), eigen_n)
    assert eigen["wiederholt"] is False


def test_neuer_commit_widerspruch_und_korrektur(client, chat, delta):
    _melden(client, chat.klartext, **_bericht("m-1", "v1", beobachtet_am="2026-11-01T09:00"))
    _melden(
        client,
        chat.klartext,
        meldung_id="m-2",
        version="v2",
        quelle="Build",
        modul="oberflaeche",
        umsetzung={"umfang": "Folgecommit"},
        beobachtet_am="2026-11-02T09:00",
    )
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    assert ober["umsetzung"].version == "v2"
    assert not ober["tests"][0]["fuer_aktuell"]
    assert any("gilt für v1, nicht für den gemeldeten Stand v2" in x for x in ober["luecken"])
    # Zwei Tests derselben Version und Umgebung mit verschiedenem Ergebnis: widersprüchlich.
    for nr, ergebnis in ((3, "bestanden"), (4, "fehlgeschlagen")):
        _melden(
            client,
            chat.klartext,
            meldung_id=f"m-{nr}",
            version="v2",
            quelle="CI",
            modul="oberflaeche",
            tests=[{"umfang": "Smoke", "ergebnis": ergebnis, "umgebung": "lokal"}],
            beobachtet_am=f"2026-11-02T1{nr}:00",
        )
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    smoke = next(x for x in ober["tests"] if x["n"].umfang == "Smoke")
    assert smoke["widerspruch"]
    kontext = akte.kontext_erzeugen(dienst.Akteur.aus_bindung(chat, projekt=delta), delta)
    assert "widersprüchliche Tests in Lokal (Smoke)" in kontext.teile[0]
    # Korrektur: der falsche Test wird ersetzt, bleibt aber mit Herkunft erhalten.
    fehler, daten = _melden(
        client,
        chat.klartext,
        meldung_id="m-5",
        version="v2",
        quelle="CI erneut",
        modul="oberflaeche",
        tests=[{"umfang": "Smoke", "ergebnis": "bestanden", "umgebung": "lokal"}],
        korrigiert="m-4",
        beobachtet_am="2026-11-02T16:00",
    )
    assert not fehler, daten
    alt = Nachweis.objects.get(meldung="m-4")
    assert alt.korrigiert_durch == "m-5" and alt.ergebnis == "fehlgeschlagen"
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    smoke = next(x for x in ober["tests"] if x["n"].umfang == "Smoke")
    assert not smoke["widerspruch"] and smoke["n"].ergebnis == "bestanden"


def test_wiederholung_und_vertauschte_reihenfolge(client, akteur, chat, delta):
    neu = _bericht("m-neu", "v2", beobachtet_am="2026-11-02T12:00")
    neu["installation"] = {"umgebung": "test", "komponente": "app", "version": "v2"}
    alt = _bericht("m-alt", "v1", beobachtet_am="2026-11-01T12:00")
    alt["installation"] = {"umgebung": "test", "komponente": "app", "version": "v1"}
    assert not _melden(client, chat.klartext, **neu)[0]
    # Die ältere Meldung kommt später an (Offline-Nachlieferung): verdrängt nichts.
    assert not _melden(client, chat.klartext, **alt)[0]
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    assert ober["umsetzung"].version == "v2"
    aktuell = Installation.objects.exclude(zustand="ersetzt").get(komponente="app")
    assert aktuell.version == "v2"
    # Wiederholung mit gleicher meldung_id: keine Duplikate, auch über einen anderen Zugang.
    zahl = Nachweis.objects.count()
    fehler, daten = _melden(client, chat.klartext, **neu)
    assert not fehler and daten["wiederholt"] is True
    pc, schluessel = dienst.bindung_anlegen(akteur, None, name="Anderer Zugang")
    fehler, daten = _melden(client, schluessel, projekt="delta", **neu)
    assert not fehler and daten["wiederholt"] is True
    assert Nachweis.objects.count() == zahl
    # Gleiche meldung_id mit anderem Inhalt: abgewiesen.
    fehler, daten = _melden(client, schluessel, projekt="delta", **{**neu, "version": "v9"})
    assert fehler and "anderem Inhalt" in daten["fehler"]


def test_fremde_projekte_und_widerrufene_zugaenge(client, akteur, chat, delta, auftrag):
    andere = dienst.projekt_registrieren(
        akteur, kennung="epsilon", name="Epsilon", module=[("x", "X")]
    )
    fremd_chat, schluessel = dienst.bindung_anlegen(akteur, andere, name="Chat Epsilon")
    fehler, daten = _melden(client, schluessel, **_bericht("m-f", "v1", modul="oberflaeche"))
    assert fehler and "gibt es in diesem Projekt nicht" in daten["fehler"]
    fehler, daten = _melden(
        client, schluessel, **_bericht("m-g", "v1", modul="x", auftrag=auftrag.nummer)
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."
    assert not Nachweis.objects.filter(projekt=delta).exists()
    chat.widerrufen_am = chat.angelegt_am
    chat.save()
    antwort = _werkzeug.__globals__["_rpc"](client, chat.klartext, "tools/list")
    assert antwort.status_code == 401


def test_textunterlage_neue_version_ueber_mcp(client, akteur, chat, delta, auftrag):
    u = akte.text_anlegen(akteur, delta, titel="Schnittstellenvertrag", text="Version eins")
    Unterlagenzuordnung.objects.create(
        projekt=delta, version=u.versionen.get(), auftrag=auftrag, auftrag_revision=1
    )
    argumente = {
        "unterlage": u.kennung,
        "erwartete_version": 1,
        "text": "Version zwei",
        "grund": "Feld ergänzt",
        "anfrage_kennung": "uv-1",
    }
    fehler, daten = _werkzeug(client, chat.klartext, "kipm_unterlage_version", **argumente)
    assert not fehler, daten
    assert daten["unterlage"] == {
        "kennung": u.kennung,
        "version": 2,
        "rolle": "entwurf",
        "sha256": daten["unterlage"]["sha256"],
        "herkunft": "Entwurf von Modulchat Delta; Grund: Feld ergänzt",
    }
    fehler, daten = _werkzeug(client, chat.klartext, "kipm_unterlage_version", **argumente)
    assert not fehler and daten["wiederholt"] is True
    fehler, daten = _werkzeug(
        client, chat.klartext, "kipm_unterlage_version", **{**argumente, "anfrage_kennung": "uv-2"}
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        chat.klartext,
        "kipm_unterlage_version",
        **{**argumente, "anfrage_kennung": "uv-3", "erwartete_version": 2, "grund": ""},
    )
    assert fehler and "Grund" in daten["fehler"]
    u.refresh_from_db()
    assert u.aktuelle_nr == 2 and u.versionen.count() == 2
    assert Unterlagenzuordnung.objects.get(auftrag=auftrag).version.nummer == 1


def test_kontext_je_modul_und_unterlagenart(client, akteur, chat, delta, auftrag):
    _melden(client, chat.klartext, **_bericht("m-1", "abc1111"))
    _melden(client, chat.klartext, **_bericht("m-2", "def2222", modul="schnittstelle"))
    dienst.wunsch_erfassen(
        akteur,
        delta,
        titel="API-Fehler",
        beschreibung="b",
        modul=delta.module.get(kennung="schnittstelle"),
    )
    u = akte.text_anlegen(akteur, delta, titel="Notiz", text="Inhalt")
    akte.ki_freigabe(akteur, u, True)
    fehler, daten = _werkzeug(client, chat.klartext, "kipm_projektkontext", modul="oberflaeche")
    assert not fehler, daten
    text = daten["text"]
    assert "Ausgewählt: Modul Oberfläche" in text and daten["manifest"]["modul"] == "oberflaeche"
    assert "abc1111" in text and "def2222" not in text
    assert "Karten" in text and "API-Fehler" not in text
    assert "Notiz, Version 1 (Entwurf), eingefügter Text" in text


def _melder(url, schluessel, *argumente, schlange):
    umgebung = {**os.environ, "KIPM_URL": url, "KIPM_SCHLUESSEL": schluessel}
    return subprocess.run(
        [
            sys.executable,
            str(MELDER),
            *argumente,
            "--warteschlange",
            str(schlange),
            "--versuche",
            "1",
            "--zeit",
            "5",
        ],
        capture_output=True,
        text=True,
        env=umgebung,
        timeout=60,
    )


@pytest.mark.django_db(transaction=True)
def test_synthetischer_melder_real_gegen_laufenden_server(live_server, akteur, tmp_path):
    p = dienst.projekt_registrieren(
        akteur, kennung="delta", name="Delta", module=[("oberflaeche", "Oberfläche")]
    )
    akte.umgebung_anlegen(akteur, p, name="Lokal", art="lokal")
    _, schluessel = dienst.bindung_anlegen(akteur, p, name="CI Delta")
    schlange = tmp_path / "schlange"
    bericht = tmp_path / "bericht.json"
    bericht.write_text(json.dumps(_bericht("ci-1", "abc1111")), encoding="utf-8")
    erst = _melder(live_server.url, schluessel, "senden", str(bericht), schlange=schlange)
    assert erst.returncode == 0, erst.stdout + erst.stderr
    assert json.loads(erst.stdout)["ergebnis"] == "gespeichert"
    # Antwort verloren nach möglicher Speicherung: dieselbe Meldung erneut senden.
    zweit = _melder(live_server.url, schluessel, "senden", str(bericht), schlange=schlange)
    assert zweit.returncode == 0 and json.loads(zweit.stdout)["wiederholt"] is True
    # Server nicht erreichbar: Meldung landet in der Warteschlange, ohne Schlüssel.
    offline = tmp_path / "offline.json"
    offline.write_text(
        json.dumps(_bericht("ci-0", "v0", beobachtet_am="2026-11-01T08:00")), encoding="utf-8"
    )
    weg = _melder("http://127.0.0.1:9", schluessel, "senden", str(offline), schlange=schlange)
    assert weg.returncode == 3
    gespeichert = (schlange / "ci-0.json").read_text(encoding="utf-8")
    assert schluessel not in gespeichert
    # Nachliefern: ältere Meldung kommt an, verdrängt den neueren Stand nicht.
    nach = _melder(live_server.url, schluessel, "nachliefern", schlange=schlange)
    assert nach.returncode == 0, nach.stdout + nach.stderr
    assert not list(schlange.glob("*.json"))
    ober = next(z for z in akte.modulstaende(p) if z["name"] == "Oberfläche")
    assert ober["umsetzung"].version == "abc1111"
    assert Nachweis.objects.filter(meldung="ci-1").count() == 2
    # Abgewiesene Meldung (anderer Inhalt unter gleicher ID) wandert nicht zurück in die Schlange.
    bericht.write_text(json.dumps(_bericht("ci-1", "zzz9999")), encoding="utf-8")
    falsch = _melder(live_server.url, schluessel, "senden", str(bericht), schlange=schlange)
    assert falsch.returncode == 2 and not list(schlange.glob("*.json"))


def test_fehlende_meldung_nach_ergebnis_sichtbar(client, akteur, chat, delta, auftrag):
    dienst.auftrag_bearbeiten(
        akteur,
        auftrag,
        titel="Karten",
        beschreibung="b",
        ziel="Z",
        nicht_ziele="keine",
        arbeitsort=None,
        schreibumfang="src/**",
        quellen="",
        abnahmekriterien="K",
        prioritaet="mittel",
        modul=auftrag.modul,
    )
    ort = dienst.arbeitsort_anlegen(akteur, delta, kennung="repo", name="Repo")
    dienst.arbeitsort_setzen(akteur, auftrag, ort)
    dienst.pruefer_festlegen(akteur, auftrag, akteur.benutzer)
    auftrag.refresh_from_db()
    dienst.auftrag_freigeben(akteur, auftrag)
    ki = dienst.Akteur.aus_bindung(chat, projekt=delta)
    dienst.auftrag_uebernehmen(ki, auftrag, auftrag.revision_nr)
    dienst.ergebnis_einreichen(
        ki,
        auftrag,
        auftrag.revision_nr,
        zusammenfassung="fertig",
        dateien="a.py@abc",
        pruefungen="t",
    )
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    assert f"{auftrag.nummer}: Ergebnis eingereicht, aber kein Stand gemeldet" in ober["luecken"]
    _melden(client, chat.klartext, **_bericht("m-1", "abc", auftrag=auftrag.nummer))
    ober = next(z for z in akte.modulstaende(delta) if z["name"] == "Oberfläche")
    assert not any("kein Stand gemeldet" in x for x in ober["luecken"])

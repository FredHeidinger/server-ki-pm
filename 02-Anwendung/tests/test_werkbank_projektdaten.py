"""KIPM-37 (KW-14): Lesezugang „Projektdaten“ für die Werkbank.

Erwartungen aus dem Auftrag und dem Entwurf der Werkbank (``docs/kipm/projektdaten-1/ENTWURF.md``,
Beispiele ``seite-1.json`` und ``seite-2.json``), vor dem Code geschrieben:

1. ``GET /api/werkbank/v1/projektdaten/?projektbindung_id=<id>[&seite=<zeiger>]`` liefert unter
   ``kipm-werkbank/2`` alle neun Teile, auf Seite 1 das Projektprofil, auf Folgeseiten ``null``;
   höchstens 100 Einträge je Teil und Seite, jeder Eintrag mit ``id``; ``stand`` ist auf allen
   Seiten gleich. Die Felder der Beispieleinträge stehen in der Antwort.
2. Ändert sich der Stand zwischen zwei Seiten, lehnt der Server die Folgeseite mit 409 ab.
3. Fremde oder unbekannte Projektbindung: 404 ``NICHT_GEFUNDEN``.
4. Der Abruf ändert nichts: weder letzten Kontakt noch Zuweisung, Ausführung, Erlaubnis oder
   Verlauf.
5. Keine Zugangsschlüssel, MCP-Links oder Kennwörter in der Antwort, auch wenn sie in Projekttexten
   stehen (der Testschlüssel aus dem Beispiel wird entfernt).
"""

import dataclasses
import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone

from steuerung import akte, dienst, werkbank
from steuerung.models import (
    Ereignis,
    Projekt,
    WerkbankAusfuehrung,
    WerkbankChatDelegation,
    WerkbankInstanz,
    WerkbankZuweisung,
)
from tests import werkbank_hilfen as h
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db

V2 = "kipm-werkbank/2"
PFAD = h.BASIS + "projektdaten/"
TEILE = {
    "module",
    "arbeitsorte",
    "auftraege",
    "entscheidungen",
    "meilensteine",
    "texte",
    "links",
    "verlauf",
    "erlaubnisse",
}
# Aus dem Beispiel der Werkbank zusammengesetzt (Spiegelprüfung: kein Schlüssel als Literal).
BEISPIELSCHLUESSEL = "kipm_" + "0123456789ab" + "_GEHEIMERSCHLUESSEL"
BEISPIELE = (
    Path(__file__).resolve().parent.parent
    / "vertrag"
    / "kipm-werkbank-projektdaten-1"
    / "beispiele"
)
CAP = {"waehrung": "USD", "geld_mikro": 10_000_000, "zeit_ms": 600_000, "aufrufe": 10}


def _kopf(instanz, vertrag=V2):
    return {"HTTP_AUTHORIZATION": f"Bearer {instanz.klartext}", "HTTP_X_KIPM_VERTRAG": vertrag}


def _abrufen(client, instanz, pb_id, seite=None, vertrag=V2):
    daten = {"projektbindung_id": pb_id, **({"seite": seite} if seite else {})}
    return client.get(PFAD, daten, **_kopf(instanz, vertrag))


def _alle_seiten(client, instanz, pb_id):
    seiten, zeiger = [], None
    while True:
        r = _abrufen(client, instanz, pb_id, zeiger)
        assert r.status_code == 200, r.content
        seiten.append(r.json())
        zeiger = seiten[-1]["seite"]["naechste"]
        if not zeiger:
            return seiten


def _beispiel(name):
    """Die Beispiele der veröffentlichten Fassung im Server-Repository."""
    return json.loads((BEISPIELE / name).read_text(encoding="utf-8"))


@pytest.fixture
def testalpha(akteur, projekt, bindung, w_instanz, w_pbindung):
    """Die Lage aus ``seite-1.json`` nachgebaut: Profil, Arbeitsort, Auftrag, Entscheidung, Text
    mit eingeschleustem Testschlüssel, Verlauf und eine W1-Erlaubnis."""
    akte.profil_bearbeiten(
        akteur,
        projekt,
        erwartete_revision=None,
        beschreibung="Probeprojekt Kassenbuch für den Werkbank-Durchlauf",
        zweck="Barkasse einer Werkstatt",
        phase="Aufbau",
    )
    auftrag = arbeitspaket(akteur, projekt, titel="K-03 Monatsübersicht im Kassenbuch")
    dienst.auftrag_freigeben(akteur, auftrag)
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Nur Standardbibliothek")
    akte.text_anlegen(
        akteur,
        projekt,
        titel="Auftragsregeln",
        text=f"Beträge immer in Cent. Zugang: {BEISPIELSCHLUESSEL}",
    )
    werkbank.delegation_erteilen(
        akteur,
        projekt,
        bindung,
        aktionen=["auftrag_anlegen", "freigeben"],
        ziele=[w_pbindung],
        budget=CAP,
        gueltig_bis=timezone.now() + timedelta(days=7),
        standard_ziel=w_pbindung,
    )
    projekt.refresh_from_db()
    return projekt


# --- 1. Inhalt nach dem Beispiel ------------------------------------------------------------


def test_seite_eins_hat_alle_teile_das_profil_und_die_felder_des_beispiels(
    client, testalpha, w_instanz, w_pbindung
):
    r = _abrufen(client, w_instanz, w_pbindung.pk)
    assert r.status_code == 200, r.content
    assert r["Cache-Control"] == "no-store"
    d = r.json()
    assert d["vertrag"] == V2
    assert d["art"] == "projektdaten"
    assert d["schema_version"] == 1
    assert d["projektbindung_id"] == w_pbindung.pk
    assert set(d["teile"]) == TEILE
    assert set(d["stand"]) == {"inhalt_revision", "ereignis_bis", "erzeugt"}
    assert d["stand"]["inhalt_revision"] == testalpha.inhalt_revision
    assert d["stand"]["ereignis_bis"] == akte.obere_grenze(testalpha)
    assert d["seite"]["naechste"] is None

    beispiel = _beispiel("seite-1.json")
    assert set(beispiel["projekt"]) <= set(d["projekt"])
    assert d["projekt"]["kennung"] == testalpha.kennung
    assert d["projekt"]["zweck"] == "Barkasse einer Werkstatt"
    for teil, eintraege in beispiel["teile"].items():
        if not eintraege:
            continue
        assert d["teile"][teil], f"Teil {teil} ist leer"
        for eintrag in d["teile"][teil]:
            assert set(eintraege[0]) <= set(eintrag), (teil, set(eintraege[0]) - set(eintrag))
    for teil, eintraege in d["teile"].items():
        assert all("id" in e for e in eintraege), teil

    (text,) = d["teile"]["texte"]
    assert text["ki_lesbar"] is True
    assert "Beträge immer in Cent." in text["inhalt"]
    (auftrag,) = d["teile"]["auftraege"]
    assert auftrag["zustand"] == "FREIGEGEBEN"
    assert auftrag["abnahmekriterien"] == [
        "Hinweis nennt nächsten Schritt",
        "Kein Fehlercode sichtbar",
    ]
    (erlaubnis,) = d["teile"]["erlaubnisse"]
    assert erlaubnis["aktionen"] == ["auftrag_anlegen", "freigeben"]
    assert erlaubnis["widerrufen"] is False
    assert [e["id"] for e in d["teile"]["verlauf"]] == list(
        Ereignis.objects.filter(projekt=testalpha).order_by("id").values_list("id", flat=True)
    )
    assert {m["kennung"] for m in d["teile"]["module"]} == {"kern"}


def test_auftraege_und_issues_in_allen_zustaenden(client, akteur, projekt, w_instanz, w_pbindung):
    entwurf = dienst.wunsch_erfassen(akteur, projekt, titel="Nur ein Wunsch", beschreibung="x")
    abgebrochen = dienst.wunsch_erfassen(akteur, projekt, titel="Verworfen", beschreibung="y")
    dienst.auftrag_abbrechen(akteur, abgebrochen, grund="doppelt")
    d = _abrufen(client, w_instanz, w_pbindung.pk).json()
    zustaende = {a["id"]: a["zustand"] for a in d["teile"]["auftraege"]}
    assert zustaende == {entwurf.pk: "ENTWURF", abgebrochen.pk: "ABGEBROCHEN"}


def test_archivierter_text_nur_mit_titel_und_fassung(
    client, akteur, projekt, w_instanz, w_pbindung
):
    u = akte.text_anlegen(akteur, projekt, titel="Alt", text="früherer Inhalt")
    akte.unterlage_archivieren(akteur, u, True)
    d = _abrufen(client, w_instanz, w_pbindung.pk).json()
    (text,) = d["teile"]["texte"]
    assert text == {"id": u.pk, "titel": "Alt", "version": 1, "ki_lesbar": False}


def test_links_stehen_im_eigenen_teil(client, akteur, projekt, w_instanz, w_pbindung):
    akte.link_anlegen(akteur, projekt, titel="Handbuch", uri="https://example.org/handbuch")
    d = _abrufen(client, w_instanz, w_pbindung.pk).json()
    assert d["teile"]["texte"] == []
    (link,) = d["teile"]["links"]
    assert link["titel"] == "Handbuch"
    assert link["adresse"] == "https://example.org/handbuch"


# --- Seiten und Stand ---------------------------------------------------------------------


def test_seitenweise_hoechstens_100_je_teil_und_gleicher_stand(
    client, akteur, projekt, w_instanz, w_pbindung
):
    for i in range(130):
        akte.meilenstein_anlegen(akteur, projekt, name=f"Schritt {i:03d}")
    seiten = _alle_seiten(client, w_instanz, w_pbindung.pk)
    assert len(seiten) == 2
    assert seiten[0]["projekt"] is not None
    assert seiten[1]["projekt"] is None
    assert seiten[0]["stand"] == seiten[1]["stand"]
    for s in seiten:
        assert set(s["teile"]) == TEILE
        assert all(len(liste) <= 100 for liste in s["teile"].values())
    meilensteine = [m["id"] for s in seiten for m in s["teile"]["meilensteine"]]
    assert sorted(meilensteine) == sorted(projekt.meilensteine.values_list("id", flat=True))
    assert len(meilensteine) == len(set(meilensteine)) == 130
    verlauf = [e["id"] for s in seiten for e in s["teile"]["verlauf"]]
    assert verlauf == list(
        Ereignis.objects.filter(projekt=projekt).order_by("id").values_list("id", flat=True)
    )


def test_geaenderter_stand_zwischen_zwei_seiten_wird_abgelehnt(
    client, akteur, projekt, w_instanz, w_pbindung
):
    for i in range(101):
        akte.meilenstein_anlegen(akteur, projekt, name=f"Schritt {i:03d}")
    erste = _abrufen(client, w_instanz, w_pbindung.pk).json()
    assert erste["seite"]["naechste"]
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Neu dazwischen")
    r = _abrufen(client, w_instanz, w_pbindung.pk, erste["seite"]["naechste"])
    assert r.status_code == 409
    assert r.json()["fehler"]["code"] == "STAND_GEAENDERT"


def test_ungueltiger_seitenzeiger(client, projekt, w_instanz, w_pbindung):
    r = _abrufen(client, w_instanz, w_pbindung.pk, "kein-zeiger")
    assert r.status_code == 400
    assert r.json()["fehler"]["code"] == "UNGUELTIGE_NACHRICHT"


# --- 3. Fremde Bindung, Zugang, Fassung -----------------------------------------------------


def test_fremde_und_unbekannte_projektbindung_ergeben_404(
    client, akteur, projekt, projekt_b, w_instanz, w_pbindung
):
    fremd, schluessel = werkbank.instanz_anlegen(akteur, name="Andere", projekte=[projekt_b])
    fremd.klartext = schluessel
    fremde_pb = h.bindung_sichern(
        instanz=fremd, projekt=projekt_b, arbeitsort=projekt_b.arbeitsorte.first()
    )
    for pb_id in (fremde_pb.pk, 999_999):
        r = _abrufen(client, w_instanz, pb_id)
        assert r.status_code == 404, pb_id
        assert r.json()["fehler"]["code"] == "NICHT_GEFUNDEN"
    # Projekt der eigenen Bindung nicht mehr erlaubt: ebenfalls nicht gefunden.
    w_instanz.erlaubte_projekte.remove(projekt)
    assert _abrufen(client, w_instanz, w_pbindung.pk).status_code == 404


def test_fehlende_oder_falsche_bindungsangabe(client, projekt, w_instanz, w_pbindung):
    r = client.get(PFAD, **_kopf(w_instanz))
    assert r.status_code == 400
    assert _abrufen(client, w_instanz, "neun").status_code == 400


def test_nur_unter_fassung_2(client, projekt, w_instanz, w_pbindung):
    r = _abrufen(client, w_instanz, w_pbindung.pk, vertrag="kipm-werkbank/1")
    assert r.status_code == 426
    assert r.json()["fehler"]["code"] == "VERTRAG_INKOMPATIBEL"


def test_chat_zugang_und_schreibende_methode_abgewiesen(
    client, projekt, bindung, w_instanz, w_pbindung
):
    r = client.get(
        PFAD,
        {"projektbindung_id": w_pbindung.pk},
        HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}",
        HTTP_X_KIPM_VERTRAG=V2,
    )
    assert r.status_code == 403
    r = client.post(PFAD + f"?projektbindung_id={w_pbindung.pk}", **_kopf(w_instanz))
    assert r.status_code == 405


# --- 4. Der Abruf ändert nichts -------------------------------------------------------------


def test_abruf_aendert_weder_kontakt_noch_zuweisung_ausfuehrung_erlaubnis_noch_verlauf(
    client, akteur, projekt, bindung, w_instanz, w_pbindung, w_ausfuehrung
):
    werkbank.delegation_erteilen(
        akteur,
        projekt,
        bindung,
        aktionen=["auftrag_anlegen"],
        ziele=[w_pbindung],
        budget=CAP,
        gueltig_bis=timezone.now() + timedelta(days=1),
        standard_ziel=w_pbindung,
    )
    WerkbankInstanz.objects.filter(pk=w_instanz.pk).update(letzter_kontakt=None)

    def bild():
        return (
            WerkbankInstanz.objects.get(pk=w_instanz.pk).letzter_kontakt,
            list(WerkbankZuweisung.objects.values_list("pk", "version", "kontext_sha256")),
            list(WerkbankAusfuehrung.objects.values_list("pk", "zustand")),
            list(WerkbankChatDelegation.objects.values_list("pk", "revision", "reserviert")),
            Ereignis.objects.count(),
            Projekt.objects.get(pk=projekt.pk).inhalt_revision,
        )

    vorher = bild()
    assert _abrufen(client, w_instanz, w_pbindung.pk).status_code == 200
    assert bild() == vorher


# --- 5. Keine Geheimnisse -------------------------------------------------------------------


def test_keine_schluessel_mcp_links_oder_kennwoerter(
    client, akteur, owner, projekt, bindung, w_instanz, w_pbindung
):
    mcp_link = f"https://kipm.example/mcp/{bindung.klartext}/"
    a = dienst.wunsch_erfassen(
        akteur,
        projekt,
        titel="Zugang testen",
        beschreibung=f"Schlüssel {bindung.klartext} und Link {mcp_link} und {w_instanz.klartext}",
    )
    akte.entscheidung_eintragen(
        akteur, projekt, entscheidung=f"Werkbank nutzt {w_instanz.klartext}", warum=mcp_link
    )
    akte.text_anlegen(akteur, projekt, titel="Notiz", text=f"Testschlüssel {BEISPIELSCHLUESSEL}")
    akte.meilenstein_anlegen(akteur, projekt, name="Start")
    chat = dataclasses.replace(
        dienst.Akteur.aus_bindung(bindung), fred_sagte=f"nimm {bindung.klartext}"
    )
    akte.entscheidung_eintragen(chat, projekt, entscheidung="Mit Freds Worten")
    assert a.pk

    roh = b"".join(
        json.dumps(s, ensure_ascii=False).encode()
        for s in _alle_seiten(client, w_instanz, w_pbindung.pk)
    ).decode()
    for geheim in (
        bindung.klartext,
        w_instanz.klartext,
        BEISPIELSCHLUESSEL,
        "/mcp/" + bindung.klartext,
    ):
        assert geheim not in roh
    assert owner.password not in roh
    assert bindung.schluessel_hash not in roh
    assert "[Schlüssel entfernt]" in roh
    assert "Testschlüssel" in roh


# --- Gegenprüfung -----------------------------------------------------------------------


def test_sonderziffer_als_bindung_ist_ungueltig_kein_serverfehler(
    client, projekt, w_instanz, w_pbindung
):
    r = _abrufen(client, w_instanz, "²")
    assert r.status_code == 400
    assert r.json()["fehler"]["code"] == "UNGUELTIGE_NACHRICHT"


def test_zugangsdaten_in_einer_repository_adresse_gehen_nicht_hinaus(
    client, akteur, projekt, w_instanz, w_pbindung
):
    kennwort = "Kenn" + "wort" + "4711"
    dienst.arbeitsort_anlegen(
        akteur,
        projekt,
        kennung="spiegel",
        name="Spiegel",
        repository=f"https://fred:{kennwort}@git.example/x.git",
    )
    roh = _abrufen(client, w_instanz, w_pbindung.pk).content.decode()
    assert kennwort not in roh
    assert "git.example/x.git" in roh


def test_aenderung_waehrend_der_seite_wird_abgelehnt(
    client, akteur, projekt, w_instanz, w_pbindung, monkeypatch
):
    """Alles passt auf eine Seite; ändert ein Chat das Projekt, während die Seite entsteht, darf
    keine gemischte Kopie unter dem alten Stand hinausgehen."""
    from steuerung import werkbank_projektdaten

    echt = werkbank_projektdaten._modul

    def mit_aenderung(m):
        akte.entscheidung_eintragen(akteur, projekt, entscheidung="Während des Abrufs")
        return echt(m)

    monkeypatch.setattr(werkbank_projektdaten, "_modul", mit_aenderung)
    r = _abrufen(client, w_instanz, w_pbindung.pk)
    assert r.status_code == 409
    assert r.json()["fehler"]["code"] == "STAND_GEAENDERT"


@pytest.mark.parametrize(
    ("adresse", "verboten", "bleibt"),
    [
        # Kennwort mit @: der Parser trennt am letzten @ vor dem Pfad.
        (
            "https://fred:Kenn@wort4711@git.example/x.git",
            ["wort4711", "Kenn@"],
            "git.example/x.git",
        ),
        # Token als Kennwort: auch der Benutzername gehört zu den Zugangsdaten.
        ("https://fred:ghp_" + "A" * 24 + "@github.com/o/r.git", ["fred:"], "github.com/o/r.git"),
        # Direkt nach Unterstrich oder Ziffer.
        ("_https://fred:Kennwort4711@host.example/_", ["Kennwort4711"], "host.example/"),
        ("1https://fred:Kennwort4711@host.example", ["Kennwort4711"], "host.example"),
        # Keine Zugangsdaten: ein @ in Abfrage oder Anker bleibt unberührt.
        (
            "https://example.org?mail=fred@example.org",
            ["Zugangsdaten entfernt"],
            "https://example.org?mail=fred@example.org",
        ),
        ("https://host.example#a@b", ["Zugangsdaten entfernt"], "https://host.example#a@b"),
        # Ungewöhnliche, aber mögliche Zeichen im Kennwort.
        ("https://fred:pa'ss^1@host.example/x", ["pa'ss", "ss^1"], "host.example/x"),
        # Zeichen, die in einer Adresse nie roh stehen, beenden sie (HTML, JSON, Markdown).
        (
            '<a href="https://example.com">a@b.de</a>',
            ["Zugangsdaten entfernt"],
            '"https://example.com">a@b.de',
        ),
        (
            '{"url":"https://example.com","mail":"a@b.de"}',
            ["Zugangsdaten entfernt"],
            '"mail":"a@b.de"',
        ),
        (
            "[https://example.com](mailto:a@b.de)",
            ["Zugangsdaten entfernt"],
            "[https://example.com](mailto:a@b.de)",
        ),
    ],
)
def test_zugangsdaten_in_adressen_randfaelle(
    client, akteur, projekt, w_instanz, w_pbindung, adresse, verboten, bleibt
):
    dienst.wunsch_erfassen(akteur, projekt, titel="Adresse", beschreibung=f"Siehe {adresse} bitte")
    roh = json.loads(_abrufen(client, w_instanz, w_pbindung.pk).content)
    (auftrag,) = roh["teile"]["auftraege"]
    text = auftrag["beschreibung"]
    for teil in verboten:
        assert teil not in text, text
    assert bleibt in text, text


def test_ueberlange_bindungsnummer_ist_nicht_gefunden(client, projekt, w_instanz, w_pbindung):
    r = _abrufen(client, w_instanz, "9" * 25)
    assert r.status_code == 404

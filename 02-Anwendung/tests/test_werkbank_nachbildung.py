"""WP9: Die Server-Nachbildung für die Werkbank antwortet nach Vertrag kipm-werkbank/1 und stimmt
mit den Hashregeln und Schemas überein (Beispiele aus ``vertrag/kipm-werkbank-1/beispiele``)."""

import importlib.util
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from steuerung import werkbank_vertrag

ORDNER = Path(__file__).resolve().parent.parent / "vertrag" / "kipm-werkbank-1"
spec = importlib.util.spec_from_file_location("nachbildung", ORDNER / "nachbildung.py")
nachbildung = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nachbildung)

KOPF = {
    "Authorization": f"Bearer {nachbildung.SCHLUESSEL}",
    "X-KIPM-Vertrag": "kipm-werkbank/1",
    "Content-Type": "application/json",
}


@pytest.fixture
def nb():
    zustand, server, basis = nachbildung.starten(0)
    snapshot = json.loads(
        (ORDNER / "beispiele" / "uebernehmen_antwort.gut.json").read_text("utf-8")
    )["snapshot"]
    zustand.zuweisen(25, snapshot)
    yield zustand, basis
    server.shutdown()


def anfrage(basis, pfad, daten=None, kopf=None):
    req = urllib.request.Request(
        basis + pfad,
        data=json.dumps(daten).encode() if daten is not None else None,
        headers=KOPF if kopf is None else kopf,
        method="POST" if daten is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as fehler:
        return fehler.code, json.loads(fehler.read())


def nachricht(art, quelle, nutzlast, op=None):
    n = {
        "vertrag": "kipm-werkbank/1",
        "operation_id": op or str(uuid.uuid4()),
        "art": art,
        "quelle": quelle,
        "nutzlast": nutzlast,
    }
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    return n


def uebernahme(nb_zustand, basis, op=None):
    vor = {
        "server_id": nachbildung.SERVER_ID,
        "werkbank_id": nb_zustand.werkbank_id,
        "projektbindung_id": 1,
        "projektbindung_version": 1,
        "auftrag_id": 25,
        "revision": 1,
        "zuweisung_version": 1,
    }
    n = nachricht(
        "uebernehmen",
        vor,
        {
            "repo_fingerabdruck": "r",
            "konfig_fingerabdruecke": {},
            "faehigkeiten": [],
            "werkbank_version": "1",
        },
        op,
    )
    return n, anfrage(basis, "uebernehmen/", n)


def test_anmeldung_und_vertragskopf(nb):
    _, basis = nb
    status, body = anfrage(basis, "abgleich/", kopf={"X-KIPM-Vertrag": "kipm-werkbank/1"})
    assert status == 401 and werkbank_vertrag.schema_fehler("fehler", body) == []
    status, body = anfrage(basis, "abgleich/", kopf={"Authorization": KOPF["Authorization"]})
    assert status == 426 and werkbank_vertrag.schema_fehler("fehler", body) == []


def test_abgleich_entspricht_dem_schema_und_meldet_unbekannte_ausfuehrungen(nb):
    _, basis = nb
    fremd = str(uuid.uuid4())
    status, body = anfrage(basis, f"abgleich/?ausfuehrungen={fremd}")
    assert status == 200 and werkbank_vertrag.schema_fehler("abgleich_antwort", body) == []
    assert body["konflikte"][0]["ausfuehrung_id"] == fremd and body["zuweisungen"][0]["freigegeben"]


def test_uebernehmen_wiederholung_und_widerspruch(nb):
    zustand, basis = nb
    op = str(uuid.uuid4())
    n, (status, body) = uebernahme(zustand, basis, op)
    assert status == 200 and werkbank_vertrag.schema_fehler("uebernehmen_antwort", body) == []
    assert body["quelle"]["snapshot_sha256"] == werkbank_vertrag.sha256_hex(body["snapshot"])
    status2, body2 = anfrage(basis, "uebernehmen/", n)
    assert status2 == 200 and body2["ergebnis"] == "wiederholt"
    assert body2["quelle"] == body["quelle"]
    n["quelle"]["revision"] = 2
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    status3, fehler = anfrage(basis, "uebernehmen/", n)
    assert status3 == 409 and fehler["fehler"]["code"] == "WIDERSPRUCH_WIEDERHOLUNG"
    status4, _ = uebernahme(zustand, basis)[1]
    assert status4 == 409  # nicht zweimal übernehmen


def test_schritt_hoechstens_eine_autorisierung_je_versuch(nb):
    zustand, basis = nb
    _, (_, ue) = uebernahme(zustand, basis)
    quelle = ue["quelle"]

    def schritt(versuch):
        return nachricht(
            "schritt",
            quelle,
            {
                "schritt": {
                    "ausfuehrung_id": quelle["ausfuehrung_id"],
                    "planfassung": "vorplanung",
                    "paket": "root",
                    "phase": "bauen",
                    "position": 0,
                },
                "versuch_nr": versuch,
                "eingabehash": "a" * 64,
                "wirkung": "schreiben",
                "owner_aktion_id": None,
            },
        )

    erste = anfrage(basis, "schritt/", schritt(1))[1]
    zweite = anfrage(basis, "schritt/", schritt(1))[1]  # andere operation_id, gleicher Versuch
    assert erste["erlaubt"] and not zweite["erlaubt"] and zweite["autorisierung_id"] is None
    for antwort in (erste, zweite):
        assert werkbank_vertrag.schema_fehler("schritt_antwort", antwort) == []


def test_ereignis_idempotent_und_operationsabruf(nb):
    zustand, basis = nb
    _, (_, ue) = uebernahme(zustand, basis)
    eid = str(uuid.uuid4())
    nutzlast = {
        "ereignis_id": eid,
        "sequenz": 1,
        "statusversion": 2,
        "ereignis": {"art": "lebenszeichen", "daten": {}},
    }
    n = nachricht("ereignisse", ue["quelle"], nutzlast)
    status, body = anfrage(basis, "ereignisse/", n)
    assert status == 200 and werkbank_vertrag.schema_fehler("ereignisse_antwort", body) == []
    zweite = anfrage(basis, "ereignisse/", nachricht("ereignisse", ue["quelle"], nutzlast))[1]
    assert zweite["ergebnis"] == "wiederholt"
    status, abruf = anfrage(basis, f"operationen/{n['operation_id']}/")
    assert status == 200 and abruf["status"] == "gespeichert"
    assert werkbank_vertrag.schema_fehler("operation_antwort", abruf) == []
    status, unbekannt = anfrage(basis, f"operationen/{uuid.uuid4()}/")
    assert unbekannt["status"] == "unbekannt"

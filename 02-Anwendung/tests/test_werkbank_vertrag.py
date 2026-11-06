"""Vertrag kipm-werkbank/1: kanonische Darstellung, Hashes, Schemas und Beispiele.

Die erwarteten Werte stehen hier von Hand und stammen aus der Vertragsbeschreibung
(`vertrag/kipm-werkbank-1/VERTRAG.md`), nicht aus dem Code.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from steuerung import werkbank_vertrag as wv

ORDNER = Path(__file__).resolve().parent.parent / "vertrag" / "kipm-werkbank-1"
BEISPIELE = sorted((ORDNER / "beispiele").glob("*.json"))
SCHEMAS = sorted((ORDNER / "schemas").glob("*.schema.json"))

ERWARTETE_SCHEMAS = {
    "gemeinsam",
    "fehler",
    "auftragssnapshot",
    "abgleich_antwort",
    "operation_antwort",
    "uebernehmen_anfrage",
    "uebernehmen_antwort",
    "schritt_anfrage",
    "schritt_antwort",
    "ereignisse_anfrage",
    "ereignisse_antwort",
}


def test_vertragsname():
    assert wv.VERTRAG == "kipm-werkbank/1"


@pytest.mark.parametrize(
    ("wert", "erwartet"),
    [
        ({"b": 1, "a": 2}, '{"a":2,"b":1}'),
        ({"a": [3, {"z": None, "y": True}], "s": "ä€"}, '{"a":[3,{"y":true,"z":null}],"s":"ä€"}'),
        ([], "[]"),
        ({}, "{}"),
        ("x\ny", '"x\\ny"'),
        (-0, "0"),
    ],
)
def test_kanonische_darstellung_von_hand(wert, erwartet):
    assert wv.kanonisch(wert) == erwartet.encode("utf-8")


def test_hash_ist_sha256_der_kanonischen_bytes():
    wert = {"b": 1, "a": [1, 2]}
    erwartet = hashlib.sha256(b'{"a":[1,2],"b":1}').hexdigest()
    assert wv.sha256_hex(wert) == erwartet
    assert wv.sha256_hex({"a": [1, 2], "b": 1}) == erwartet


@pytest.mark.parametrize(
    "schlecht", [1.5, float("nan"), {"a": 1.0}, {1: "x"}, {"a": {1, 2}}, 2**53]
)
def test_kanonisch_lehnt_unzulaessiges_ab(schlecht):
    with pytest.raises(ValueError):
        wv.kanonisch(schlecht)


def test_hashvektoren_der_beispiele_stimmen():
    vektoren = json.loads((ORDNER / "beispiele" / "hash-vektoren.json").read_text("utf-8"))
    assert len(vektoren) >= 4
    for v in vektoren:
        assert wv.kanonisch(v["json"]).decode("utf-8") == v["kanonisch"]
        assert hashlib.sha256(v["kanonisch"].encode("utf-8")).hexdigest() == v["sha256"]
        assert wv.sha256_hex(v["json"]) == v["sha256"]


def test_inhaltshash_schliesst_sich_selbst_aus():
    nachricht = {"vertrag": "kipm-werkbank/1", "operation_id": "x", "nutzlast": {"a": 1}}
    h = wv.inhaltshash(nachricht)
    mit = {**nachricht, "inhaltshash": h}
    assert wv.inhaltshash(mit) == h
    assert wv.inhaltshash({**mit, "nutzlast": {"a": 2}}) != h


def test_alle_erwarteten_schemas_vorhanden():
    namen = {p.name.removesuffix(".schema.json") for p in SCHEMAS}
    assert namen == ERWARTETE_SCHEMAS


@pytest.mark.parametrize("pfad", SCHEMAS, ids=lambda p: p.name)
def test_schema_ist_gueltig(pfad):
    wv.schema_selbst_pruefen(pfad.name.removesuffix(".schema.json"))


def test_beispiele_decken_jedes_nachrichtenschema_ab():
    for name in ERWARTETE_SCHEMAS - {"gemeinsam"}:
        gut = [
            p for p in BEISPIELE if p.name.startswith(f"{name}.") and p.name.endswith(".gut.json")
        ]
        schlecht = [
            p
            for p in BEISPIELE
            if p.name.startswith(f"{name}.") and p.name.endswith(".schlecht.json")
        ]
        assert gut, f"kein gültiges Beispiel für {name}"
        assert schlecht, f"kein ungültiges Beispiel für {name}"


@pytest.mark.parametrize(
    "pfad", [p for p in BEISPIELE if p.name.endswith(".gut.json")], ids=lambda p: p.name
)
def test_gueltige_beispiele(pfad):
    name = pfad.name.split(".")[0]
    daten = json.loads(pfad.read_text("utf-8"))
    assert wv.schema_fehler(name, daten) == []


@pytest.mark.parametrize(
    "pfad", [p for p in BEISPIELE if p.name.endswith(".schlecht.json")], ids=lambda p: p.name
)
def test_ungueltige_beispiele(pfad):
    name = pfad.name.split(".")[0]
    daten = json.loads(pfad.read_text("utf-8"))
    assert wv.schema_fehler(name, daten) != []


@pytest.mark.parametrize(
    "pfad",
    [
        p
        for p in BEISPIELE
        if p.name.startswith("ereignisse_anfrage")
        or p.name.startswith("schritt_anfrage")
        or p.name.startswith("uebernehmen_anfrage")
        if p.name.endswith(".gut.json")
    ],
    ids=lambda p: p.name,
)
def test_inhaltshash_der_beispiele_stimmt(pfad):
    daten = json.loads(pfad.read_text("utf-8"))
    assert daten["inhaltshash"] == wv.inhaltshash(daten)


def test_beispiele_enthalten_keine_schluessel():
    for pfad in BEISPIELE + SCHEMAS + [ORDNER / "VERTRAG.md"]:
        text = pfad.read_text("utf-8").lower()
        assert not re.search(r"bearer\s+(?!<)", text), pfad.name


def test_aenderungsnachricht_verlangt_vollstaendige_quellbindung():
    gut = json.loads((ORDNER / "beispiele" / "schritt_anfrage.start.gut.json").read_text("utf-8"))
    for feld in list(gut["quelle"]):
        kaputt = json.loads(json.dumps(gut))
        del kaputt["quelle"][feld]
        assert wv.schema_fehler("schritt_anfrage", kaputt) != [], feld


def test_fehlerantwort_hat_code_text_und_naechste_handlung():
    gut = json.loads((ORDNER / "beispiele" / "fehler.veraltet.gut.json").read_text("utf-8"))
    assert {"code", "text", "naechste_handlung"} <= set(gut["fehler"])
    for feld in ("code", "text", "naechste_handlung"):
        kaputt = json.loads(json.dumps(gut))
        del kaputt["fehler"][feld]
        assert wv.schema_fehler("fehler", kaputt) != []


def test_unbekannte_felder_werden_abgewiesen():
    gut = json.loads((ORDNER / "beispiele" / "schritt_anfrage.start.gut.json").read_text("utf-8"))
    gut["zusatz"] = 1
    assert wv.schema_fehler("schritt_anfrage", gut) != []

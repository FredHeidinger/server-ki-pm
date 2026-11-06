"""Vertrag kipm-werkbank/1: kanonische Darstellung, Hashes und Schemaprüfung.

Die Beschreibung steht in `vertrag/kipm-werkbank-1/VERTRAG.md`. Dieses Modul hat keine
Regeln des Dienstes, es rechnet nur. Die Werkbank hält eine feste Kopie der Vertragsdateien
und muss dieselben Hashes erzeugen (`beispiele/hash-vektoren.json`).
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

VERTRAG = "kipm-werkbank/1"
VERTRAG_ORDNER = Path(__file__).resolve().parent.parent / "vertrag" / "kipm-werkbank-1"
SCHEMA_ORDNER = VERTRAG_ORDNER / "schemas"

#: Größte Ganzzahl, die beide Seiten ohne Verlust austauschen (JavaScript-sicher).
MAX_GANZZAHL = 2**53 - 1


def _pruefen(wert: Any, pfad: str) -> None:
    if wert is None or isinstance(wert, (bool, str)):
        return
    if isinstance(wert, int):
        if abs(wert) > MAX_GANZZAHL:
            raise ValueError(f"{pfad}: Ganzzahl außerhalb des Bereichs")
        return
    if isinstance(wert, float):
        raise ValueError(f"{pfad}: Gleitkommazahlen sind im Vertrag nicht erlaubt")
    if isinstance(wert, (list, tuple)):
        for i, element in enumerate(wert):
            _pruefen(element, f"{pfad}[{i}]")
        return
    if isinstance(wert, dict):
        for schluessel, element in wert.items():
            if not isinstance(schluessel, str):
                raise ValueError(f"{pfad}: Schlüssel müssen Texte sein")
            _pruefen(element, f"{pfad}.{schluessel}")
        return
    raise ValueError(f"{pfad}: Typ {type(wert).__name__} ist im Vertrag nicht erlaubt")


def kanonisch(wert: Any) -> bytes:
    """Kanonische JSON-Darstellung: UTF-8, Schlüssel nach Codepunkt sortiert, ohne Leerraum.

    Erlaubt sind Objekte, Listen, Texte, Wahrheitswerte, null und Ganzzahlen bis 2**53-1.
    Gleitkommazahlen, NaN und andere Typen werden abgelehnt.
    """
    _pruefen(wert, "$")
    text = json.dumps(
        wert, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return text.encode("utf-8")


def sha256_hex(wert: Any) -> str:
    return hashlib.sha256(kanonisch(wert)).hexdigest()


def inhaltshash(nachricht: dict) -> str:
    """Hash einer Nachricht ohne das Feld `inhaltshash`."""
    ohne = {k: v for k, v in nachricht.items() if k != "inhaltshash"}
    return sha256_hex(ohne)


@lru_cache(maxsize=1)
def _register():
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    ressourcen = []
    for pfad in sorted(SCHEMA_ORDNER.glob("*.schema.json")):
        inhalt = json.loads(pfad.read_text("utf-8"))
        ressourcen.append((inhalt["$id"], Resource.from_contents(inhalt, DRAFT202012)))
    return Registry().with_resources(ressourcen)


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_ORDNER / f"{name}.schema.json").read_text("utf-8"))


def schema_selbst_pruefen(name: str) -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(_schema(name))


def schema_fehler(name: str, daten: Any) -> list[str]:
    """Verstöße von `daten` gegen das Schema `name`; leer = gültig."""
    from jsonschema import Draft202012Validator

    pruefer = Draft202012Validator(_schema(name), registry=_register())
    return [
        f"{'/'.join(str(p) for p in f.absolute_path) or '/'}: {f.message}"
        for f in sorted(pruefer.iter_errors(daten), key=lambda f: list(f.absolute_path))
    ]

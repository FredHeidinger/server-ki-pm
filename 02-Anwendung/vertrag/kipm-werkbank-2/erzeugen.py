"""Erzeugt ``schemas/`` von kipm-werkbank/2 aus Fassung 1 plus den in ``VERTRAG.md`` genannten
Erweiterungen. Fassung 1 wird nur gelesen. Aufruf im Ordner 02-Anwendung:
``uv run --no-project python vertrag/kipm-werkbank-2/erzeugen.py``; ein Test
(``tests/test_werkbank_v2_vertrag.py``) prüft, dass die eingecheckten Schemas genau dieser
Erzeugung entsprechen."""

import json
from pathlib import Path

HIER = Path(__file__).resolve().parent
ALT = HIER.parent / "kipm-werkbank-1" / "schemas"
NEU = HIER / "schemas"
G = "gemeinsam.schema.json#/$defs/"
REPO_ID = {"type": "string", "pattern": "^repo-[1-9][0-9]{0,15}$"}
COMMIT = {"type": "string", "pattern": "^([0-9a-f]{40}|[0-9a-f]{64})$"}


def _ref(name: str) -> dict:
    return {"$ref": G + name}


def _objekt(eigenschaften: dict, pflicht: list[str]) -> dict:
    return {
        "type": "object",
        "properties": eigenschaften,
        "required": pflicht,
        "additionalProperties": False,
    }


def erzeugen() -> dict[str, dict]:
    schemas = {}
    for pfad in sorted(ALT.glob("*.schema.json")):
        text = pfad.read_text("utf-8").replace("/werkbank/1/", "/werkbank/2/")
        schemas[pfad.name] = json.loads(text)
    g = schemas["gemeinsam.schema.json"]
    g["$defs"]["vertrag"] = {"const": "kipm-werkbank/2"}
    g["$defs"]["repo_id"] = REPO_ID
    g["$defs"]["commit_voll"] = COMMIT

    s = schemas["auftragssnapshot.schema.json"]
    s["title"] = "Unveränderlicher Auftragssnapshot (Fassung 2)"
    s["properties"]["snapshot_version"] = {"const": 2}
    s["properties"]["zustaendigkeit"] = _objekt(
        {
            "weg": {"const": "W1"},
            "fuehrendes_projekt": _ref("kurztext"),
            "erlaubnis": {
                "oneOf": [
                    {"type": "null"},
                    _objekt(
                        {
                            "id": _ref("posint"),
                            "revision": _ref("posint"),
                            "aktionen": {
                                "type": "array",
                                "items": {"type": "string", "pattern": "^[a-z_]{1,32}$"},
                                "maxItems": 20,
                            },
                        },
                        ["id", "revision", "aktionen"],
                    ),
                ]
            },
        },
        ["weg", "fuehrendes_projekt", "erlaubnis"],
    )
    s["properties"]["zielverbund"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": _objekt(
            {
                "repo_id": _ref("repo_id"),
                "fuehrend": {"type": "boolean"},
                "projekt": _ref("kurztext"),
                "projektbindung_id": _ref("posint"),
                "projektbindung_version": _ref("posint"),
                "arbeitsort": s["properties"]["arbeitsort"],
                "basiscommit": {"oneOf": [{"type": "null"}, _ref("commit_voll")]},
            },
            [
                "repo_id",
                "fuehrend",
                "projekt",
                "projektbindung_id",
                "projektbindung_version",
                "arbeitsort",
                "basiscommit",
            ],
        ),
    }
    s["properties"]["integrationseinheiten"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": _objekt(
            {
                "einheit": _ref("repo_id"),
                "repos": {"type": "array", "items": _ref("repo_id"), "minItems": 1, "maxItems": 1},
            },
            ["einheit", "repos"],
        ),
    }
    s["required"] += ["zustaendigkeit", "zielverbund", "integrationseinheiten"]

    u = schemas["uebernehmen_anfrage.schema.json"]["properties"]["nutzlast"]
    u["properties"]["repos"] = {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "items": _objekt(
            {
                "repo_id": _ref("repo_id"),
                "basiscommit": _ref("commit_voll"),
                "repo_fingerabdruck": _ref("kurztext"),
            },
            ["repo_id", "basiscommit", "repo_fingerabdruck"],
        ),
    }
    u["required"].append("repos")

    e = schemas["ereignisse_anfrage.schema.json"]
    arten = {
        fall["if"]["properties"]["art"]["const"]: fall["then"]["properties"]["daten"]
        for fall in e["properties"]["nutzlast"]["properties"]["ereignis"]["allOf"]
    }
    paket = arten["plan_bestaetigt"]["properties"]["pakete"]["items"]
    paket["properties"]["repo_id"] = _ref("repo_id")
    paket["required"].append("repo_id")
    arten["paket_status"]["properties"]["integration"] = _objekt(
        {"repo_id": _ref("repo_id"), "commit": {"type": "string", "pattern": "^[0-9a-f]{7,64}$"}},
        ["repo_id", "commit"],
    )
    integration = arten["abschluss_bericht"]["properties"]["integrationen"]["items"]
    integration["properties"]["repo_id"] = _ref("repo_id")
    integration["required"].append("repo_id")
    return schemas


def text(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    NEU.mkdir(exist_ok=True)
    for name, schema in erzeugen().items():
        (NEU / name).write_text(text(schema), encoding="utf-8", newline="\n")
    print("geschrieben:", len(list(NEU.glob("*.schema.json"))), "Schemas")

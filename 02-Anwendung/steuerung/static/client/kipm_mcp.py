#!/usr/bin/env python3
"""kipm_mcp – MCP-Server (stdio) für Claude Code; nur Standardbibliothek.

Stellt die Funktionen von Server-KI-PM als Werkzeuge bereit und spricht ausschließlich mit der
HTTP-Schnittstelle (über kipm.py im selben Ordner). Das Projekt ergibt sich aus dem Arbeitsordner,
in dem Claude Code gestartet wurde; Freigabe, Prüferwechsel und Abnahme gibt es hier nicht.

Eintrag in Claude Code:  claude mcp add --scope user kipm -- python <pfad>\\kipm_mcp.py
"""

import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kipm  # noqa: E402

PROTOKOLL = "2025-06-18"

WERKZEUGE = [
    ("kipm_projekt", "Projekt, Module und Arbeitsorte des aktuellen Ordners anzeigen", {}, []),
    (
        "kipm_auftraege",
        "Alle offenen Aufträge des Projekts (mit alle=true auch abgeschlossene; nur_meine=true nur übernehmbare und eigene)",
        {"alle": {"type": "boolean"}, "nur_meine": {"type": "boolean"}},
        [],
    ),
    (
        "kipm_auftrag_anlegen",
        "Wunsch als Entwurf anlegen; gleicher Titel wie ein offener Auftrag wird abgewiesen (erzwingen=true überstimmt). Freigabe bleibt beim Menschen.",
        {
            "titel": {"type": "string"},
            "text": {"type": "string"},
            "modul": {"type": "string", "description": "Modulkennung aus kipm_projekt"},
            "prioritaet": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
            "erzwingen": {"type": "boolean"},
        },
        ["titel", "text"],
    ),
    (
        "kipm_lesen",
        "Auftrag vollständig lesen (Ziel, Schreibumfang, Abnahmekriterien, Revision)",
        {"auftrag": {"type": "string", "description": "z. B. A-12"}},
        ["auftrag"],
    ),
    (
        "kipm_uebernehmen",
        "Freigegebenen Auftrag exklusiv übernehmen; revision muss die aktuelle sein",
        {"auftrag": {"type": "string"}, "revision": {"type": "integer"}},
        ["auftrag", "revision"],
    ),
    (
        "kipm_fortschritt",
        "Fortschritt melden",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_hindernis",
        "Hindernis melden; der Auftrag wird blockiert, bis der Mensch es aufhebt",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_rueckfrage",
        "Rückfrage an den Menschen; der Auftrag ist blockiert, bis die Antwort kommt (in kipm_lesen unter meldungen)",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_fehler",
        "Fehler melden (Abbruch, Werkzeugproblem); der Auftrag ist blockiert",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_vorschlag",
        "Änderungsvorschlag zum Auftrag; wird erst durch den Menschen wirksam",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_pruefbericht",
        "Prüfbericht zu einem fremden Auftrag; ersetzt keine Abnahme",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "text": {"type": "string"},
        },
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_ergebnis",
        "Ergebnis mit Nachweisen einreichen (dateien: pfad@commit, pruefungen: befehl: ergebnis); Auftrag geht zur Prüfung",
        {
            "auftrag": {"type": "string"},
            "revision": {"type": "integer"},
            "zusammenfassung": {"type": "string"},
            "dateien": {"type": "array", "items": {"type": "string"}},
            "pruefungen": {"type": "array", "items": {"type": "string"}},
            "luecken": {"type": "string"},
        },
        ["auftrag", "revision", "zusammenfassung", "dateien", "pruefungen"],
    ),
    ("kipm_entscheidungen", "Gemeinsame Entscheidungen, die das Projekt betreffen", {}, []),
    (
        "kipm_bestaetigen",
        "Übernahme einer entschiedenen Entscheidung für das Projekt bestätigen",
        {"entscheidung": {"type": "string", "description": "z. B. E-2"}},
        ["entscheidung"],
    ),
    (
        "kipm_entscheidung_vorschlagen",
        "Entscheidung für das Projekt vorschlagen",
        {"titel": {"type": "string"}, "text": {"type": "string"}, "verweis": {"type": "string"}},
        ["titel", "text"],
    ),
]


def _schreiben(nachricht: dict) -> None:
    sys.stdout.write(json.dumps(nachricht, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _nr(text: str) -> int:
    return kipm._nummer(str(text))


def ausfuehren(name: str, a: dict) -> tuple[int, dict]:
    kennung = uuid.uuid4().hex
    if name == "kipm_projekt":
        return kipm.anfrage("projekt/")
    if name == "kipm_auftraege":
        q = "?alle=1" if a.get("alle") else ("?nur_meine=1" if a.get("nur_meine") else "")
        return kipm.anfrage(f"auftraege/{q}")
    if name == "kipm_auftrag_anlegen":
        return kipm.anfrage(
            "auftraege/anlegen/",
            {
                "titel": a.get("titel", ""),
                "beschreibung": a.get("text", ""),
                "modul": a.get("modul", ""),
                "prioritaet": a.get("prioritaet") or "mittel",
                "erzwingen": bool(a.get("erzwingen")),
                "anfrage_kennung": kennung,
            },
        )
    if name == "kipm_lesen":
        return kipm.anfrage(f"auftraege/{_nr(a['auftrag'])}/")
    if name == "kipm_uebernehmen":
        return kipm.anfrage(
            f"auftraege/{_nr(a['auftrag'])}/uebernehmen/",
            {"revision": int(a["revision"]), "anfrage_kennung": kennung},
        )
    if name in (
        "kipm_fortschritt",
        "kipm_hindernis",
        "kipm_rueckfrage",
        "kipm_fehler",
        "kipm_vorschlag",
        "kipm_pruefbericht",
    ):
        pfad = name.removeprefix("kipm_")
        return kipm.anfrage(
            f"auftraege/{_nr(a['auftrag'])}/{pfad}/",
            {"revision": int(a["revision"]), "text": a.get("text", ""), "anfrage_kennung": kennung},
        )
    if name == "kipm_ergebnis":
        return kipm.anfrage(
            f"auftraege/{_nr(a['auftrag'])}/ergebnis/",
            {
                "revision": int(a["revision"]),
                "zusammenfassung": a.get("zusammenfassung", ""),
                "dateien": a.get("dateien") or [],
                "pruefungen": a.get("pruefungen") or [],
                "luecken": a.get("luecken", ""),
                "anfrage_kennung": kennung,
            },
        )
    if name == "kipm_entscheidungen":
        return kipm.anfrage("entscheidungen/")
    if name == "kipm_bestaetigen":
        return kipm.anfrage(
            f"entscheidungen/{_nr(a['entscheidung'])}/bestaetigen/", {"anfrage_kennung": kennung}
        )
    if name == "kipm_entscheidung_vorschlagen":
        return kipm.anfrage(
            "entscheidungen/vorschlagen/",
            {
                "titel": a.get("titel", ""),
                "text": a.get("text", ""),
                "verweis": a.get("verweis", ""),
                "anfrage_kennung": kennung,
            },
        )
    return 400, {"ok": False, "fehler": f"Unbekanntes Werkzeug {name}"}


def main() -> None:
    os.environ.setdefault("KIPM_ORDNER", os.getcwd())
    for zeile in sys.stdin:
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            anfrage = json.loads(zeile)
        except ValueError:
            continue
        rid = anfrage.get("id")
        methode = anfrage.get("method", "")
        if methode == "initialize":
            _schreiben(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": anfrage.get("params", {}).get(
                            "protocolVersion", PROTOKOLL
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "kipm", "version": "1.0"},
                        "instructions": (
                            "Server-KI-PM: zentrale Auftragssteuerung. Das Projekt ergibt sich aus dem Arbeitsordner. "
                            "Vor dem Anlegen immer kipm_auftraege prüfen; nur freigegebene Aufträge nach kipm_uebernehmen bearbeiten; "
                            "Ziel, Schreibumfang und Abnahmekriterien aus kipm_lesen sind bindend; bei offenen Fragen kipm_rueckfrage statt raten; Umfang, Freigabe und Abnahme nie selbst ändern."
                        ),
                    },
                }
            )
        elif methode == "tools/list":
            _schreiben(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "tools": [
                            {
                                "name": n,
                                "description": b,
                                "inputSchema": {"type": "object", "properties": p, "required": r},
                            }
                            for n, b, p, r in WERKZEUGE
                        ]
                    },
                }
            )
        elif methode == "tools/call":
            params = anfrage.get("params", {})
            try:
                status, antwort = ausfuehren(params.get("name", ""), params.get("arguments") or {})
                text = json.dumps(antwort, ensure_ascii=False, indent=2)
                _schreiben(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": text}],
                            "isError": not antwort.get("ok", False),
                        },
                    }
                )
            except SystemExit as fehler:
                _schreiben(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": str(fehler)}],
                            "isError": True,
                        },
                    }
                )
            except Exception as fehler:  # noqa: BLE001
                _schreiben(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "result": {
                            "content": [{"type": "text", "text": f"Fehler: {fehler}"}],
                            "isError": True,
                        },
                    }
                )
        elif methode == "ping":
            _schreiben({"jsonrpc": "2.0", "id": rid, "result": {}})
        elif rid is not None:
            _schreiben(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Unbekannte Methode {methode}"},
                }
            )


if __name__ == "__main__":
    main()

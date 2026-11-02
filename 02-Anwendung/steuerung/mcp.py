"""MCP-Endpunkt (Streamable HTTP, nur JSON-Antworten) für jedes KI-Programm.

Verbunden wird mit einem geheimen Link ``https://<server>/mcp/<schlüssel>/``, den Claude Code,
Codex, Claude- und ChatGPT-Connectors gleich annehmen; alternativ ``/mcp/`` mit Kopfzeile
``Authorization: Bearer <schlüssel>``.

Jeder Werkzeugaufruf wird intern an die HTTP-Schnittstelle (``api.py``) weitergereicht, damit
Befugnis, Wiederholungsschutz und Projektwahl genau dieselben Regeln haben. Das Projekt ergibt
sich aus ``projekt`` (Kennung) oder ``ordner`` (Arbeitsverzeichnis). Freigeben und Abnehmen
gibt es hier nicht; das macht der Mensch in der Oberfläche.
"""

import json
import uuid
from urllib.parse import urlsplit

from django.http import HttpRequest, HttpResponse, JsonResponse, QueryDict
from django.urls import resolve, reverse
from django.views.decorators.csrf import csrf_exempt

from . import dienst

PROTOKOLLE = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")

ANWEISUNGEN = (
    "Server-KI-PM ist Freds zentrale Projektsteuerung. Du arbeitest als sein Assistent: "
    "Projekte und Module anlegen, Wünsche und Issues (art=issue) erfassen, Aufträge mit "
    "kipm_planen zu Arbeitspaketen ausarbeiten (Ziel, Nicht-Ziele, Arbeitsort, Schreibumfang, "
    "Abnahmekriterien, Prüfer), bis „fuer_freigabe_fehlt“ leer ist, Rückfragen mit "
    "kipm_antworten beantworten und alles lesen. Freigeben und Abnehmen macht Fred selbst in "
    "der Oberfläche. Das Projekt nennst du mit „projekt“ (Kennung aus kipm_projekte) oder "
    "„ordner“ (dein Arbeitsverzeichnis). Vor dem Anlegen kipm_auftraege prüfen, nichts doppelt. "
    "Arbeitest du selbst an einem Auftrag: erst kipm_uebernehmen, dann kipm_fortschritt und "
    "zum Schluss kipm_ergebnis; Ziel, Schreibumfang und Abnahmekriterien aus kipm_lesen sind "
    "bindend."
)

_PROJEKT = {"type": "string", "description": "Projektkennung aus kipm_projekte"}
_ORDNER = {
    "type": "string",
    "description": "Dein Arbeitsverzeichnis; bestimmt das Projekt, wenn „projekt“ fehlt",
}
_AUFTRAG = {"type": "string", "description": "Auftragsnummer, z. B. A-12"}
_REVISION = {"type": "integer", "description": "Aktuelle Revision aus kipm_lesen"}
_TEXT = {"type": "string"}
_LISTE = {"type": "array", "items": {"type": "string"}}
_MELDUNG = {"auftrag": _AUFTRAG, "revision": _REVISION, "text": _TEXT}

WERKZEUGE = [
    ("kipm_projekte", "Alle Projekte mit Modulen, Arbeitsorten und Zahl offener Aufträge", {}, []),
    ("kipm_projekt", "Ein Projekt mit Modulen und Arbeitsorten", {}, []),
    (
        "kipm_projekt_anlegen",
        "Neues Projekt anlegen; module je Eintrag „Name“ oder „kennung: Name“",
        {"name": _TEXT, "kennung": _TEXT, "beschreibung": _TEXT, "module": _LISTE},
        ["name"],
    ),
    ("kipm_modul_anlegen", "Modul im Projekt anlegen", {"name": _TEXT, "kennung": _TEXT}, ["name"]),
    (
        "kipm_auftraege",
        "Offene Aufträge und Issues; ohne projekt und ordner aus allen Projekten. alle=true auch "
        "abgeschlossene, nur_meine=true nur übernehmbare und eigene, art=issue nur Issues",
        {
            "alle": {"type": "boolean"},
            "nur_meine": {"type": "boolean"},
            "art": {"type": "string", "enum": ["auftrag", "issue"]},
        },
        [],
    ),
    (
        "kipm_auftrag_anlegen",
        "Wunsch (art=auftrag) oder Issue (art=issue, Fehler oder Problem) als Entwurf anlegen; "
        "gleicher Titel wie ein offener Auftrag wird abgewiesen, außer erzwingen=true",
        {
            "titel": _TEXT,
            "text": _TEXT,
            "art": {"type": "string", "enum": ["auftrag", "issue"]},
            "modul": {"type": "string", "description": "Modulkennung"},
            "prioritaet": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
            "erzwingen": {"type": "boolean"},
        },
        ["titel", "text"],
    ),
    (
        "kipm_lesen",
        "Auftrag vollständig lesen: Ziel, Schreibumfang, Abnahmekriterien, Revision, Meldungen, "
        "fuer_freigabe_fehlt",
        {"auftrag": _AUFTRAG},
        ["auftrag"],
    ),
    (
        "kipm_planen",
        "Auftrag zum Arbeitspaket planen oder ändern; nur angegebene Felder ändern sich, eine "
        "Freigabe erlischt. Listenfelder je Eintrag eine Zeile. pruefer ist ein Benutzername.",
        {
            "auftrag": _AUFTRAG,
            "titel": _TEXT,
            "beschreibung": _TEXT,
            "ziel": _TEXT,
            "nicht_ziele": _TEXT,
            "arbeitsort": {"type": "string", "description": "Arbeitsort-Kennung"},
            "schreibumfang": _LISTE,
            "quellen": _TEXT,
            "abnahmekriterien": _LISTE,
            "modul": _TEXT,
            "prioritaet": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
            "art": {"type": "string", "enum": ["auftrag", "issue"]},
            "pruefer": _TEXT,
            "anlass": _TEXT,
        },
        ["auftrag"],
    ),
    (
        "kipm_antworten",
        "Rückfrage, Hindernis oder Fehler eines Bearbeiters in Freds Namen beantworten; hebt die "
        "Blockade auf",
        {"auftrag": _AUFTRAG, "text": _TEXT},
        ["auftrag", "text"],
    ),
    (
        "kipm_uebernehmen",
        "Freigegebenen Auftrag selbst exklusiv übernehmen; revision muss die aktuelle sein",
        {"auftrag": _AUFTRAG, "revision": _REVISION},
        ["auftrag", "revision"],
    ),
    ("kipm_fortschritt", "Fortschritt melden", _MELDUNG, ["auftrag", "revision", "text"]),
    (
        "kipm_rueckfrage",
        "Rückfrage an Fred; der Auftrag ist blockiert, bis die Antwort kommt",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_hindernis",
        "Hindernis melden; der Auftrag ist blockiert",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_fehler",
        "Fehler melden (Abbruch, Werkzeugproblem); der Auftrag ist blockiert",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_vorschlag",
        "Änderungsvorschlag zu einem Auftrag, an dem du arbeitest",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_pruefbericht",
        "Prüfbericht zu einem fremden Auftrag; ersetzt keine Abnahme",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_ergebnis",
        "Ergebnis mit Nachweisen einreichen (dateien: pfad@commit, pruefungen: befehl: "
        "ergebnis); der Auftrag geht zur Prüfung",
        {
            "auftrag": _AUFTRAG,
            "revision": _REVISION,
            "zusammenfassung": _TEXT,
            "dateien": _LISTE,
            "pruefungen": _LISTE,
            "luecken": _TEXT,
        },
        ["auftrag", "revision", "zusammenfassung", "dateien", "pruefungen"],
    ),
    ("kipm_entscheidungen", "Gemeinsame Entscheidungen, die das Projekt betreffen", {}, []),
    (
        "kipm_entscheidung_vorschlagen",
        "Entscheidung für das Projekt vorschlagen",
        {"titel": _TEXT, "text": _TEXT, "verweis": _TEXT},
        ["titel", "text"],
    ),
    (
        "kipm_bestaetigen",
        "Übernahme einer entschiedenen Entscheidung für das Projekt bestätigen",
        {"entscheidung": {"type": "string", "description": "z. B. E-2"}},
        ["entscheidung"],
    ),
]

# Werkzeug -> (URL-Name, Felder, die unverändert in den Rumpf gehen)
_AUFTRAGSWERKZEUGE = {
    "kipm_uebernehmen": ("api_uebernehmen", ()),
    "kipm_fortschritt": ("api_fortschritt", ("text",)),
    "kipm_rueckfrage": ("api_rueckfrage", ("text",)),
    "kipm_hindernis": ("api_hindernis", ("text",)),
    "kipm_fehler": ("api_fehler", ("text",)),
    "kipm_vorschlag": ("api_vorschlag", ("text",)),
    "kipm_pruefbericht": ("api_pruefbericht", ("text",)),
    "kipm_ergebnis": ("api_ergebnis", ("zusammenfassung", "dateien", "pruefungen", "luecken")),
}


class _Ungueltig(Exception):
    pass


def werkzeugliste() -> list[dict]:
    return [
        {
            "name": name,
            "description": beschreibung,
            "inputSchema": {
                "type": "object",
                "properties": {"projekt": _PROJEKT, "ordner": _ORDNER, **eigenschaften},
                "required": pflicht,
            },
        }
        for name, beschreibung, eigenschaften, pflicht in WERKZEUGE
    ]


def _nummer(text) -> int:
    wert = str(text).strip().upper()
    for praefix in ("A-", "E-"):
        wert = wert.removeprefix(praefix)
    if not wert.isdigit():
        raise _Ungueltig(f"Ungültige Nummer: {text}")
    return int(wert)


def _revision(a: dict) -> int:
    try:
        return int(a["revision"])
    except (KeyError, TypeError, ValueError) as fehler:
        raise _Ungueltig("revision fehlt oder ist keine Zahl.") from fehler


def _intern(schluessel: str, aussen: HttpRequest, a: dict, pfad: str, daten=None) -> dict:
    """Anfrage an die HTTP-Schnittstelle im selben Prozess; gleicher Schlüssel, gleiche Regeln."""
    pfad, _, abfrage = pfad.partition("?")
    innen = HttpRequest()
    innen.method = "GET" if daten is None else "POST"
    innen.path = innen.path_info = pfad
    innen.GET = QueryDict(abfrage)
    innen.META = {
        "HTTP_AUTHORIZATION": f"Bearer {schluessel}",
        "HTTP_X_KIPM_ORDNER": str(a.get("ordner") or "").strip(),
        "HTTP_X_KIPM_PROJEKT": str(a.get("projekt") or "").strip(),
        "REMOTE_ADDR": aussen.META.get("REMOTE_ADDR", ""),
        "CONTENT_TYPE": "application/json",
    }
    if daten is not None:
        daten = daten | {"anfrage_kennung": uuid.uuid4().hex}
    innen._body = json.dumps(daten).encode("utf-8") if daten is not None else b""
    treffer = resolve(pfad)
    antwort = treffer.func(innen, *treffer.args, **treffer.kwargs)
    return json.loads(antwort.content.decode("utf-8"))


def _felder(a: dict, namen) -> dict:
    return {n: a[n] for n in namen if n in a and a[n] is not None}


def werkzeug_ausfuehren(schluessel: str, request: HttpRequest, name: str, a: dict) -> dict:
    def rufe(pfad: str, daten=None) -> dict:
        return _intern(schluessel, request, a, pfad, daten)

    if name == "kipm_projekte":
        return rufe(reverse("api_projekte"))
    if name == "kipm_projekt":
        return rufe(reverse("api_projekt"))
    if name == "kipm_projekt_anlegen":
        return rufe(
            reverse("api_projekt_anlegen"),
            _felder(a, ("name", "kennung", "beschreibung", "module")),
        )
    if name == "kipm_modul_anlegen":
        return rufe(reverse("api_modul_anlegen"), _felder(a, ("name", "kennung")))
    if name == "kipm_auftraege":
        abfrage = "&".join(f"{k}=1" for k in ("alle", "nur_meine") if a.get(k)) + (
            f"&art={a['art']}" if a.get("art") else ""
        )
        return rufe(reverse("api_auftraege") + "?" + abfrage.lstrip("&"))
    if name == "kipm_auftrag_anlegen":
        daten = _felder(a, ("titel", "modul", "prioritaet", "art", "erzwingen"))
        daten["beschreibung"] = a.get("text", "")
        return rufe(reverse("api_auftrag_anlegen"), daten)
    if name == "kipm_entscheidungen":
        return rufe(reverse("api_entscheidungen"))
    if name == "kipm_entscheidung_vorschlagen":
        return rufe(
            reverse("api_entscheidung_vorschlagen"), _felder(a, ("titel", "text", "verweis"))
        )
    if name == "kipm_bestaetigen":
        nr = {"entscheidung_id": _nummer(a.get("entscheidung", ""))}
        return rufe(reverse("api_entscheidung_bestaetigen", kwargs=nr), {})
    if not a.get("auftrag"):
        raise _Ungueltig(f"Unbekanntes Werkzeug oder auftrag fehlt: {name}")
    nr = {"auftrag_id": _nummer(a["auftrag"])}
    if name == "kipm_lesen":
        return rufe(reverse("api_auftrag", kwargs=nr))
    if name == "kipm_planen":
        felder = {k: v for k, v in a.items() if k not in ("auftrag", "projekt", "ordner")}
        return rufe(reverse("api_planen", kwargs=nr), felder)
    if name == "kipm_antworten":
        return rufe(reverse("api_antworten", kwargs=nr), _felder(a, ("text",)))
    if name in _AUFTRAGSWERKZEUGE:
        url_name, felder = _AUFTRAGSWERKZEUGE[name]
        daten = {"revision": _revision(a)} | _felder(a, felder)
        return rufe(reverse(url_name, kwargs=nr), daten)
    raise _Ungueltig(f"Unbekanntes Werkzeug: {name}")


def _fehlerantwort(status: int, code: int, text: str) -> JsonResponse:
    return JsonResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": text}}, status=status
    )


def _antwort(rid, *, ergebnis: dict | None = None, fehler: tuple[int, str] | None = None):
    nachricht: dict = {"jsonrpc": "2.0", "id": rid}
    if fehler is not None:
        nachricht["error"] = {"code": fehler[0], "message": fehler[1]}
    else:
        nachricht["result"] = ergebnis or {}
    return JsonResponse(nachricht)


@csrf_exempt
def endpunkt(request: HttpRequest, schluessel: str = "") -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    herkunft = request.headers.get("Origin")
    if herkunft and urlsplit(herkunft).netloc != request.get_host():
        return HttpResponse(status=403)
    if not schluessel:
        kopf = request.headers.get("Authorization", "")
        schluessel = kopf[7:] if kopf.startswith("Bearer ") else ""
    if not schluessel or dienst.bindung_pruefen(schluessel) is None:
        return _fehlerantwort(
            401, -32001, "Link oder Schlüssel ungültig, abgelaufen oder widerrufen."
        )
    try:
        nachricht = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _fehlerantwort(400, -32700, "Kein JSON.")
    if not isinstance(nachricht, dict):
        return _fehlerantwort(400, -32600, "Ungültig.")
    if "id" not in nachricht:
        return HttpResponse(status=202)  # Benachrichtigung, z. B. notifications/initialized
    rid = nachricht["id"]
    methode = nachricht.get("method", "")
    params = nachricht.get("params") or {}
    if methode == "initialize":
        gewuenscht = params.get("protocolVersion")
        return _antwort(
            rid,
            ergebnis={
                "protocolVersion": gewuenscht if gewuenscht in PROTOKOLLE else PROTOKOLLE[-2],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "kipm", "title": "Server-KI-PM", "version": "1.1"},
                "instructions": ANWEISUNGEN,
            },
        )
    if methode == "ping":
        return _antwort(rid)
    if methode == "tools/list":
        return _antwort(rid, ergebnis={"tools": werkzeugliste()})
    if methode == "tools/call":
        try:
            daten = werkzeug_ausfuehren(
                schluessel, request, str(params.get("name", "")), params.get("arguments") or {}
            )
        except _Ungueltig as fehler:
            daten = {"ok": False, "fehler": str(fehler)}
        return _antwort(
            rid,
            ergebnis={
                "content": [
                    {"type": "text", "text": json.dumps(daten, ensure_ascii=False, indent=2)}
                ],
                "isError": not daten.get("ok", False),
            },
        )
    return _antwort(rid, fehler=(-32601, f"Unbekannte Methode {methode}"))

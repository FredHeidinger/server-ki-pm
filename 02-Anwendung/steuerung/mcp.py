"""MCP-Endpunkt (Streamable HTTP, nur JSON-Antworten) für Claude Code und andere KI-Chats.

Einrichtung beim Chat mit einem Befehl, ohne Dateien auf dem PC:
``claude mcp add --transport http --scope user kipm https://<server>/mcp/ --header
"Authorization: Bearer kipm_…"``.

Jeder Werkzeugaufruf wird intern an die HTTP-Schnittstelle (``api.py``) weitergereicht, damit
Befugnis, Wiederholungsschutz und Ordnerzuordnung genau dieselben Regeln haben. Das Projekt
ergibt sich aus dem Argument ``ordner`` (Arbeitsverzeichnis des Chats). Freigabe,
Prüferwechsel und Abnahme gibt es hier nicht.
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
    "Server-KI-PM: zentrale Auftragssteuerung. Übergib bei jedem Werkzeug in „ordner“ dein "
    "aktuelles Arbeitsverzeichnis; daraus ergibt sich das Projekt. Vor dem Anlegen immer "
    "kipm_auftraege prüfen; nur freigegebene Aufträge nach kipm_uebernehmen bearbeiten; Ziel, "
    "Schreibumfang und Abnahmekriterien aus kipm_lesen sind bindend; bei offenen Fragen "
    "kipm_rueckfrage statt raten; Umfang, Freigabe und Abnahme nie selbst ändern."
)

_ORDNER = {
    "type": "string",
    "description": "Absoluter Pfad deines aktuellen Arbeitsverzeichnisses; bestimmt das Projekt",
}
_AUFTRAG = {"type": "string", "description": "Auftragsnummer, z. B. A-12"}
_REVISION = {"type": "integer", "description": "Aktuelle Revision aus kipm_lesen"}
_MELDUNG = {"auftrag": _AUFTRAG, "revision": _REVISION, "text": {"type": "string"}}

WERKZEUGE = [
    ("kipm_projekt", "Projekt, Module und Arbeitsorte des aktuellen Ordners anzeigen", {}, []),
    (
        "kipm_auftraege",
        "Offene Aufträge des Projekts (alle=true auch abgeschlossene; nur_meine=true nur "
        "übernehmbare und eigene)",
        {"alle": {"type": "boolean"}, "nur_meine": {"type": "boolean"}},
        [],
    ),
    (
        "kipm_auftrag_anlegen",
        "Wunsch als Entwurf anlegen; gleicher Titel wie ein offener Auftrag wird abgewiesen "
        "(erzwingen=true überstimmt). Freigabe bleibt beim Menschen.",
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
        "Auftrag vollständig lesen (Ziel, Schreibumfang, Abnahmekriterien, Revision, Antworten)",
        {"auftrag": _AUFTRAG},
        ["auftrag"],
    ),
    (
        "kipm_uebernehmen",
        "Freigegebenen Auftrag exklusiv übernehmen; revision muss die aktuelle sein",
        {"auftrag": _AUFTRAG, "revision": _REVISION},
        ["auftrag", "revision"],
    ),
    ("kipm_fortschritt", "Fortschritt melden", _MELDUNG, ["auftrag", "revision", "text"]),
    (
        "kipm_hindernis",
        "Hindernis melden; der Auftrag ist blockiert, bis der Mensch es aufhebt",
        _MELDUNG,
        ["auftrag", "revision", "text"],
    ),
    (
        "kipm_rueckfrage",
        "Rückfrage an den Menschen; der Auftrag ist blockiert, bis die Antwort kommt "
        "(in kipm_lesen unter meldungen)",
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
        "Änderungsvorschlag zum Auftrag; wird erst durch den Menschen wirksam",
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

_MELDUNGSWERKZEUGE = {
    "kipm_fortschritt": "api_fortschritt",
    "kipm_hindernis": "api_hindernis",
    "kipm_rueckfrage": "api_rueckfrage",
    "kipm_fehler": "api_fehler",
    "kipm_vorschlag": "api_vorschlag",
    "kipm_pruefbericht": "api_pruefbericht",
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
                "properties": {"ordner": _ORDNER, **eigenschaften},
                "required": ["ordner", *pflicht],
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


def _intern(aussen: HttpRequest, pfad: str, ordner: str, daten: dict | None = None) -> dict:
    """Anfrage an die HTTP-Schnittstelle im selben Prozess; gleicher Schlüssel, gleiche Regeln."""
    pfad, _, abfrage = pfad.partition("?")
    innen = HttpRequest()
    innen.method = "GET" if daten is None else "POST"
    innen.path = innen.path_info = pfad
    innen.GET = QueryDict(abfrage)
    innen.META = {
        "HTTP_AUTHORIZATION": aussen.headers.get("Authorization", ""),
        "HTTP_X_KIPM_ORDNER": ordner,
        "REMOTE_ADDR": aussen.META.get("REMOTE_ADDR", ""),
        "CONTENT_TYPE": "application/json",
    }
    innen._body = json.dumps(daten or {}).encode("utf-8") if daten is not None else b""
    treffer = resolve(pfad)
    antwort = treffer.func(innen, *treffer.args, **treffer.kwargs)
    return json.loads(antwort.content.decode("utf-8"))


def werkzeug_ausfuehren(request: HttpRequest, name: str, a: dict) -> dict:
    ordner = str(a.get("ordner", "")).strip()
    kennung = {"anfrage_kennung": uuid.uuid4().hex}
    if name == "kipm_projekt":
        return _intern(request, reverse("api_projekt"), ordner)
    if name == "kipm_auftraege":
        abfrage = "?alle=1" if a.get("alle") else ("?nur_meine=1" if a.get("nur_meine") else "")
        return _intern(request, reverse("api_auftraege") + abfrage, ordner)
    if name == "kipm_auftrag_anlegen":
        daten = {
            "titel": a.get("titel", ""),
            "beschreibung": a.get("text", ""),
            "modul": a.get("modul", ""),
            "prioritaet": a.get("prioritaet") or "mittel",
            "erzwingen": bool(a.get("erzwingen")),
        }
        return _intern(request, reverse("api_auftrag_anlegen"), ordner, daten | kennung)
    if name == "kipm_entscheidungen":
        return _intern(request, reverse("api_entscheidungen"), ordner)
    if name == "kipm_entscheidung_vorschlagen":
        daten = {k: a.get(k, "") for k in ("titel", "text", "verweis")}
        return _intern(request, reverse("api_entscheidung_vorschlagen"), ordner, daten | kennung)
    if name == "kipm_bestaetigen":
        pfad = reverse(
            "api_entscheidung_bestaetigen", kwargs={"entscheidung_id": _nummer(a["entscheidung"])}
        )
        return _intern(request, pfad, ordner, kennung)
    if "auftrag" not in a:
        raise _Ungueltig(f"Unbekanntes Werkzeug oder auftrag fehlt: {name}")
    nr = {"auftrag_id": _nummer(a["auftrag"])}
    if name == "kipm_lesen":
        return _intern(request, reverse("api_auftrag", kwargs=nr), ordner)
    if name == "kipm_uebernehmen":
        daten = {"revision": _revision(a)}
        return _intern(request, reverse("api_uebernehmen", kwargs=nr), ordner, daten | kennung)
    if name in _MELDUNGSWERKZEUGE:
        daten = {"revision": _revision(a), "text": a.get("text", "")}
        pfad = reverse(_MELDUNGSWERKZEUGE[name], kwargs=nr)
        return _intern(request, pfad, ordner, daten | kennung)
    if name == "kipm_ergebnis":
        daten = {
            "revision": _revision(a),
            "zusammenfassung": a.get("zusammenfassung", ""),
            "dateien": a.get("dateien") or [],
            "pruefungen": a.get("pruefungen") or [],
            "luecken": a.get("luecken", ""),
        }
        return _intern(request, reverse("api_ergebnis", kwargs=nr), ordner, daten | kennung)
    raise _Ungueltig(f"Unbekanntes Werkzeug: {name}")


def _antwort(rid, *, ergebnis: dict | None = None, fehler: tuple[int, str] | None = None):
    nachricht: dict = {"jsonrpc": "2.0", "id": rid}
    if fehler is not None:
        nachricht["error"] = {"code": fehler[0], "message": fehler[1]}
    else:
        nachricht["result"] = ergebnis or {}
    return JsonResponse(nachricht)


@csrf_exempt
def endpunkt(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "POST"})
    herkunft = request.headers.get("Origin")
    if herkunft and urlsplit(herkunft).netloc != request.get_host():
        return HttpResponse(status=403)
    kopf = request.headers.get("Authorization", "")
    if not kopf.startswith("Bearer ") or dienst.bindung_pruefen(kopf[7:]) is None:
        return JsonResponse(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32001,
                    "message": "Zugangsschlüssel fehlt, ist ungültig, abgelaufen oder widerrufen.",
                },
            },
            status=401,
        )
    try:
        nachricht = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Kein JSON."}},
            status=400,
        )
    if not isinstance(nachricht, dict):
        return JsonResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Ungültig."}},
            status=400,
        )
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
                "serverInfo": {"name": "kipm", "title": "Server-KI-PM", "version": "1.0"},
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
                request, str(params.get("name", "")), params.get("arguments") or {}
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

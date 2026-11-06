"""Server-Nachbildung für die Werkbank-Implementierung (Vertrag kipm-werkbank/1).

Ein kleiner Server nur mit der Standardbibliothek, den die Werkbank-Seite ohne echtes KI-PM gegen
den Vertrag testen kann. Er hält alles im Speicher und spiegelt das Verhalten der Serverhälfte
nach Vertrag: Anmeldung per Bearer-Schlüssel, Kopf ``X-KIPM-Vertrag``, Fehler im Vertragsformat,
Wiederholungsschutz je ``operation_id`` (gleicher Hash: gespeicherter Ausgang, anderer Hash: 409),
höchstens eine Autorisierung je Schrittversuch, Ereignisse mit ``ereignis_id`` und Sequenz,
historische Quittierung später Meldungen. Fachregeln von KI-PM (Freigabe, Kontext, Abschluss)
bildet er nicht nach: Ein Test setzt den Stand mit ``NachbildungsServer.zuweisen`` und prüft die
Antworten der Werkbank gegen die Schemas.

Start: ``python nachbildung.py 8768`` (Schlüssel ``kipm_test_nachbildung``). Keine echten Schlüssel,
keine Netzaufrufe nach außen, nur 127.0.0.1.
"""

import hashlib
import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERTRAG = "kipm-werkbank/1"
SCHLUESSEL = "kipm_test_nachbildung"
SERVER_ID = "5e2f0a52-8d3c-4c6e-9a41-0b7d2f6c1a01"


def kanonisch(wert) -> bytes:
    return json.dumps(wert, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_hex(wert) -> str:
    return hashlib.sha256(kanonisch(wert)).hexdigest()


class Fehler(Exception):
    def __init__(self, status: int, code: str, text: str, handlung: str):
        self.status, self.code, self.text, self.handlung = status, code, text, handlung


class NachbildungsServer:
    """Der Zustand; ``zuweisen`` bereitet einen übernehmbaren Auftrag vor."""

    def __init__(self, werkbank_id: str | None = None):
        self.werkbank_id = werkbank_id or str(uuid.uuid4())
        self.zuweisungen: list[dict] = []
        self.operationen: dict[str, dict] = {}
        self.ausfuehrungen: dict[str, dict] = {}
        self.ereignisse: dict[str, dict] = {}
        self.autorisierungen: dict[tuple, str] = {}
        self.sperre = threading.Lock()

    def zuweisen(self, auftrag_id: int, snapshot: dict, revision: int = 1) -> dict:
        eintrag = {
            "auftrag_id": auftrag_id,
            "nummer": snapshot.get("nummer", f"T-{auftrag_id}"),
            "revision": revision,
            "zuweisung_version": 1,
            "projektbindung_id": 1,
            "projektbindung_version": 1,
            "freigegeben": True,
            "ausfuehrung_id": None,
            "_snapshot": snapshot,
        }
        self.zuweisungen.append(eintrag)
        return eintrag

    # --- Endpunkte ---------------------------------------------------------------------------

    def abgleich(self, abfrage: dict) -> dict:
        bekannt = [k for k in (abfrage.get("ausfuehrungen") or [""])[0].split(",") if k]
        konflikte = [
            {
                "ausfuehrung_id": k,
                "code": "AUSFUEHRUNG_UNBEKANNT",
                "text": "Der Server kennt diese Ausführung nicht.",
            }
            for k in bekannt
            if k not in self.ausfuehrungen
        ]
        zuw = [{k: v for k, v in z.items() if not k.startswith("_")} for z in self.zuweisungen]
        return {
            "vertrag": VERTRAG,
            "server_id": SERVER_ID,
            "server_zeit": "2030-01-01T00:00:00Z",
            "abruf_intervall_s": 10,
            "lebenszeichen_max_s": 30,
            "warnung_nach_s": 90,
            "seite": {"groesse": len(zuw), "naechste": None},
            "zuweisungen": zuw,
            "aktionen": [],
            "halte": [],
            "konflikte": konflikte,
        }

    def operation(self, operation_id: str) -> dict:
        op = self.operationen.get(operation_id)
        if op is None:
            return {
                "vertrag": VERTRAG,
                "operation_id": operation_id,
                "status": "unbekannt",
                "inhaltshash": None,
                "ausgang": None,
            }
        return {
            "vertrag": VERTRAG,
            "operation_id": operation_id,
            "status": "gespeichert",
            "inhaltshash": op["hash"],
            "ausgang": {"http_status": op["status"], "antwort": op["body"]},
        }

    def aendern(self, art: str, n: dict) -> tuple[int, dict]:
        if n.get("vertrag") != VERTRAG or n.get("art") != art:
            raise Fehler(
                400, "UNGUELTIGE_NACHRICHT", "Nachricht ungültig.", "Nach Vertrag korrigieren."
            )
        ohne = {k: v for k, v in n.items() if k != "inhaltshash"}
        if sha256_hex(ohne) != n.get("inhaltshash"):
            raise Fehler(
                400, "UNGUELTIGE_NACHRICHT", "Der Inhaltshash passt nicht.", "Neu berechnen."
            )
        with self.sperre:
            bekannt = self.operationen.get(n["operation_id"])
            if bekannt is not None:
                if bekannt["hash"] != n["inhaltshash"]:
                    raise Fehler(
                        409, "WIDERSPRUCH_WIEDERHOLUNG", "Andere Nachricht, gleiche operation_id.",
                        "Neue operation_id verwenden.",
                    )  # fmt: skip
                body = dict(bekannt["body"])
                if "ergebnis" in body:
                    body["ergebnis"] = "wiederholt"
                return bekannt["status"], body
            status, body = getattr(self, f"_{art}")(n)
            self.operationen[n["operation_id"]] = {
                "hash": n["inhaltshash"],
                "status": status,
                "body": body,
            }
            return status, body

    def _uebernehmen(self, n: dict) -> tuple[int, dict]:
        q = n["quelle"]
        z = next((z for z in self.zuweisungen if z["auftrag_id"] == q["auftrag_id"]), None)
        if z is None:
            raise Fehler(404, "NICHT_GEFUNDEN", "Unbekannter Auftrag.", "Abgleich abrufen.")
        if z["ausfuehrung_id"]:
            raise Fehler(409, "ZUSTANDSKONFLIKT", "Schon übernommen.", "Abgleich abrufen.")
        ausf = str(uuid.uuid4())
        snapshot = z["_snapshot"]
        sha = sha256_hex(snapshot)
        z["ausfuehrung_id"] = ausf
        quelle = {**q, "ausfuehrung_id": ausf, "snapshot_sha256": sha}
        self.ausfuehrungen[ausf] = {"quelle": quelle, "statusversion": 1, "sequenzen": set()}
        return 200, {
            "vertrag": VERTRAG,
            "operation_id": n["operation_id"],
            "ergebnis": "uebernommen",
            "quelle": quelle,
            "statusversion": 1,
            "snapshot": snapshot,
        }

    def _schritt(self, n: dict) -> tuple[int, dict]:
        nl = n["nutzlast"]
        s = nl["schritt"]
        schluessel = (s["ausfuehrung_id"], s["planfassung"], s["paket"], s["phase"], s["position"])
        antwort = {
            "vertrag": VERTRAG,
            "operation_id": n["operation_id"],
            "statusversion": 1,
        }
        if (*schluessel, nl["versuch_nr"]) in self.autorisierungen:
            return 200, {
                **antwort,
                "erlaubt": False,
                "grund_code": "VERSUCH_BEREITS_AUTORISIERT",
                "text": "Dieser Versuch ist schon autorisiert.",
                "naechste_handlung": "Die erste Antwort verwenden.",
                "autorisierung_id": None,
            }
        auto = str(uuid.uuid4())
        self.autorisierungen[(*schluessel, nl["versuch_nr"])] = auto
        return 200, {
            **antwort,
            "erlaubt": True,
            "grund_code": None,
            "text": "Schritt freigegeben.",
            "naechste_handlung": "Schritt ausführen und Ereignis melden.",
            "autorisierung_id": auto,
        }

    def _ereignisse(self, n: dict) -> tuple[int, dict]:
        nl = n["nutzlast"]
        ausf = self.ausfuehrungen.get(n["quelle"]["ausfuehrung_id"])
        if ausf is None:
            raise Fehler(404, "NICHT_GEFUNDEN", "Unbekannte Ausführung.", "Abgleich abrufen.")
        inhalt = sha256_hex(nl)
        vorhanden = self.ereignisse.get(nl["ereignis_id"])
        if vorhanden is not None and vorhanden != inhalt:
            raise Fehler(
                409, "WIDERSPRUCH_WIEDERHOLUNG", "Anderer Inhalt.", "Neue ereignis_id verwenden."
            )
        self.ereignisse[nl["ereignis_id"]] = inhalt
        ausf["sequenzen"].add(nl["sequenz"])
        if nl["statusversion"] is not None:
            ausf["statusversion"] = max(ausf["statusversion"], nl["statusversion"])
        return 200, {
            "vertrag": VERTRAG,
            "operation_id": n["operation_id"],
            "ereignis_id": nl["ereignis_id"],
            "ergebnis": "wiederholt" if vorhanden is not None else "verarbeitet",
            "statusversion": ausf["statusversion"],
            "technischer_zustand": "laeuft",
            "haltanforderung": None,
            "naechste_handlung": "Weiterarbeiten wie bisher.",
        }


def _handler(zustand: NachbildungsServer):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):  # still
            return

        def _senden(self, status: int, body: dict) -> None:
            roh = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(roh)))
            self.end_headers()
            self.wfile.write(roh)

        def _fehler(self, f: Fehler) -> None:
            self._senden(
                f.status,
                {"fehler": {"code": f.code, "text": f.text, "naechste_handlung": f.handlung}},
            )

        def _pruefen(self) -> None:
            if self.headers.get("Authorization") != f"Bearer {SCHLUESSEL}":
                raise Fehler(
                    401, "NICHT_ANGEMELDET", "Zugang fehlt oder ungültig.", "Schlüssel prüfen."
                )
            if self.headers.get("X-KIPM-Vertrag") != VERTRAG:
                raise Fehler(
                    426,
                    "VERTRAG_INKOMPATIBEL",
                    "Vertrag nicht kompatibel.",
                    "Werkbank aktualisieren.",
                )

        def do_GET(self):
            try:
                self._pruefen()
                url = urlparse(self.path)
                teile = [t for t in url.path.split("/") if t]
                if teile[-1:] == ["abgleich"]:
                    return self._senden(200, zustand.abgleich(parse_qs(url.query)))
                if len(teile) >= 2 and teile[-2] == "operationen":
                    return self._senden(200, zustand.operation(teile[-1]))
                raise Fehler(404, "NICHT_GEFUNDEN", "Unbekannter Pfad.", "Pfad prüfen.")
            except Fehler as f:
                self._fehler(f)

        def do_POST(self):
            try:
                self._pruefen()
                art = [t for t in urlparse(self.path).path.split("/") if t][-1]
                if art not in ("uebernehmen", "schritt", "ereignisse"):
                    raise Fehler(404, "NICHT_GEFUNDEN", "Unbekannter Pfad.", "Pfad prüfen.")
                roh = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    n = json.loads(roh)
                except ValueError:
                    raise Fehler(
                        400, "UNGUELTIGE_NACHRICHT", "Kein JSON.", "Nachricht korrigieren."
                    ) from None
                status, body = zustand.aendern(art, n)
                self._senden(status, body)
            except Fehler as f:
                self._fehler(f)

    return H


def starten(port: int = 0, zustand: NachbildungsServer | None = None):
    """Startet die Nachbildung in einem Thread; gibt (Zustand, Server, Basisadresse) zurück."""
    zustand = zustand or NachbildungsServer()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(zustand))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return zustand, server, f"http://127.0.0.1:{server.server_address[1]}/api/werkbank/v1/"


if __name__ == "__main__":
    _, _server, adresse = starten(int(sys.argv[1]) if len(sys.argv) > 1 else 8768)
    print("Nachbildung läuft unter", adresse, "mit Schlüssel", SCHLUESSEL)
    threading.Event().wait()

"""Nachgebildeter Microsoft-Dienst für tests/test_kopieren.py: Anmeldung, Website, Hochladen, Auflisten, Abruf.

Aus einem grünen Lauf folgt nicht, dass der echte Dienst genauso antwortet; das belegt erst die Abnahme auf dem Server.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Dienst:
    def __init__(self):
        self.anfragen: list[dict] = []
        self.dateien: dict[str, bytes] = {}
        self.anmeldung_status = 200
        self.website_status = 200
        self.hochladen_status = 200
        self.falsche_groesse = False
        self.umleiten = False
        self.basis = ""

    def start(self):
        dienst = self

        class Griff(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _senden(self, status, daten):
                roh = json.dumps(daten).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(roh)))
                self.end_headers()
                self.wfile.write(roh)

            def _merken(self, koerper=b""):
                dienst.anfragen.append(
                    {
                        "methode": self.command,
                        "pfad": self.path,
                        "zugang": self.headers.get("Authorization"),
                        "koerper": koerper,
                    }
                )

            def do_POST(self):
                self._merken(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                if dienst.anmeldung_status != 200:
                    return self._senden(dienst.anmeldung_status, {"error": "invalid_client"})
                return self._senden(200, {"access_token": "token-der-nachbildung"})

            def do_PUT(self):
                koerper = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._merken(koerper)
                if dienst.hochladen_status != 200:
                    return self._senden(
                        dienst.hochladen_status, {"error": {"code": "accessDenied"}}
                    )
                name = self.path.split(":/")[1].rsplit(":", 1)[0].split("/")[-1]
                dienst.dateien[name] = koerper
                return self._senden(
                    200, {"name": name, "size": len(koerper) + (1 if dienst.falsche_groesse else 0)}
                )

            def do_GET(self):
                self._merken()
                if self.path.startswith("/abruf/"):
                    inhalt = dienst.dateien[self.path.removeprefix("/abruf/")]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(inhalt)))
                    self.end_headers()
                    self.wfile.write(inhalt)
                    return None
                if self.path.endswith(":/children"):
                    werte = [
                        {
                            "name": n,
                            "size": len(i),
                            "@microsoft.graph.downloadUrl": f"{dienst.basis}/abruf/{n}",
                        }
                        for n, i in dienst.dateien.items()
                    ]
                    return self._senden(200, {"value": [*werte, {"name": "fremd.txt", "size": 1}]})
                if dienst.umleiten:
                    self.send_response(302)
                    self.send_header("Location", "/umgeleitet/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if dienst.website_status != 200:
                    return self._senden(dienst.website_status, {"error": {"code": "accessDenied"}})
                return self._senden(200, {"id": "website-kennung"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Griff)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.basis = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self.basis

    def stopp(self):
        self.server.shutdown()
        self.server.server_close()

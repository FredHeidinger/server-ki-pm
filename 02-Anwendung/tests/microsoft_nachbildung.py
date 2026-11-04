"""Nachgebildeter Microsoft-Dienst für tests/test_kopieren.py: Anmeldung, Website, Hochladen, Auflisten, Abruf.

Aus einem grünen Lauf folgt nicht, dass der echte Dienst genauso antwortet; das belegt erst die Abnahme auf dem Server.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ADMIN_TOKEN = "anmeldung-der-" + "nachbildung"
GEHEIMNIS_ANFANG = "erzeugtes-geheimnis-"


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
        # Einrichtung mit Anmeldung
        self.anmeldung_wartet = 0
        self.admin_status = 200
        self.website_da = False
        self.website_verzoegerung = 1
        self.gruppen: list[dict] = []
        self.apps: list[dict] = []
        self.dienstkonten: list[dict] = []
        self.zuweisungen: list[dict] = []
        self.freigaben: list[dict] = []
        self.geheimnisse: list[str] = []
        self.entfernte_geheimnisse: list[str] = []
        self.client_fehlschlaege = 0
        self.hochladen_fehlschlaege = 0
        self.fremde_erreichbar = False

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

            def _admin(self) -> bool:
                """Einrichtungswege gehen nur mit der Anmeldung, nie mit dem Zugang der App."""
                if self.headers.get("Authorization") == f"Bearer {ADMIN_TOKEN}":
                    return True
                self._senden(403, {"error": {"code": "accessDenied"}})
                return False

            def _anmeldung(self, koerper: bytes):
                werte = urllib.parse.parse_qs(koerper.decode())
                if werte["grant_type"] == ["client_credentials"]:
                    if dienst.client_fehlschlaege > 0:
                        dienst.client_fehlschlaege -= 1
                        return self._senden(401, {"error": "invalid_client"})
                    if dienst.anmeldung_status != 200:
                        return self._senden(dienst.anmeldung_status, {"error": "invalid_client"})
                    return self._senden(200, {"access_token": "token-der-nachbildung"})
                if dienst.anmeldung_wartet > 0:
                    dienst.anmeldung_wartet -= 1
                    return self._senden(400, {"error": "authorization_pending"})
                return self._senden(200, {"access_token": ADMIN_TOKEN})

            def do_POST(self):
                koerper = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._merken(koerper)
                if "/oauth2/v2.0/devicecode" in self.path:
                    return self._senden(
                        200,
                        {
                            "device_code": "geraetecode",
                            "user_code": "ABCD1234",
                            "expires_in": 900,
                            "interval": 0,
                            "message": "Gehe zu https://microsoft.com/devicelogin, Code ABCD1234",
                        },
                    )
                if "/oauth2/v2.0/token" in self.path:
                    return self._anmeldung(koerper)
                if not self._admin():
                    return None
                if dienst.admin_status != 200:
                    return self._senden(
                        dienst.admin_status, {"error": {"code": "Authorization_RequestDenied"}}
                    )
                daten = json.loads(koerper or b"{}")
                if self.path == "/groups":
                    dienst.gruppen.append({"id": "gruppe-1", **daten})
                    return self._senden(201, {"id": "gruppe-1"})
                if self.path == "/applications":
                    app = {
                        "id": "app-objekt",
                        "appId": "app-id",
                        "passwordCredentials": [],
                        **daten,
                    }
                    dienst.apps.append(app)
                    return self._senden(201, app)
                if self.path == "/servicePrincipals":
                    konto = {"id": "dienstkonto-1", **daten}
                    dienst.dienstkonten.append(konto)
                    return self._senden(201, konto)
                if self.path.endswith("/appRoleAssignments"):
                    dienst.zuweisungen.append(daten)
                    return self._senden(201, daten)
                if self.path.endswith("/permissions"):
                    dienst.freigaben.append(
                        {
                            "id": "1",
                            "roles": daten["roles"],
                            "grantedToIdentitiesV2": daten["grantedToIdentities"],
                            "wunsch": daten,
                        }
                    )
                    return self._senden(201, daten)
                if self.path.endswith("/addPassword"):
                    geheimnis = f"{GEHEIMNIS_ANFANG}{len(dienst.geheimnisse) + 1}"
                    dienst.geheimnisse.append(geheimnis)
                    dienst.apps[0]["passwordCredentials"].append(
                        {"keyId": f"schluessel-{len(dienst.geheimnisse)}"}
                    )
                    return self._senden(200, {"secretText": geheimnis})
                if self.path.endswith("/removePassword"):
                    dienst.entfernte_geheimnisse.append(daten["keyId"])
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._senden(404, {"error": {"code": "itemNotFound"}})

            def do_PUT(self):
                koerper = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self._merken(koerper)
                if dienst.hochladen_fehlschlaege > 0:
                    dienst.hochladen_fehlschlaege -= 1
                    return self._senden(403, {"error": {"code": "accessDenied"}})
                if dienst.hochladen_status != 200:
                    return self._senden(
                        dienst.hochladen_status, {"error": {"code": "accessDenied"}}
                    )
                name = self.path.split(":/")[1].rsplit(":", 1)[0].split("/")[-1]
                dienst.dateien[name] = koerper
                return self._senden(
                    200, {"name": name, "size": len(koerper) + (1 if dienst.falsche_groesse else 0)}
                )

            def _einrichtungswege(self):
                """(Status, Antwort) für die Wege der Einrichtung, sonst None."""
                pfad = urllib.parse.unquote(self.path)
                admin = self.headers.get("Authorization") == f"Bearer {ADMIN_TOKEN}"
                nicht_da = (404, {"error": {"code": "itemNotFound"}})
                website = {
                    "id": "website-kennung",
                    "webUrl": "https://firma.sharepoint.com/sites/kipm-sicherung",
                }
                if pfad == "/sites/firma.sharepoint.com":  # Stammwebsite: die „andere“ Website
                    if admin or dienst.fremde_erreichbar:
                        return 200, {"id": "stamm"}
                    return 403, {"error": {"code": "accessDenied"}}
                if pfad == "/sites/firma.sharepoint.com:/sites/kipm-sicherung":
                    return (200, website) if dienst.website_da else nicht_da
                if not admin:
                    return None
                if pfad == "/me":
                    return 200, {"id": "ich"}
                if pfad == "/organization":
                    return 200, {"value": [{"id": "mandant-x"}]}
                if pfad.split("?")[0] == "/sites/root":
                    return 200, {
                        "id": "stamm",
                        "siteCollection": {"hostname": "firma.sharepoint.com"},
                    }
                if pfad.startswith("/groups?"):
                    return 200, {"value": dienst.gruppen}
                if pfad.startswith("/groups/") and pfad.endswith("/sites/root"):
                    if dienst.website_verzoegerung > 0:
                        dienst.website_verzoegerung -= 1
                        return nicht_da
                    dienst.website_da = True
                    return 200, website
                if pfad.startswith("/applications?"):
                    return 200, {"value": dienst.apps}
                if pfad.startswith("/servicePrincipals(appId='00000003"):
                    rollen = [
                        {
                            "id": "rolle-breit",
                            "value": "Sites.ReadWrite.All",
                            "allowedMemberTypes": ["Application"],
                        },
                        {
                            "id": "rolle-eng",
                            "value": "Sites.Selected",
                            "allowedMemberTypes": ["Application"],
                        },
                    ]
                    return 200, {"id": "graph-dienst", "appRoles": rollen}
                if pfad.startswith("/servicePrincipals(appId="):
                    return (200, dienst.dienstkonten[0]) if dienst.dienstkonten else nicht_da
                if pfad.endswith("/appRoleAssignments"):
                    return 200, {"value": dienst.zuweisungen}
                if pfad.endswith("/permissions"):
                    return 200, {"value": dienst.freigaben}
                return None

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
                antwort = self._einrichtungswege()
                if antwort is not None:
                    return self._senden(*antwort)
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

"""Nachgebildetes GitHub: lokaler HTTP-Server mit den Antworten, die der Lesezugang erwartet.

Für Tests des echten Transports und die Browserprüfung; ersetzt keinen Abruf bei GitHub selbst.
Eigenständig starten (Browserprüfung, ``KIPM_GITHUB_API`` zeigt dann auf diese Adresse):

    uv run python tests/github_nachbildung.py 8767
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

GUELTIG = "nachbildung-lese-schluessel"
MIT_SCHREIBRECHT = "nachbildung-klassisch-mit-repo"
LANGSAM_SEKUNDEN = 3


def _commit(zeichen: str) -> str:
    return zeichen * 40


def _bereitstellung(kennung, umgebung, ref, sha, tag, produktion=False):
    return {
        "id": kennung,
        "environment": umgebung,
        "ref": ref,
        "sha": sha,
        "task": "deploy",
        "created_at": f"2026-10-{tag:02d}T10:00:00Z",
        "production_environment": produktion,
    }


REPOS = {
    "fred/nexus": {
        "id": 1,
        "release": "v1.3",
        "tags": {"v1.1": "a", "v1.2": "b", "v1.3": "c"},
        "bereitstellungen": [
            _bereitstellung(11, "Produktion", "v1.2", _commit("b"), 10, produktion=True),
            _bereitstellung(12, "Test", "main", _commit("c"), 20),
        ],
    },
    "fred/server-ki-pm": {
        "id": 2,
        "release": "v2.0",
        "tags": {"v2.0": "d"},
        "bereitstellungen": [_bereitstellung(21, "Produktion", "v2.0", _commit("d"), 15)],
    },
    "fred/gesperrt": {"id": 3, "gesperrt": True},
    "fred/langsam": {"id": 4, "langsam": True},
    "fred/leer": {"id": 5},
    # Vom Schlüssel gelistet, aber nicht (mehr) abrufbar.
    "fred/weg": {"id": 6, "fehlt": True},
    "fred/kaputt": {"id": 7, "kaputt": True},
}
ZUSTAENDE = {11: "success", 12: "success", 21: "success"}


class _Anfrage(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def _antwort(self, status, daten, kopf=None):
        rumpf = daten if isinstance(daten, bytes) else json.dumps(daten).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(rumpf)))
        for name, wert in (kopf or {}).items():
            self.send_header(name, wert)
        self.end_headers()
        try:
            self.wfile.write(rumpf)
        except OSError:
            pass  # Aufrufer hat wegen Zeitüberschreitung aufgelegt.

    def _aufzeichnen(self):
        self.server.anfragen.append(
            {
                "methode": self.command,
                "pfad": self.path,
                "schluessel": self.headers.get("Authorization", "").removeprefix("Bearer "),
            }
        )

    def do_GET(self):
        self._aufzeichnen()
        schluessel = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if schluessel not in (GUELTIG, MIT_SCHREIBRECHT):
            return self._antwort(401, {"message": "Bad credentials"})
        teile = urlsplit(self.path)
        pfad, abfrage = teile.path, parse_qs(teile.query)
        if pfad == "/user":
            scopes = "repo, read:user" if schluessel == MIT_SCHREIBRECHT else ""
            return self._antwort(200, {"login": "fred-test"}, {"X-OAuth-Scopes": scopes})
        if pfad == "/user/repos":
            return self._repos(abfrage)
        if pfad == "/umgeleitet/fremd":
            ziel = self.server.fremdes_ziel
            return self._antwort(301, {}, {"Location": ziel})
        if pfad == "/umgeleitet/hier":
            return self._antwort(301, {}, {"Location": "/user"})
        if not pfad.startswith("/repos/"):
            return self._antwort(404, {"message": "Not Found"})
        _, _, besitzer, name, *rest = pfad.split("/")
        repo = REPOS.get(f"{besitzer}/{name}")
        if repo is None or repo.get("fehlt"):
            return self._antwort(404, {"message": "Not Found"})
        return self._repo(f"{besitzer}/{name}", repo, rest)

    def _repos(self, abfrage):
        namen = list(REPOS)
        seite = int(abfrage.get("page", ["1"])[0])
        daten = [
            {"id": REPOS[n]["id"], "full_name": n, "default_branch": "main"}
            for n in (namen[:3] if seite == 1 else namen[3:])
        ]
        kopf = {}
        if seite == 1:
            host = self.headers.get("Host")
            kopf["Link"] = f'<http://{host}/user/repos?per_page=100&page=2>; rel="next"'
        return self._antwort(200, daten, kopf)

    def _repo(self, voll, repo, rest):
        if repo.get("kaputt"):
            return self._antwort(200, b"<html>keine Antwort von GitHub</html>")
        if not rest:
            return self._antwort(
                200, {"id": repo["id"], "full_name": voll, "default_branch": "main"}
            )
        if repo.get("gesperrt"):
            return self._antwort(
                403, {"message": "Resource not accessible by personal access token"}
            )
        if repo.get("langsam"):
            time.sleep(LANGSAM_SEKUNDEN)
        if rest == ["releases", "latest"]:
            if "release" not in repo:
                return self._antwort(404, {"message": "Not Found"})
            return self._antwort(
                200,
                {
                    "tag_name": repo["release"],
                    "name": f"Release {repo['release']}",
                    "html_url": f"https://github.example/{voll}/releases/{repo['release']}",
                },
            )
        if rest == ["tags"]:
            return self._antwort(
                200,
                [
                    {"name": n, "commit": {"sha": _commit(z)}}
                    for n, z in repo.get("tags", {}).items()
                ],
            )
        if rest == ["deployments"]:
            return self._antwort(200, repo.get("bereitstellungen", []))
        if len(rest) == 3 and rest[0] == "deployments" and rest[2] == "statuses":
            zustand = ZUSTAENDE.get(int(rest[1]))
            return self._antwort(
                200,
                [{"id": 1, "state": zustand, "created_at": "2026-10-10T10:01:00Z"}]
                if zustand
                else [],
            )
        return self._antwort(404, {"message": "Not Found"})

    def _nicht_erlaubt(self):
        """Schreibende Methoden darf KI-PM nie benutzen; sie stehen dann in ``anfragen``."""
        self._aufzeichnen()
        self._antwort(405, {"message": "Nur Lesen"})

    do_POST = do_PUT = do_PATCH = do_DELETE = _nicht_erlaubt


def starten(port: int = 0) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), _Anfrage)
    server.anfragen = []
    server.fremdes_ziel = ""
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def adresse(server: ThreadingHTTPServer) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    laufend = starten(int(sys.argv[1]))
    print("NACHBILDUNG", adresse(laufend), flush=True)
    threading.Event().wait()

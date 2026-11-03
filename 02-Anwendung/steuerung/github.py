"""Transport zu GitHub über eine GitHub App. Nur der Dienst ruft ihn auf; Browser und MCP
erhalten nie Zugangsdaten.

Die App authentisiert sich mit einem kurzlebigen JWT (RS256) und tauscht es je Installation
gegen ein Installationstoken, das auf genau das verbundene Repository beschränkt wird. Lesen
nutzt Paginierung (``Link``) und bedingte Anfragen (``ETag``/304). Fehler kommen als
``GitHubFehler`` mit Status; Zeitüberschreitungen gelten als ungeklärter Ausgang.

Für Tests wird mit ``adapter_setzen`` eine Attrappe eingesetzt; sie beweist keine echte
Schreibfähigkeit.
"""

import base64
import calendar
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

API_VERSION = "2022-11-28"
_test_adapter = None


class GitHubFehler(Exception):
    """Antwort mit Fehlerstatus. ``status`` 0 heißt: Ausgang unbekannt (Zeit, Verbindung)."""

    def __init__(self, status: int, text: str, warten: int = 0):
        super().__init__(text)
        self.status = status
        self.warten = warten

    @property
    def unklar(self) -> bool:
        return self.status == 0 or self.status >= 500


def adapter_setzen(adapter) -> None:
    global _test_adapter
    _test_adapter = adapter


def adapter():
    """Eingesetzter Adapter oder echte App; ``None``, wenn GitHub nicht eingerichtet ist."""
    if _test_adapter is not None:
        return _test_adapter
    if settings.KIPM_GITHUB_APP_ID and settings.KIPM_GITHUB_APP_SCHLUESSEL:
        return GitHubApp(settings.KIPM_GITHUB_APP_ID, settings.KIPM_GITHUB_APP_SCHLUESSEL)
    return None


def app_jwt(app_id: str, schluessel_pem: str, jetzt: float | None = None) -> str:
    """JWT für die App: ausgestellt 60 s zurückdatiert, höchstens 10 Minuten gültig."""
    import jwt

    jetzt = int(jetzt if jetzt is not None else time.time())
    return jwt.encode(
        {"iat": jetzt - 60, "exp": jetzt + 540, "iss": str(app_id)}, schluessel_pem, "RS256"
    )


class GitHubApp:
    def __init__(self, app_id: str, schluessel_pfad: str, api: str | None = None):
        self.app_id = app_id
        self.schluessel_pfad = schluessel_pfad
        self.api = (api or settings.KIPM_GITHUB_API).rstrip("/")
        self._tokens: dict = {}

    # --- Anfragen ---

    def _roh(self, methode, url, *, token, daten=None, etag="", zeit=15):
        kopf = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Server-KI-PM",
            "Authorization": f"Bearer {token}",
        }
        if etag:
            kopf["If-None-Match"] = etag
        rumpf = None
        if daten is not None:
            rumpf = json.dumps(daten).encode("utf-8")
            kopf["Content-Type"] = "application/json"
        anfrage = urllib.request.Request(url, data=rumpf, method=methode, headers=kopf)
        try:
            with urllib.request.urlopen(anfrage, timeout=zeit) as antwort:  # noqa: S310
                inhalt = antwort.read()
                return antwort.status, (json.loads(inhalt) if inhalt else None), antwort.headers
        except urllib.error.HTTPError as fehler:
            if fehler.code == 304:
                return 304, None, fehler.headers
            warten = int(fehler.headers.get("Retry-After") or 0)
            if fehler.code in (403, 429) and fehler.headers.get("X-RateLimit-Remaining") == "0":
                warten = max(
                    warten, int(fehler.headers.get("X-RateLimit-Reset", 0)) - int(time.time())
                )
            try:
                text = json.loads(fehler.read() or b"{}").get("message", "")
            except ValueError:
                text = ""
            raise GitHubFehler(
                fehler.code, text or f"GitHub antwortet {fehler.code}", warten
            ) from None
        except (TimeoutError, urllib.error.URLError, OSError) as fehler:
            raise GitHubFehler(0, f"GitHub nicht erreichbar: {type(fehler).__name__}") from None

    def _app_token(self) -> str:
        with open(self.schluessel_pfad, encoding="utf-8") as f:
            return app_jwt(self.app_id, f.read())

    def _token(self, ziel) -> str:
        schluessel = (ziel.installation_id, ziel.github_id)
        token, ablauf = self._tokens.get(schluessel, ("", 0))
        if token and ablauf - 60 > time.time():
            return token
        _, daten, _ = self._roh(
            "POST",
            f"{self.api}/app/installations/{ziel.installation_id}/access_tokens",
            token=self._app_token(),
            daten={"repository_ids": [ziel.github_id]},
        )
        ablauf = calendar.timegm(time.strptime(daten["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        self._tokens[schluessel] = (daten["token"], ablauf)
        return daten["token"]

    def _repo(self, ziel, pfad: str) -> str:
        return f"{self.api}/repos/{ziel.name}{pfad}"

    def _anfrage(self, ziel, methode, pfad, daten=None, etag=""):
        return self._roh(
            methode, self._repo(ziel, pfad), token=self._token(ziel), daten=daten, etag=etag
        )

    # --- Einrichtung ---

    def repo_finden(self, name: str) -> dict:
        """Repository und App-Installation zu ``besitzer/name``; stabile ID für die Zuordnung."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
            raise GitHubFehler(400, "Name muss besitzer/repository sein.")
        _, inst, _ = self._roh(
            "GET", f"{self.api}/repos/{name}/installation", token=self._app_token()
        )
        # Kurzlebiges Token der Installation, nur um die stabile Repository-ID zu erfahren.
        _, daten, _ = self._roh(
            "POST",
            f"{self.api}/app/installations/{inst['id']}/access_tokens",
            token=self._app_token(),
        )
        _, repo, _ = self._roh("GET", f"{self.api}/repos/{name}", token=daten["token"])
        return {
            "id": repo["id"],
            "name": repo["full_name"],
            "standardbranch": repo["default_branch"],
            "installation_id": inst["id"],
        }

    def repo_lesen(self, ziel) -> dict:
        _, repo, _ = self._roh(
            "GET", f"{self.api}/repositories/{ziel.github_id}", token=self._token(ziel)
        )
        return {
            "id": repo["id"],
            "name": repo["full_name"],
            "standardbranch": repo["default_branch"],
        }

    # --- Lesen ---

    def seiten(self, ziel, pfad: str, parameter: dict, etag: str = "", max_seiten: int = 10):
        """(Einträge, ETag der ersten Seite, Status, vollständig). 304 heißt unverändert."""
        url = self._repo(ziel, pfad) + "?" + urllib.parse.urlencode({**parameter, "per_page": 100})
        eintraege, erster_etag, erste = [], "", True
        for _ in range(max_seiten):
            status, daten, kopf = self._roh(
                "GET", url, token=self._token(ziel), etag=etag if erste else ""
            )
            if erste:
                erster_etag = kopf.get("ETag", "") if kopf else ""
                if status == 304:
                    return [], etag, 304, True
                erste = False
            eintraege.extend(
                daten["check_runs"] if isinstance(daten, dict) and "check_runs" in daten else daten
            )
            weiter = re.search(r'<([^>]+)>;\s*rel="next"', (kopf.get("Link", "") if kopf else ""))
            if not weiter:
                return eintraege, erster_etag, 200, True
            url = weiter.group(1)
        return eintraege, erster_etag, 200, False

    def datei(self, ziel, pfad: str, ref: str) -> dict | None:
        try:
            _, daten, _ = self._anfrage(
                ziel, "GET", f"/contents/{urllib.parse.quote(pfad)}?ref={urllib.parse.quote(ref)}"
            )
        except GitHubFehler as fehler:
            if fehler.status == 404:
                return None
            raise
        if not isinstance(daten, dict) or daten.get("type") != "file":
            raise GitHubFehler(422, "Pfad ist keine normale Datei (Symlink, Submodul oder Ordner).")
        return {"sha": daten["sha"], "inhalt": base64.b64decode(daten["content"]).decode("utf-8")}

    def ref(self, ziel, branch: str) -> str | None:
        try:
            _, daten, _ = self._anfrage(ziel, "GET", f"/git/ref/heads/{urllib.parse.quote(branch)}")
        except GitHubFehler as fehler:
            if fehler.status == 404:
                return None
            raise
        return daten["object"]["sha"]

    def workflow_stand(self, ziel, ref: str) -> tuple[str, list[str]]:
        """Hash über Namen und SHAs der Workflow-Dateien; leer, wenn es keine gibt."""
        try:
            _, daten, _ = self._anfrage(
                ziel, "GET", f"/contents/.github/workflows?ref={urllib.parse.quote(ref)}"
            )
        except GitHubFehler as fehler:
            if fehler.status == 404:
                return "", []
            raise
        eintraege = sorted((e["name"], e["sha"]) for e in daten or [])
        return hashlib.sha256(json.dumps(eintraege).encode()).hexdigest(), [n for n, _ in eintraege]

    def issue(self, ziel, nummer: int) -> dict:
        _, daten, _ = self._anfrage(ziel, "GET", f"/issues/{int(nummer)}")
        return daten

    def kommentare(self, ziel, nummer: int) -> list[dict]:
        eintraege, _, _, _ = self.seiten(ziel, f"/issues/{int(nummer)}/comments", {}, max_seiten=5)
        return eintraege

    def issues_mit_marke(self, ziel, marke: str) -> list[dict]:
        eintraege, _, _, _ = self.seiten(
            ziel, "/issues", {"state": "all", "sort": "created", "direction": "desc"}, max_seiten=2
        )
        return [e for e in eintraege if marke in (e.get("body") or "")]

    def pr_fuer_branch(self, ziel, branch: str) -> dict | None:
        besitzer = ziel.name.split("/")[0]
        eintraege, _, _, _ = self.seiten(
            ziel, "/pulls", {"state": "all", "head": f"{besitzer}:{branch}"}, max_seiten=1
        )
        return eintraege[0] if eintraege else None

    # --- Schreiben (nur nach bestätigter Aktion) ---

    def issue_anlegen(self, ziel, titel: str, text: str) -> dict:
        _, daten, _ = self._anfrage(ziel, "POST", "/issues", {"title": titel, "body": text})
        return daten

    def kommentar(self, ziel, nummer: int, text: str) -> dict:
        _, daten, _ = self._anfrage(ziel, "POST", f"/issues/{int(nummer)}/comments", {"body": text})
        return daten

    def issue_aendern(self, ziel, nummer: int, felder: dict) -> dict:
        _, daten, _ = self._anfrage(ziel, "PATCH", f"/issues/{int(nummer)}", felder)
        return daten

    def branch_anlegen(self, ziel, branch: str, sha: str) -> None:
        self._anfrage(ziel, "POST", "/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})

    def datei_schreiben(self, ziel, pfad, inhalt, branch, nachricht, sha=None) -> str:
        daten = {
            "message": nachricht,
            "branch": branch,
            "content": base64.b64encode(inhalt.encode("utf-8")).decode("ascii"),
        }
        if sha:
            daten["sha"] = sha
        _, antwort, _ = self._anfrage(ziel, "PUT", f"/contents/{urllib.parse.quote(pfad)}", daten)
        return antwort["commit"]["sha"]

    def pr_anlegen(self, ziel, titel, head, base, text) -> dict:
        _, daten, _ = self._anfrage(
            ziel, "POST", "/pulls", {"title": titel, "head": head, "base": base, "body": text}
        )
        return daten

"""Transport zu GitHub, nur lesend. Nur der Dienst ruft ihn auf; Browser und MCP erhalten nie
Zugangsdaten.

``GitHubLesen`` arbeitet mit Freds Lese-Schlüssel und kennt nur GET: Konto, Repositorys, Releases,
Tags und Bereitstellungen. Der Schlüssel steht nur im Kopf der Anfrage, nie in Adresse, Meldung
oder Log; bei einer Umleitung auf einen anderen Host geht er nicht mit. Fehler kommen als
``GitHubFehler`` mit Status.

Für Tests wird mit ``adapter_setzen`` eine Attrappe eingesetzt; sie beweist keine echte
Integration.
"""

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from django.conf import settings

API_VERSION = "2022-11-28"
GESAMT_SEKUNDEN = 20  # je Abruf; der Serverprozess bricht eine Anfrage nach 60 s ab
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_test_adapter = None


class GitHubFehler(Exception):
    """Antwort mit Fehlerstatus. ``status`` 0 heißt: Ausgang unbekannt (Zeit, Verbindung)."""

    def __init__(self, status: int, text: str, warten: int = 0):
        super().__init__(text)
        self.status = status
        self.warten = warten


def adapter_setzen(adapter) -> None:
    global _test_adapter
    _test_adapter = adapter


def adapter(schluessel: str) -> Any:
    """Eingesetzter Adapter, sonst der Lesezugang mit diesem Schlüssel."""
    return _test_adapter if _test_adapter is not None else GitHubLesen(schluessel)


class _NurGleicherHost(urllib.request.HTTPRedirectHandler):
    """Umgeleitet wird nur auf demselben Host; urllib gäbe den Schlüssel sonst weiter."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_oeffner = urllib.request.build_opener(_NurGleicherHost)


def _zahl(wert) -> int:
    try:
        return int(wert or 0)
    except (TypeError, ValueError):
        return 0


def _anfrage(url, *, token, zeit=15) -> tuple[int, Any, Any]:
    """Eine GET-Anfrage: (Status, Antwort, Kopfzeilen); jeder Fehlschlag wird ``GitHubFehler``."""
    kopf = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "Server-KI-PM",
        "Authorization": f"Bearer {token}",
    }
    anfrage = urllib.request.Request(url, method="GET", headers=kopf)
    try:
        with _oeffner.open(anfrage, timeout=zeit) as antwort:
            inhalt = antwort.read()
            return antwort.status, (json.loads(inhalt) if inhalt else None), antwort.headers
    except urllib.error.HTTPError as fehler:
        warten = _zahl(fehler.headers.get("Retry-After"))
        if fehler.code in (403, 429) and fehler.headers.get("X-RateLimit-Remaining") == "0":
            warten = max(warten, _zahl(fehler.headers.get("X-RateLimit-Reset")) - int(time.time()))
        try:
            text = json.loads(fehler.read() or b"{}").get("message", "")
        except (ValueError, AttributeError, OSError, http.client.HTTPException):
            text = ""
        raise GitHubFehler(fehler.code, text or f"GitHub antwortet {fehler.code}", warten) from None
    except ValueError:
        raise GitHubFehler(502, "GitHub antwortet nicht lesbar") from None
    except (TimeoutError, urllib.error.URLError, http.client.HTTPException, OSError) as fehler:
        raise GitHubFehler(0, f"GitHub nicht erreichbar: {type(fehler).__name__}") from None


class GitHubLesen:
    """Lesezugang mit dem Lese-Schlüssel: nur GET, keine schreibende Methode."""

    def __init__(self, schluessel: str, api: str | None = None, zeit: float | None = None):
        self._schluessel = schluessel
        self.api = (api or settings.KIPM_GITHUB_API).rstrip("/")
        self.zeit = zeit if zeit is not None else settings.KIPM_GITHUB_ZEIT
        self._ende = time.monotonic() + GESAMT_SEKUNDEN

    def __repr__(self) -> str:
        return "GitHubLesen()"

    def _holen(self, url: str):
        if time.monotonic() > self._ende:
            raise GitHubFehler(0, "GitHub nicht erreichbar: Gesamtzeit überschritten")
        return _anfrage(url, token=self._schluessel, zeit=self.zeit)

    def _liste(self, pfad: str, max_seiten: int = 3) -> list[dict]:
        url, eintraege = f"{self.api}{pfad}", []
        trenner = "&" if "?" in url else "?"
        url += f"{trenner}per_page=100"
        for _ in range(max_seiten):
            _, daten, kopf = self._holen(url)
            if not isinstance(daten, list):
                raise GitHubFehler(502, "GitHub antwortet nicht lesbar")
            eintraege.extend(e for e in daten if isinstance(e, dict))
            weiter = re.search(r'<([^>]+)>;\s*rel="next"', kopf.get("Link", "") if kopf else "")
            if not weiter:
                break
            url = weiter.group(1)
            if not url.startswith(f"{self.api}/"):
                raise GitHubFehler(502, "GitHub antwortet nicht lesbar")
        return eintraege

    def _objekt(self, pfad: str) -> tuple[dict, object]:
        _, daten, kopf = self._holen(f"{self.api}{pfad}")
        if not isinstance(daten, dict):
            raise GitHubFehler(502, "GitHub antwortet nicht lesbar")
        return daten, kopf

    @staticmethod
    def _repo_daten(repo: dict) -> dict:
        return {
            "id": repo.get("id"),
            "name": repo.get("full_name", ""),
            "standardbranch": repo.get("default_branch", "main"),
        }

    def _repo_pfad(self, name: str) -> str:
        if not REPO_NAME.fullmatch(name or ""):
            raise GitHubFehler(404, "Name muss besitzer/repository sein.")
        return f"/repos/{name}"

    def konto(self) -> dict:
        """Angemeldetes Konto; ``bereiche`` sind die Rechte eines klassischen Schlüssels (bei
        feingranularen Schlüsseln nennt GitHub keine)."""
        daten, kopf = self._objekt("/user")
        bereiche = (kopf.get("X-OAuth-Scopes", "") if kopf else "") or ""
        return {
            "login": daten.get("login", ""),
            "bereiche": [b.strip() for b in bereiche.split(",") if b.strip()],
        }

    def repositorys(self) -> list[dict]:
        return [
            self._repo_daten(r) for r in self._liste("/user/repos?sort=full_name", max_seiten=10)
        ]

    def repository(self, name: str) -> dict:
        daten = self._repo_daten(self._objekt(self._repo_pfad(name))[0])
        if not isinstance(daten["id"], int):
            raise GitHubFehler(502, "GitHub antwortet nicht lesbar")
        return daten

    def neuestes_release(self, name: str) -> dict | None:
        """Neuestes veröffentlichtes Release (ohne Entwürfe und Vorabversionen); ``None``, wenn es
        keines gibt. Das Repository selbst prüft vorher ``repository``."""
        try:
            daten, _ = self._objekt(f"{self._repo_pfad(name)}/releases/latest")
        except GitHubFehler as fehler:
            if fehler.status == 404:
                return None
            raise
        return {
            "tag": daten.get("tag_name", ""),
            "name": daten.get("name") or daten.get("tag_name", ""),
            "url": daten.get("html_url", ""),
        }

    def tags(self, name: str) -> list[dict]:
        return [
            {"name": t.get("name") or "", "sha": (t.get("commit") or {}).get("sha") or ""}
            for t in self._liste(f"{self._repo_pfad(name)}/tags")
        ]

    def bereitstellungen(self, name: str) -> list[dict]:
        return [
            {
                "id": d.get("id"),
                "umgebung": d.get("environment") or "",
                "ref": (d.get("ref") or "").removeprefix("refs/tags/").removeprefix("refs/heads/"),
                "sha": d.get("sha") or "",
                "erstellt": d.get("created_at") or "",
                "produktion": bool(d.get("production_environment")),
            }
            for d in self._liste(f"{self._repo_pfad(name)}/deployments")
            if isinstance(d.get("id"), int)
        ]

    def bereitstellung_zustand(self, name: str, kennung: int) -> str:
        """Jüngster Zustand einer Bereitstellung; leer, wenn GitHub keinen kennt."""
        eintraege = self._liste(
            f"{self._repo_pfad(name)}/deployments/{int(kennung)}/statuses", max_seiten=1
        )
        if not eintraege:
            return ""
        return max(eintraege, key=lambda e: (e.get("created_at", ""), e.get("id", 0))).get(
            "state", ""
        )

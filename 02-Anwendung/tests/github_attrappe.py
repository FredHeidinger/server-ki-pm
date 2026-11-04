"""Attrappe des Lesezugangs für Tests: ersetzt nur den Netztransport, beweist keine echte Integration."""

from steuerung.github import GitHubFehler


class Attrappe:
    """Antworten eines GitHub-Kontos; Standard: Produktion auf v1.2, neueste v1.3."""

    def __init__(self):
        self.fehler = {}
        self.konto_daten = {"login": "fred", "bereiche": []}
        self.repo_liste = [
            {"id": 4711, "name": "fred/test", "standardbranch": "main"},
            {"id": 4712, "name": "fred/zweites", "standardbranch": "main"},
        ]
        self.release = {"tag": "v1.3", "name": "v1.3", "url": ""}
        self.tag_liste = [{"name": "v1.2", "sha": "b" * 40}, {"name": "v1.3", "sha": "c" * 40}]
        self.bereitstellungs_liste = [
            {
                "id": 1,
                "umgebung": "Produktion",
                "ref": "v1.2",
                "sha": "b" * 40,
                "erstellt": "2026-10-01T10:00:00Z",
                "produktion": True,
            }
        ]
        self.zustaende = {1: "success"}
        self.lesezugriffe = []

    def konto(self):
        self._pruefe("konto")
        return dict(self.konto_daten)

    def repositorys(self):
        self._pruefe("repositorys")
        return [dict(r) for r in self.repo_liste]

    def repository(self, name):
        self.lesezugriffe.append(name)
        self._pruefe("repository")
        treffer = next((r for r in self.repo_liste if r["name"] == name), None)
        if treffer is None:
            raise GitHubFehler(404, "Not Found")
        return dict(treffer)

    def neuestes_release(self, name):
        self._pruefe("release")
        return dict(self.release) if self.release else None

    def tags(self, name):
        return [dict(t) for t in self.tag_liste]

    def bereitstellungen(self, name):
        self._pruefe("bereitstellungen")
        return [dict(b) for b in self.bereitstellungs_liste]

    def bereitstellung_zustand(self, name, kennung):
        return self.zustaende.get(kennung, "")

    def _pruefe(self, name):
        if name in self.fehler:
            fehler = self.fehler[name]
            if isinstance(fehler, list):
                fehler = fehler.pop(0) if fehler else None
                if not self.fehler[name]:
                    del self.fehler[name]
            else:
                del self.fehler[name]
            if fehler:
                raise fehler

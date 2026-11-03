"""GitHub lesen und kontrolliert schreiben (P11–P15) gegen eine Attrappe.

Die Attrappe ersetzt nur den Netztransport; sie beweist keine echte Schreibfähigkeit bei
GitHub (P13 bleibt ein Integrationsnachweis mit einem bestimmten Testrepository).
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import abgleich, dienst, github
from steuerung.github import GitHubFehler
from steuerung.models import Abrufstand, Ereignis, ExterneAktion, ExternesObjekt, RepoVerbindung

pytestmark = pytest.mark.django_db


class Attrappe:
    """Speicher eines Repositorys; protokolliert jede verändernde Anfrage in ``schreiben``."""

    def __init__(self):
        self.repo = {
            "id": 4711,
            "name": "fred/test",
            "standardbranch": "main",
            "installation_id": 9,
        }
        self.issues = {
            1: {
                "id": 101,
                "number": 1,
                "title": "Alt",
                "body": "Text",
                "state": "open",
                "updated_at": "2026-11-01T10:00:00Z",
                "html_url": "https://github.example/i/1",
            }
        }
        self.kommentar_liste = {1: []}
        self.refs = {"main": "c0ffee"}
        self.dateien = {("c0ffee", "docs/a.md"): {"sha": "s1", "inhalt": "alt\n"}}
        self.prs = []
        self.workflows = ("w1", ["ci.yml"])
        self.schreiben = []
        self.fehler = {}
        self.antworten = {}

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

    def repo_finden(self, name):
        return dict(self.repo)

    def repo_lesen(self, ziel):
        return {k: self.repo[k] for k in ("id", "name", "standardbranch")}

    def seiten(self, ziel, pfad, parameter, etag="", max_seiten=10):
        bereich = pfad.split("/")[1]
        self._pruefe("seiten:" + bereich)
        if bereich in self.antworten:
            return self.antworten[bereich](parameter, etag)
        if bereich == "issues":
            return list(self.issues.values()), "e1", 200, True
        return [], "", 200, True

    def workflow_stand(self, ziel, ref):
        return self.workflows

    def ref(self, ziel, branch):
        return self.refs.get(branch)

    def datei(self, ziel, pfad, ref):
        d = self.dateien.get((ref, pfad))
        return dict(d) if d else None

    def issue(self, ziel, nummer):
        return dict(self.issues[nummer])

    def kommentare(self, ziel, nummer):
        return list(self.kommentar_liste.get(nummer, []))

    def issues_mit_marke(self, ziel, marke):
        return [i for i in self.issues.values() if marke in (i.get("body") or "")]

    def pr_fuer_branch(self, ziel, branch):
        return next((p for p in self.prs if p["head"] == branch), None)

    def issue_anlegen(self, ziel, titel, text):
        self._pruefe("issue_anlegen:vorher")
        nr = max(self.issues) + 1
        self.issues[nr] = {
            "id": 100 + nr,
            "number": nr,
            "title": titel,
            "body": text,
            "state": "open",
            "updated_at": "2026-11-03T10:00:00Z",
            "html_url": f"https://github.example/i/{nr}",
        }
        self.schreiben.append(("issue", nr))
        self._pruefe("issue_anlegen:nachher")
        return self.issues[nr]

    def kommentar(self, ziel, nummer, text):
        k = {
            "id": len(self.schreiben) + 500,
            "body": text,
            "html_url": f"https://github.example/k/{nummer}",
        }
        self.kommentar_liste.setdefault(nummer, []).append(k)
        self.schreiben.append(("kommentar", nummer))
        return k

    def issue_aendern(self, ziel, nummer, felder):
        i = self.issues[nummer]
        i.update(
            {
                "title": felder.get("title", i["title"]),
                "body": felder.get("body", i["body"]),
                "state": felder.get("state", i["state"]),
            }
        )
        self.schreiben.append(("aendern", nummer))
        return i

    def branch_anlegen(self, ziel, branch, sha):
        self.refs[branch] = sha
        self.schreiben.append(("branch", branch))

    def datei_schreiben(self, ziel, pfad, inhalt, branch, nachricht, sha=None):
        self.dateien[(branch, pfad)] = {"sha": "neu", "inhalt": inhalt}
        self.schreiben.append(("datei", pfad, nachricht))
        return "beefcafe"

    def pr_anlegen(self, ziel, titel, head, base, text):
        self._pruefe("pr_anlegen")
        pr = {
            "number": len(self.prs) + 7,
            "html_url": f"https://github.example/pr/{len(self.prs) + 7}",
            "head": head,
            "body": text,
        }
        self.prs.append(pr)
        self.schreiben.append(("pr", head))
        return pr


@pytest.fixture
def gh():
    attrappe = Attrappe()
    github.adapter_setzen(attrappe)
    yield attrappe
    github.adapter_setzen(None)


@pytest.fixture
def verbindung(akteur, projekt, gh):
    return abgleich.verbindung_anlegen(
        akteur,
        projekt,
        name="fred/test",
        schreibend=True,
        aktionen="issue_anlegen\nkommentar\nissue_aendern\ndokument_pr",
        pfade="docs/**",
    )


def _bestaetigt(akteur, a):
    a.refresh_from_db()
    abgleich.aktion_bestaetigen(akteur, a, revision=a.revision, nutzlast_hash=a.nutzlast_hash)
    a.refresh_from_db()
    return a


def test_verbindung_stabile_id_und_ein_schreibendes_projekt(
    akteur, projekt, projekt_b, verbindung, gh
):
    assert verbindung.github_id == 4711 and "ci.yml" in verbindung.workflow_hinweis
    with pytest.raises(dienst.DienstFehler, match="genau ein Projekt"):
        abgleich.verbindung_anlegen(akteur, projekt_b, name="fred/test", schreibend=True)
    lesend = abgleich.verbindung_anlegen(akteur, projekt_b, name="fred/test")
    assert not lesend.schreibend
    with pytest.raises(dienst.DienstFehler, match="nicht erlaubt"):
        abgleich.verbindung_anlegen(akteur, projekt, name="x/y", pfade=".github/workflows/**")


def test_ohne_app_klare_meldung(akteur, projekt):
    github.adapter_setzen(None)
    with pytest.raises(dienst.DienstFehler, match="nicht eingerichtet"):
        abgleich.verbindung_anlegen(akteur, projekt, name="fred/test")


def test_p11_lesen_fehler_304_doppelte_und_veraltete_antworten(akteur, verbindung, gh):
    bericht = abgleich.aktualisieren(akteur, verbindung)
    assert bericht["issues"]["geaendert"] == 1
    issue = ExternesObjekt.objects.get(typ="issue")
    stand = Abrufstand.objects.get(verbindung=verbindung, bereich="issues")
    assert stand.vollstaendig and stand.etag == "e1" and stand.seit is not None
    ereignisse = Ereignis.objects.count()
    # Gleiche Antwort nochmal: nichts ändert sich, kein Ereignis.
    assert abgleich.aktualisieren(akteur, verbindung)["issues"]["geaendert"] == 0
    assert Ereignis.objects.count() == ereignisse
    # 304: unverändert, ETag wird mitgeschickt.
    gesendet = []
    gh.antworten["issues"] = lambda parameter, etag: (
        gesendet.append((parameter, etag)) or ([], etag, 304, True)
    )
    assert abgleich.aktualisieren(akteur, verbindung)["issues"]["status"] == 304
    assert gesendet[0][1] == "e1" and "since" in gesendet[0][0]
    # Ältere Antwort überschreibt keine jüngere Beobachtung.
    alt = dict(gh.issues[1], title="Veraltet", updated_at="2026-10-01T10:00:00Z")
    gh.antworten["issues"] = lambda parameter, etag: ([alt], "e2", 200, True)
    abgleich.aktualisieren(akteur, verbindung)
    issue.refresh_from_db()
    assert issue.titel == "Alt"
    # Fehlerstatus: zuletzt gelesene Daten bleiben, Fehler sichtbar, Erfolgszeit unverändert.
    erfolg = Abrufstand.objects.get(verbindung=verbindung, bereich="issues").letzter_erfolg
    for status, text in (
        (401, "Bad credentials"),
        (403, "Forbidden"),
        (404, "Not Found"),
        (429, "rate limit"),
        (0, "nicht erreichbar"),
    ):
        gh.fehler["seiten:issues"] = GitHubFehler(status, text, warten=60 if status == 429 else 0)
        bericht = abgleich.aktualisieren(akteur, verbindung)
        assert text in bericht["issues"]["fehler"]
        stand = Abrufstand.objects.get(verbindung=verbindung, bereich="issues")
        assert stand.letzter_erfolg == erfolg and text in stand.fehler
        if status in (401, 403, 404):
            assert "kein Beweis" in stand.fehler
    assert ExternesObjekt.objects.filter(typ="issue").count() == 1
    # Abgebrochener Teilabruf: nicht vollständig, Wasserlinie bleibt.
    seit = Abrufstand.objects.get(verbindung=verbindung, bereich="issues").seit
    neu = dict(gh.issues[1], id=202, number=2, title="Neu", updated_at="2026-11-05T10:00:00Z")
    gh.antworten["issues"] = lambda parameter, etag: ([neu], "e3", 200, False)
    abgleich.aktualisieren(akteur, verbindung)
    stand = Abrufstand.objects.get(verbindung=verbindung, bereich="issues")
    assert not stand.vollstaendig and stand.seit == seit and "unvollständig" in stand.fehler
    assert ExternesObjekt.objects.filter(externe_id="202").exists()


def test_p11_transport_paginierung_und_304(monkeypatch):
    app = github.GitHubApp("1", "unbenutzt", api="https://api.example")
    monkeypatch.setattr(app, "_token", lambda ziel: "t")
    seiten = {
        "https://api.example/repos/fred/test/issues?state=all&per_page=100": (
            200,
            [{"id": 1}],
            {"ETag": '"a"', "Link": '<https://api.example/n2>; rel="next"'},
        ),
        "https://api.example/n2": (
            200,
            [{"id": 2}],
            {"Link": '<https://api.example/n3>; rel="next"'},
        ),
        "https://api.example/n3": (200, [{"id": 3}], {}),
    }
    aufrufe = []

    def roh(methode, url, *, token, daten=None, etag="", zeit=15):
        aufrufe.append((url, etag))
        if etag == '"a"':
            return 304, None, {}
        return seiten[url]

    monkeypatch.setattr(app, "_roh", roh)

    class Ziel:
        name = "fred/test"

    eintraege, etag, status, voll = app.seiten(Ziel, "/issues", {"state": "all"})
    assert [e["id"] for e in eintraege] == [1, 2, 3] and etag == '"a"' and status == 200 and voll
    assert app.seiten(Ziel, "/issues", {"state": "all"}, etag='"a"')[2] == 304
    _, _, _, voll = app.seiten(Ziel, "/issues", {"state": "all"}, max_seiten=2)
    assert voll is False


def test_app_jwt_rs256():
    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    schluessel = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = schluessel.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    token = github.app_jwt("12345", pem, jetzt=1_800_000_000)
    inhalt = jwt.decode(
        token,
        schluessel.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert inhalt == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "12345"}


def test_p12_keine_mutation_ohne_passende_bestaetigung(
    akteur, projekt, projekt_b, verbindung, gh, bindung, zweiter_mensch
):
    # Falsches Projekt: ein Projektchat B sieht die Verbindung von A nicht.
    chat_b, _ = dienst.bindung_anlegen(akteur, projekt_b, name="Chat B")
    with pytest.raises(dienst.KeinZugriff):
        abgleich.aktion_vorbereiten(
            dienst.Akteur.aus_bindung(chat_b), verbindung, "issue_anlegen", {"titel": "x"}
        )
    for pfad in (
        "../x.md",
        "docs/../../etc.md",
        ".github/workflows/ci.md",
        "src/a.md",
        "docs/a.py",
        "/docs/a.md",
        "docs\\a.md",
    ):
        with pytest.raises(dienst.KeinZugriff):
            abgleich.aktion_vorbereiten(
                akteur, verbindung, "dokument_pr", {"pfad": pfad, "inhalt": "x"}
            )
    # Fehlendes Recht: Aktion nicht erlaubt.
    RepoVerbindung.objects.filter(pk=verbindung.pk).update(aktionen="kommentar")
    verbindung.refresh_from_db()
    with pytest.raises(dienst.KeinZugriff, match="nicht erlaubt"):
        abgleich.aktion_vorbereiten(akteur, verbindung, "issue_anlegen", {"titel": "x"})
    RepoVerbindung.objects.filter(pk=verbindung.pk).update(
        aktionen="issue_anlegen\ndokument_pr\nissue_aendern"
    )
    verbindung.refresh_from_db()
    # Chat schlägt vor, darf aber nicht bestätigen; ein Flag „genehmigt“ wirkt nicht.
    chat = dienst.Akteur.aus_bindung(bindung)
    a = abgleich.aktion_vorbereiten(
        chat, verbindung, "issue_anlegen", {"titel": "Vom Chat", "genehmigt": True}
    )
    assert "genehmigt" not in a.nutzlast and a.zustand == ExterneAktion.ENTWURF
    with pytest.raises(dienst.KeinZugriff):
        abgleich.aktion_bestaetigen(chat, a, revision=1, nutzlast_hash=a.nutzlast_hash)
    with pytest.raises(dienst.KeinZugriff):
        abgleich.aktion_bestaetigen(
            dienst.Akteur.aus_benutzer(zweiter_mensch), a, revision=1, nutzlast_hash=a.nutzlast_hash
        )
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_ausfuehren(akteur, a)
    # Bestätigung mit falschem Hash oder alter Revision greift nicht.
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_bestaetigen(akteur, a, revision=1, nutzlast_hash="0" * 64)
    a = _bestaetigt(akteur, a)
    # Geänderte Nutzlast hebt die Bestätigung auf.
    a = abgleich.aktion_aendern(akteur, a, {"titel": "Geändert"}, erwartete_revision=1)
    assert a.zustand == ExterneAktion.ENTWURF and a.bestaetigter_hash == ""
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_ausfuehren(akteur, a)
    # Abgelaufene Bestätigung.
    a = _bestaetigt(akteur, a)
    ExterneAktion.objects.filter(pk=a.pk).update(
        bestaetigt_am=timezone.now() - timedelta(minutes=16)
    )
    with pytest.raises(dienst.Konflikt, match="15 Minuten"):
        abgleich.aktion_ausfuehren(akteur, a)
    # Veralteter Basisstand (Datei inzwischen geändert) und geänderte Workflows: Konflikt.
    d = abgleich.aktion_vorbereiten(
        akteur, verbindung, "dokument_pr", {"pfad": "docs/a.md", "inhalt": "neu\n"}
    )
    d = _bestaetigt(akteur, d)
    gh.dateien[("c0ffee", "docs/a.md")] = {"sha": "s2", "inhalt": "fremd\n"}
    abgleich.aktion_ausfuehren(akteur, d)
    d.refresh_from_db()
    assert d.zustand == ExterneAktion.KONFLIKT and "Ausgangsstand" in d.fehler
    w = abgleich.aktion_vorbereiten(
        akteur, verbindung, "dokument_pr", {"pfad": "docs/a.md", "inhalt": "neu\n"}
    )
    gh.workflows = ("w2", ["ci.yml", "deploy.yml"])
    w = _bestaetigt(akteur, w)
    abgleich.aktion_ausfuehren(akteur, w)
    w.refresh_from_db()
    assert w.zustand == ExterneAktion.KONFLIKT and "Workflows" in w.fehler
    # Widerruf sperrt sofort.
    r = _bestaetigt(
        akteur, abgleich.aktion_vorbereiten(akteur, verbindung, "issue_anlegen", {"titel": "R"})
    )
    abgleich.verbindung_widerrufen(akteur, verbindung)
    r.refresh_from_db()
    assert r.zustand == ExterneAktion.VERWORFEN
    assert gh.schreiben == []


def test_p13_ablauf_gegen_attrappe(owner_client, akteur, projekt, verbindung, gh):
    """Vollständiger Ablauf über die Oberfläche; ersetzt nicht den Test im echten Repository."""
    url = reverse("github_aktion_neu", kwargs={"kennung": projekt.kennung, "pk": verbindung.pk})
    antwort = owner_client.post(
        url, {"art": "issue_anlegen", "titel": "Fehler beim Druck", "text": "Bitte prüfen"}
    )
    a = ExterneAktion.objects.get()
    assert antwort["Location"].endswith(a.aktions_id + "/") and gh.schreiben == []
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "Genaue Vorschau" in seite and "Fehler beim Druck" in seite

    def schritt(s):
        return reverse(
            "github_aktion_schritt",
            kwargs={"kennung": projekt.kennung, "aktions_id": a.aktions_id, "schritt": s},
        )

    owner_client.post(schritt("bestaetigen"), {"revision": a.revision, "hash": a.nutzlast_hash})
    owner_client.post(schritt("ausfuehren"))
    a.refresh_from_db()
    assert a.zustand == ExterneAktion.ERFOLGREICH and a.ergebnis_url.endswith("/i/2")
    assert a.schritte[0]["nummer"] == 2 and f"kipm-aktion:{a.aktions_id}" in gh.issues[2]["body"]
    k = _bestaetigt(
        akteur,
        abgleich.aktion_vorbereiten(
            akteur, verbindung, "kommentar", {"nummer": 2, "text": "Ergänzung"}
        ),
    )
    abgleich.aktion_ausfuehren(akteur, k)
    d = _bestaetigt(
        akteur,
        abgleich.aktion_vorbereiten(
            akteur,
            verbindung,
            "dokument_pr",
            {"pfad": "docs/a.md", "inhalt": "neu\n", "titel": "Doku"},
        ),
    )
    seite = owner_client.get(
        reverse("github_aktion", kwargs={"kennung": projekt.kennung, "aktions_id": d.aktions_id})
    ).content.decode()
    assert "-alt" in seite and "+neu" in seite and "kein Merge" in seite and "ci.yml" in seite
    abgleich.aktion_ausfuehren(akteur, d)
    d.refresh_from_db()
    assert d.zustand == ExterneAktion.ERFOLGREICH and [s["schritt"] for s in d.schritte] == [
        "branch",
        "datei",
        "pr",
    ]
    assert ("c0ffee", "docs/a.md") in gh.dateien and gh.dateien[("c0ffee", "docs/a.md")][
        "inhalt"
    ] == "alt\n"
    assert [w[0] for w in gh.schreiben] == ["issue", "kommentar", "branch", "datei", "pr"]
    assert not any(w[0] in ("merge", "release", "deploy") for w in gh.schreiben)


def test_p14_doppelklick_timeout_abbruch_und_teilfehler(akteur, verbindung, gh):
    a = _bestaetigt(
        akteur,
        abgleich.aktion_vorbereiten(akteur, verbindung, "issue_anlegen", {"titel": "Einmal"}),
    )
    abgleich.aktion_ausfuehren(akteur, a)
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_ausfuehren(akteur, a)
    assert gh.schreiben == [("issue", 2)]
    # Zeitüberschreitung nach möglichem Erfolg: unklar, kein erneutes Senden, Abgleich per Marke.
    b = _bestaetigt(
        akteur,
        abgleich.aktion_vorbereiten(akteur, verbindung, "issue_anlegen", {"titel": "Vielleicht"}),
    )
    gh.fehler["issue_anlegen:nachher"] = GitHubFehler(0, "Zeitüberschreitung")
    abgleich.aktion_ausfuehren(akteur, b)
    b.refresh_from_db()
    assert b.zustand == ExterneAktion.UNKLAR and len(gh.schreiben) == 2
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_ausfuehren(akteur, b)
    abgleich.aktion_abgleichen(akteur, b)
    b.refresh_from_db()
    assert b.zustand == ExterneAktion.ERFOLGREICH and len(gh.schreiben) == 2
    # Zeitüberschreitung vor Wirkung: Abgleich findet nichts, Mensch klärt, dann neue Bestätigung.
    c = _bestaetigt(
        akteur, abgleich.aktion_vorbereiten(akteur, verbindung, "issue_anlegen", {"titel": "Nie"})
    )
    gh.fehler["issue_anlegen:vorher"] = GitHubFehler(0, "Zeitüberschreitung")
    abgleich.aktion_ausfuehren(akteur, c)
    abgleich.aktion_abgleichen(akteur, c)
    c.refresh_from_db()
    assert c.zustand == ExterneAktion.UNKLAR and "0 Treffer" in c.fehler
    abgleich.aktion_nicht_ausgefuehrt(akteur, c, "Issues bei GitHub angesehen, keins vorhanden")
    c.refresh_from_db()
    assert c.zustand == ExterneAktion.ENTWURF and c.bestaetigter_hash == ""
    # Prozessabbruch: hängende Ausführung wird nicht blind neu gestartet.
    ExterneAktion.objects.filter(pk=c.pk).update(
        zustand=ExterneAktion.IN_AUSFUEHRUNG, ausfuehrung_am=timezone.now()
    )
    c.refresh_from_db()
    with pytest.raises(dienst.Konflikt, match="läuft gerade"):
        abgleich.aktion_abgleichen(akteur, c)
    with pytest.raises(dienst.Konflikt):
        abgleich.aktion_ausfuehren(akteur, c)
    # Branch erstellt, PR fehlgeschlagen: Teilergebnis, Fortsetzen erzeugt genau einen PR.
    d = _bestaetigt(
        akteur,
        abgleich.aktion_vorbereiten(
            akteur, verbindung, "dokument_pr", {"pfad": "docs/neu.md", "inhalt": "x\n"}
        ),
    )
    gh.fehler["pr_anlegen"] = GitHubFehler(422, "Validation Failed")
    abgleich.aktion_ausfuehren(akteur, d)
    d.refresh_from_db()
    assert d.zustand == ExterneAktion.TEILWEISE and [s["schritt"] for s in d.schritte] == [
        "branch",
        "datei",
    ]
    abgleich.aktion_ausfuehren(akteur, d, fortsetzen=True)
    d.refresh_from_db()
    assert d.zustand == ExterneAktion.ERFOLGREICH and len(gh.prs) == 1
    assert [w[0] for w in gh.schreiben].count("branch") == 1


def test_p15_konkurrierende_issue_und_dokumentaenderung(
    owner_client, akteur, projekt, verbindung, gh
):
    a = abgleich.aktion_vorbereiten(
        akteur, verbindung, "issue_aendern", {"nummer": 1, "titel": "Neu", "zustand": "closed"}
    )
    seite = owner_client.get(
        reverse("github_aktion", kwargs={"kennung": projekt.kennung, "aktions_id": a.aktions_id})
    ).content.decode()
    assert "keine atomare Sperre" in seite and "ergänzender Kommentar" in seite
    a = _bestaetigt(akteur, a)
    gh.issues[1]["body"] = "Jemand anderes hat geändert"
    abgleich.aktion_ausfuehren(akteur, a)
    a.refresh_from_db()
    assert a.zustand == ExterneAktion.KONFLIKT and gh.schreiben == []
    # Neue Vorschau auf aktuellem Stand ist möglich.
    a = abgleich.aktion_aendern(
        akteur, a, {"nummer": 1, "titel": "Neu"}, erwartete_revision=a.revision
    )
    assert a.basis["text"] == "Jemand anderes hat geändert"
    d = abgleich.aktion_vorbereiten(
        akteur, verbindung, "dokument_pr", {"pfad": "docs/a.md", "inhalt": "mein Stand\n"}
    )
    gh.dateien[("c0ffee", "docs/a.md")] = {"sha": "s9", "inhalt": "fremder Stand\n"}
    d = abgleich.aktion_aendern(
        akteur, d, {"pfad": "docs/a.md", "inhalt": "mein Stand\n"}, erwartete_revision=1
    )
    assert d.revision == 2 and d.basis["alter_inhalt"] == "fremder Stand\n"


def test_api_github_vorschlag_bleibt_entwurf(client, verbindung, gh, bindung):
    antwort = client.post(
        reverse("api_github_vorschlag"),
        json.dumps(
            {
                "anfrage_kennung": "g1",
                "repository": "fred/test",
                "art": "issue_anlegen",
                "nutzlast": {"titel": "Vom Chat"},
                "genehmigt": True,
            }
        ),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}",
    )
    assert antwort.status_code == 201 and antwort.json()["aktion"]["zustand"] == "entwurf"
    assert gh.schreiben == []

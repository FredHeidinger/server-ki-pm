"""GitHub im Dienst: Lese-Schlüssel, Repository je Projekt, Versionszeile. KI-PM liest bei GitHub
nur, schreibt nie.

Der Lese-Schlüssel gehört allein dem Owner: nur er setzt, prüft und trennt ihn, KI-Zugänge erreichen
ihn nicht. Er wird verschlüsselt gespeichert und verlässt diesen Dienst nur Richtung ``github``.
Die Versionszeile liest bei GitHub und merkt sich das Gelesene in ``VersionStand``; ein Fehlschlag
wird eine verständliche Zeile, das zuletzt Gelesene bleibt sichtbar. Das Lesen schreibt kein Ereignis.
"""

import base64
import re
import time
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.views.decorators.debug import sensitive_variables

from . import github
from .dienst import (
    Akteur,
    DienstFehler,
    Konflikt,
    _ereignis,
    _mensch_erforderlich,
    _owner_erforderlich,
    _projekt_sichtbar,
)
from .models import (
    GitHubZugang,
    Projekt,
    RepoVerbindung,
    VersionStand,
)

VERSION_ALTER = timedelta(minutes=10)
ANZEIGE_SEKUNDEN = 3
UMGEBUNG_RUECKBLICK = 5
UMGEBUNGEN_HOECHSTENS = 10
UMGEBUNGEN_IN_ZEILE = 4
PRODUKTIONSNAMEN = {"produktion", "production", "prod", "live", "betrieb"}
SCHLUESSEL_FORM = re.compile(r"[\x21-\x7e]{1,1000}")


# --- Lese-Schlüssel --------------------------------------------------------------------------


class SchluesselUnlesbar(github.GitHubFehler):
    """Der gespeicherte Schlüssel lässt sich nicht entschlüsseln (Servergeheimnis geändert)."""

    def __init__(self):
        super().__init__(401, "Schlüssel nicht lesbar")


def fehlerzeile(fehler: github.GitHubFehler, repository: str = "") -> str:
    """Verständliche Zeile statt Fehlerseite; nennt nie Schlüssel oder Rohantwort."""
    ziel = f"Repository {repository}" if repository else "Das Repository"
    if isinstance(fehler, SchluesselUnlesbar):
        return (
            "Der gespeicherte Schlüssel ist nicht mehr lesbar. Bitte unter „Konto & KI verbinden“ "
            "neu einfügen."
        )
    if fehler.status == 401:
        return (
            "GitHub lehnt den Schlüssel ab (ungültig oder abgelaufen). Bitte unter „Konto & KI "
            "verbinden“ einen neuen einfügen."
        )
    if fehler.status in (403, 429) and (fehler.warten or "rate limit" in str(fehler).lower()):
        minuten = max(1, round(fehler.warten / 60))
        return f"GitHub begrenzt gerade die Abfragen. In etwa {minuten} Minuten geht es wieder."
    if fehler.status == 403:
        return (
            f"Kein Zugriff: Der Schlüssel darf {ziel if repository else 'das'} nicht lesen. Er "
            "braucht die Rechte Contents und Deployments (nur lesen)."
        )
    if fehler.status == 404:
        return f"{ziel} fehlt: GitHub kennt es nicht, oder der Schlüssel sieht es nicht."
    if fehler.status == 0:
        return (
            "GitHub nicht erreichbar (Zeitüberschreitung oder Netzproblem). Später erneut prüfen."
        )
    if fehler.status >= 500:
        return f"GitHub hat gerade eine Störung (Status {fehler.status}). Später erneut prüfen."
    return f"GitHub antwortet unerwartet (Status {fehler.status}). Später erneut prüfen."


def _fernet() -> Fernet:
    abgeleitet = salted_hmac("kipm.github.lese-schluessel", "fernet", algorithm="sha256").digest()
    return Fernet(base64.urlsafe_b64encode(abgeleitet))


@sensitive_variables()
def _lesezugang(zugang: GitHubZugang):
    """Adapter mit dem entschlüsselten Schlüssel; er wird nirgends sonst entschlüsselt."""
    try:
        schluessel = _fernet().decrypt(zugang.schluessel.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        raise SchluesselUnlesbar() from None
    return github.adapter(schluessel)


def _pruefen(zugang: GitHubZugang) -> GitHubZugang:
    """Schlüssel bei GitHub prüfen und das Ergebnis (ohne Schlüssel) festhalten."""
    jetzt = timezone.now()
    try:
        gh = _lesezugang(zugang)
        konto = gh.konto()
        repos = gh.repositorys()
    except github.GitHubFehler as fehler:
        # Eine Störung beim Prüfen sagt nichts darüber, ob der Schlüssel wieder taugt.
        abgelehnt = fehler.status == 401 or zugang.abgelehnt
        aenderung = {
            "fehler": fehlerzeile(fehler)[:300],
            "abgelehnt": abgelehnt,
            "geprueft_am": jetzt,
        }
        if abgelehnt:
            aenderung |= {"konto": "", "repositorys": [], "hinweis": ""}
    else:
        schreibrechte = [b for b in konto["bereiche"] if b in ("repo", "public_repo", "write:org")]
        aenderung = {
            "konto": konto["login"][:100],
            "repositorys": sorted(repos, key=lambda r: r["name"].lower()),
            "hinweis": (
                f"Dieser Schlüssel darf mehr als lesen ({', '.join(schreibrechte)}). KI-PM liest "
                "nur; sicherer ist ein Schlüssel nur mit Leserechten."
                if schreibrechte
                else ""
            ),
            "fehler": "",
            "abgelehnt": False,
            "geprueft_am": jetzt,
        }
    GitHubZugang.objects.filter(pk=zugang.pk).update(**aenderung)
    if not aenderung["fehler"]:
        # Alte Fehlerzeilen gelten nicht mehr; die Seiten lesen beim nächsten Aufruf neu.
        VersionStand.objects.exclude(fehler="").update(
            fehler="", abgerufen_am=jetzt - VERSION_ALTER
        )
    zugang.refresh_from_db()
    return zugang


@sensitive_variables("schluessel")
def github_verbinden(akteur: Akteur, schluessel: str) -> GitHubZugang:
    """Schlüssel verschlüsselt speichern und gleich prüfen. Ein vorhandener, gültiger wird nur
    ersetzt, wenn GitHub den neuen bestätigt: Er lässt sich nie wieder anzeigen."""
    _owner_erforderlich(akteur, "GitHub verbinden")
    _mensch_erforderlich(akteur, "GitHub verbinden")
    schluessel = (schluessel or "").strip()
    if not SCHLUESSEL_FORM.fullmatch(schluessel):
        raise DienstFehler("Das sieht nicht nach einem GitHub-Schlüssel aus; bitte neu kopieren.")
    vorhanden = GitHubZugang.objects.first()
    if vorhanden is not None and not vorhanden.abgelehnt:
        try:
            github.adapter(schluessel).konto()
        except github.GitHubFehler as fehler:
            raise DienstFehler(
                f"{fehlerzeile(fehler)} Der bisherige Schlüssel bleibt gespeichert."
            ) from None
    with transaction.atomic():
        GitHubZugang.objects.all().delete()
        zugang = GitHubZugang.objects.create(
            schluessel=_fernet().encrypt(schluessel.encode("ascii")).decode("ascii")
        )
        _ereignis(
            akteur,
            projekt=None,
            objekt_typ="GitHub",
            objekt_kennung="Lese-Schlüssel",
            ereignis="Lese-Schlüssel gespeichert",
        )
    return _pruefen(zugang)


def github_pruefen(akteur: Akteur) -> GitHubZugang:
    _owner_erforderlich(akteur, "GitHub prüfen")
    zugang = GitHubZugang.objects.first()
    if zugang is None:
        raise DienstFehler("GitHub ist nicht verbunden.")
    return _pruefen(zugang)


def github_trennen(akteur: Akteur) -> None:
    """Löscht den Schlüssel und alles daraus Gelesene; die Repository-Wahl der Projekte bleibt."""
    _owner_erforderlich(akteur, "GitHub trennen")
    with transaction.atomic():
        geloescht, _ = GitHubZugang.objects.all().delete()
        if not geloescht:
            raise Konflikt("GitHub ist nicht verbunden.")
        VersionStand.objects.all().delete()
        _ereignis(
            akteur,
            projekt=None,
            objekt_typ="GitHub",
            objekt_kennung="Lese-Schlüssel",
            ereignis="Lese-Schlüssel gelöscht",
        )


def zugang_anzeige(akteur: Akteur) -> dict:
    """Alles, was Seiten über den Zugang erfahren; enthält den Schlüssel nie. Konto und Namen
    auch privater Repositorys sieht nur der Owner."""
    zugang = GitHubZugang.objects.first()
    if zugang is None or not akteur.ist_owner:
        return {"verbunden": zugang is not None, "repositorys": [], "owner": akteur.ist_owner}
    return {
        "verbunden": True,
        "owner": True,
        "konto": zugang.konto,
        "anzahl": len(zugang.repositorys),
        "repositorys": zugang.repositorys,
        "hinweis": zugang.hinweis,
        "fehler": zugang.fehler,
        "geprueft_am": zugang.geprueft_am,
    }


# --- Repository je Projekt -------------------------------------------------------------------


def aktives_repo(projekt: Projekt) -> RepoVerbindung | None:
    return projekt.repos.filter(widerrufen_am=None).order_by("-pk").first()


def repository_waehlen(akteur: Akteur, projekt: Projekt, name: str) -> RepoVerbindung | None:
    """Das Repository des Projekts festlegen (leer = keines). KI-PM liest dort nur."""
    _owner_erforderlich(akteur, "Das GitHub-Repository wählen")
    name = (name or "").strip()
    info = None
    if name:
        zugang = GitHubZugang.objects.first()
        if zugang is None:
            raise DienstFehler("GitHub ist nicht verbunden.")
        try:
            info = _lesezugang(zugang).repository(name)
        except github.GitHubFehler as fehler:
            raise DienstFehler(fehlerzeile(fehler, name)) from fehler
    with transaction.atomic():
        andere = projekt.repos.filter(widerrufen_am=None)
        if info:
            andere = andere.exclude(host="github.com", github_id=info["id"])
        andere.update(widerrufen_am=timezone.now())
        VersionStand.objects.filter(projekt=projekt).delete()
        verbindung = None
        if info:
            verbindung, _ = RepoVerbindung.objects.update_or_create(
                projekt=projekt,
                host="github.com",
                github_id=info["id"],
                defaults={
                    "name": info["name"],
                    "standardbranch": info["standardbranch"],
                    "schreibend": False,
                    "widerrufen_am": None,
                },
            )
        _ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="GitHub",
            objekt_kennung=info["name"] if info else "Repository",
            ereignis="GitHub-Repository gewählt" if info else "GitHub-Repository entfernt",
        )
    if verbindung:
        version_abrufen(akteur, projekt)
    return verbindung


# --- Versionszeile ---------------------------------------------------------------------------


def _ist_produktion(umgebung: str, flagge: bool) -> bool:
    return flagge or umgebung.strip().lower() in PRODUKTIONSNAMEN


def _version_von(bereitstellung: dict, tags: list[dict]) -> tuple[str, bool]:
    """Anzeigename einer Bereitstellung und ob er ein Release-Tag ist."""
    ref, sha = bereitstellung["ref"], bereitstellung["sha"]
    namen = {t["name"] for t in tags}
    if ref in namen:
        return ref, True
    passend = next((t["name"] for t in tags if sha and t["sha"] == sha), None)
    if passend:
        return passend, True
    return f"Commit {sha[:7]}" if sha else (ref or "unbekannt"), False


def _umgebungen_lesen(gh, name: str, bereitstellungen: list[dict], tags: list[dict]) -> list[dict]:
    """Je Umgebung die jüngste wirksame Bereitstellung. „inactive“ ist abgelöst und zählt nicht;
    Umgebungen ganz ohne wirksame Bereitstellung und ohne Problem fallen weg."""
    gruppen: dict[str, list[dict]] = {}
    for b in sorted(bereitstellungen, key=lambda b: (b["erstellt"], b["id"] or 0), reverse=True):
        if b["umgebung"]:
            gruppen.setdefault(b["umgebung"], []).append(b)
    # Je Bereitstellung kostet der Zustand eine Anfrage: nur die ersten, Produktion zuerst.
    gewaehlt = sorted(
        gruppen.items(),
        key=lambda g: not any(_ist_produktion(g[0], b["produktion"]) for b in g[1]),
    )[:UMGEBUNGEN_HOECHSTENS]
    umgebungen = []
    for umgebung, liste in gewaehlt:
        eintrag = {
            "name": umgebung,
            "version": "unbekannt",
            "tag": False,
            "sha": "",
            "ref": "",
            "produktion": any(_ist_produktion(umgebung, b["produktion"]) for b in liste),
            "ohne_status": False,
            "problem": "",
            "problem_version": "",
        }
        for b in liste[:UMGEBUNG_RUECKBLICK]:
            zustand = gh.bereitstellung_zustand(name, b["id"])
            version, ist_tag = _version_von(b, tags)
            if zustand in ("success", ""):
                eintrag |= {
                    "version": version,
                    "tag": ist_tag,
                    "sha": b["sha"],
                    "ref": b["ref"],
                    "ohne_status": zustand == "",
                }
                break
            if zustand != "inactive" and not eintrag["problem"]:
                eintrag |= {"problem": zustand, "problem_version": version}
        if eintrag["version"] != "unbekannt" or eintrag["problem"]:
            umgebungen.append(eintrag)
    return sorted(umgebungen, key=lambda u: (not u["produktion"], u["name"].lower()))


def _aktuell(eintrag: dict, release: dict, tags: list[dict]) -> bool:
    tag = release["tag"]
    if eintrag["ref"] == tag or eintrag["version"] == tag:
        return True
    return any(t["name"] == tag and t["sha"] == eintrag["sha"] for t in tags)


def _bewerten(release: dict | None, umgebungen: list[dict], tags: list[dict]) -> tuple[str, list]:
    """Zustand und Abweichungen (Was, Warum). Gibt es Produktion, entscheidet nur sie."""
    massgeblich = [u for u in umgebungen if u["produktion"]] or umgebungen
    abweichungen = []
    for u in massgeblich:
        if u["problem"] in ("failure", "error"):
            abweichungen.append(
                {
                    "was": f"Die letzte Bereitstellung nach {u['name']} ({u['problem_version']}) ist fehlgeschlagen.",
                    "warum": f"GitHub meldet den Zustand „{u['problem']}“; dort läuft weiter {u['version']}.",
                }
            )
        elif u["problem"]:
            abweichungen.append(
                {
                    "was": f"Eine Bereitstellung nach {u['name']} ({u['problem_version']}) läuft noch.",
                    "warum": f"GitHub meldet den Zustand „{u['problem']}“; bis zum Erfolg zählt {u['version']}.",
                }
            )
        if u["ohne_status"]:
            abweichungen.append(
                {
                    "was": f"Die Bereitstellung {u['version']} nach {u['name']} hat keine Erfolgsmeldung.",
                    "warum": "Bei GitHub ist kein Zustand eingetragen; KI-PM zählt sie als installiert.",
                }
            )
    if release is None:
        if umgebungen:
            abweichungen.append(
                {
                    "was": "Es gibt Bereitstellungen, aber kein veröffentlichtes Release.",
                    "warum": "Ohne Release ist nichts da, womit KI-PM vergleichen könnte.",
                }
            )
        return VersionStand.UNBEKANNT, abweichungen
    bekannt = [u for u in massgeblich if u["version"] != "unbekannt"]
    if not bekannt:
        abweichungen.append(
            {
                "was": f"Release {release['tag']} ist veröffentlicht, aber keine Bereitstellung bekannt.",
                "warum": "Bei GitHub ist keine erfolgreiche Bereitstellung eingetragen; der installierte Stand bleibt unbekannt.",
            }
        )
        return VersionStand.UNBEKANNT, abweichungen
    alle_aktuell = True
    for u in bekannt:
        if _aktuell(u, release, tags):
            continue
        alle_aktuell = False
        abweichungen.append(
            {
                "was": f"{u['name']} läuft {u['version']}, neueste ist {release['tag']}.",
                "warum": (
                    f"Release {release['tag']} ist dort nicht bereitgestellt; {u['version']} kann älter oder neuer sein (etwa eine Vorabversion)."
                    if u["tag"]
                    else f"Dieser Commit gehört zu keinem Release; ob er älter oder neuer als {release['tag']} ist, sagt GitHub hier nicht."
                ),
            }
        )
    return (VersionStand.AKTUELL if alle_aktuell else VersionStand.NICHT_AKTUELL), abweichungen


def _version_lesen(gh, verbindung: RepoVerbindung) -> dict:
    """Alles für die Versionszeile bei GitHub lesen, nur GET."""
    repo = gh.repository(verbindung.name)
    if repo["id"] != verbindung.github_id:
        raise github.GitHubFehler(404, "Repository hat eine andere ID")
    if repo["name"] and repo["name"] != verbindung.name:
        RepoVerbindung.objects.filter(pk=verbindung.pk).update(name=repo["name"])
        verbindung.name = repo["name"]
    release = gh.neuestes_release(verbindung.name)
    bereitstellungen = gh.bereitstellungen(verbindung.name)
    tags = gh.tags(verbindung.name) if bereitstellungen else []
    umgebungen = _umgebungen_lesen(gh, verbindung.name, bereitstellungen, tags)
    zustand, abweichungen = _bewerten(release, umgebungen, tags)
    return {
        "neueste": release["tag"] if release else "",
        "umgebungen": umgebungen,
        "abweichungen": abweichungen,
        "zustand": zustand,
    }


def _abrufen(projekt: Projekt, zugang: GitHubZugang) -> github.GitHubFehler | None:
    """Abruf für ein Projekt; das Netz läuft außerhalb jeder Transaktion. Rückgabe: der Fehlschlag."""
    verbindung = aktives_repo(projekt)
    if verbindung is None:
        return None
    jetzt = timezone.now()
    try:
        daten = _version_lesen(_lesezugang(zugang), verbindung)
    except github.GitHubFehler as fehler:
        zeile = fehlerzeile(fehler, verbindung.name)[:300]
        VersionStand.objects.update_or_create(
            projekt=projekt, defaults={"fehler": zeile, "abgerufen_am": jetzt}
        )
        if fehler.status == 401:
            GitHubZugang.objects.filter(pk=zugang.pk).update(
                abgelehnt=True,
                fehler=zeile,
                konto="",
                repositorys=[],
                hinweis="",
                geprueft_am=jetzt,
            )
        return fehler
    VersionStand.objects.update_or_create(
        projekt=projekt,
        defaults={**daten, "fehler": "", "abgerufen_am": jetzt, "gelesen_am": jetzt},
    )
    if zugang.abgelehnt:
        _pruefen(zugang)  # Das Lesen hat geklappt: Der Schlüssel taugt, die Ablehnung ist überholt.
    return None


def version_abrufen(akteur: Akteur, projekt: Projekt) -> None:
    """„Jetzt prüfen“: ein Projekt sofort lesen."""
    _projekt_sichtbar(akteur, projekt)
    zugang = GitHubZugang.objects.first()
    if zugang is None:
        raise DienstFehler("GitHub ist nicht verbunden.")
    _abrufen(projekt, zugang)


def versionen_auffrischen(projekte: list[Projekt]) -> None:
    """Beim Seitenaufruf Veraltetes nachlesen, ohne die Seite aufzuhalten: Schluss nach wenigen
    Sekunden, nach einem Netzfehler und bei abgelehntem Schlüssel gar nicht erst."""
    zugang = GitHubZugang.objects.first()
    if zugang is None or zugang.abgelehnt:
        return
    grenze = timezone.now() - VERSION_ALTER
    ende = time.monotonic() + ANZEIGE_SEKUNDEN
    frisch = set(
        VersionStand.objects.filter(projekt__in=projekte, abgerufen_am__gte=grenze).values_list(
            "projekt_id", flat=True
        )
    )
    faellig = [p for p in projekte if p.pk not in frisch]
    for stelle, projekt in enumerate(faellig, 1):
        fehler = _abrufen(projekt, zugang)
        if fehler is not None and fehler.status == 0:
            # Die Störung gilt für alle: vermerken, ohne je Projekt erneut darauf zu warten.
            for rest in faellig[stelle:]:
                if aktives_repo(rest) is not None:
                    VersionStand.objects.update_or_create(
                        projekt=rest,
                        defaults={
                            "fehler": fehlerzeile(fehler)[:300],
                            "abgerufen_am": timezone.now(),
                        },
                    )
            return
        if (fehler is not None and fehler.status == 401) or time.monotonic() > ende:
            return


def _zeile(stand: VersionStand | None) -> str:
    umgebungen = stand.umgebungen if stand else []
    teile = [f"{u['name']}: {u['version']}" for u in umgebungen[:UMGEBUNGEN_IN_ZEILE]]
    if len(umgebungen) > UMGEBUNGEN_IN_ZEILE:
        teile.append(f"{len(umgebungen) - UMGEBUNGEN_IN_ZEILE} weitere")
    teile = teile or ["Produktion: unbekannt"]
    teile.append(f"neueste: {(stand.neueste if stand else '') or 'unbekannt'}")
    zeile = " · ".join(teile)
    if stand and stand.zustand != VersionStand.UNBEKANNT:
        zeile += f" → {stand.zustand}"
    return zeile


def version_anzeige(projekt: Projekt) -> dict:
    """Versionszeile, Abweichungen und Hinweis aus dem zuletzt Gelesenen; kein Netz."""
    zugang = GitHubZugang.objects.first()
    verbindung = aktives_repo(projekt)
    stand = None
    if zugang is not None and verbindung is not None:
        stand = VersionStand.objects.filter(projekt=projekt).first()
    fehler = stand.fehler if stand else ""
    if verbindung is not None and zugang is not None and zugang.abgelehnt:
        # Gilt für alle Projekte, auch solche, die seither nicht mehr gelesen wurden.
        fehler = zugang.fehler
    return {
        "zeile": _zeile(stand),
        # Was nicht gelesen werden konnte, nennt die Karte nicht „aktuell“.
        "zustand": stand.zustand if stand and not fehler else VersionStand.UNBEKANNT,
        "abweichungen": stand.abweichungen if stand else [],
        "umgebungen": stand.umgebungen if stand else [],
        "fehler": fehler,
        "gelesen_am": stand.gelesen_am if stand else None,
        "verbunden": zugang is not None,
        "repository": verbindung.name if verbindung else "",
    }

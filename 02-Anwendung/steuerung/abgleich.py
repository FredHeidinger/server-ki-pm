"""GitHub im Dienst: Verbindungen, Lesen mit Abrufstand, bestätigte Schreibaktionen.

GitHub bleibt führend für Code, Issues, Pull Requests und Releases; KI-PM speichert
Beobachtungen mit Quellstand. Schreiben läuft immer als ``ExterneAktion``: Entwurf mit exakter
Vorschau → Fred bestätigt genau diese Revision und Nutzlast → kurze Transaktion reserviert die
Ausführung (einmalig, höchstens 15 Minuten nach Bestätigung) → Netzaufrufe außerhalb der
Transaktion, jeder Schritt einzeln verbucht. Zeitüberschreitung heißt „Ausgang unklar“; dann
wird anhand der Aktionsmarke abgeglichen, nie blind wiederholt. Keine Merges, Releases,
Deployments, Workflow-Dateien oder Rechteänderungen.
"""

import fnmatch
import hashlib
import json
import uuid
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import github
from .dienst import (
    Akteur,
    DienstFehler,
    KeinZugriff,
    Konflikt,
    _ereignis,
    _owner_erforderlich,
    _projekt_sichtbar,
    zeilen,
)
from .models import Abrufstand, ExterneAktion, ExternesObjekt, Projekt, RepoVerbindung

BESTAETIGUNG_GUELTIG = timedelta(minutes=15)
UEBERLAPPUNG = timedelta(minutes=5)
DOKUMENTENDUNGEN = (".md", ".txt")
AKTIONEN = dict(RepoVerbindung.AKTIONEN)


def _gh():
    gh = github.adapter()
    if gh is None:
        raise DienstFehler(
            "GitHub ist auf diesem Server nicht eingerichtet (GitHub App fehlt). Lesen und Schreiben "
            "sind erst nach der Einrichtung möglich."
        )
    return gh


# --- Verbindungen ----------------------------------------------------------------------------


def verbindung_anlegen(
    akteur: Akteur,
    projekt: Projekt,
    *,
    name: str,
    schreibend: bool = False,
    aktionen: str = "",
    pfade: str = "docs/**",
) -> RepoVerbindung:
    """Repository an der stabilen GitHub-ID binden. Schreibend höchstens ein Projekt je Repo."""
    _owner_erforderlich(akteur, "GitHub-Verbindungen einrichten")
    unbekannt = [a for a in zeilen(aktionen) if a not in AKTIONEN]
    if unbekannt:
        raise DienstFehler("Unbekannte Aktion: " + ", ".join(unbekannt))
    for muster in zeilen(pfade):
        if muster.startswith(("/", ".github")) or ".." in muster:
            raise DienstFehler(f"Pfadmuster „{muster}“ ist nicht erlaubt.")
    gh = _gh()
    try:
        info = gh.repo_finden(name.strip())
    except github.GitHubFehler as fehler:
        raise DienstFehler(
            f"Repository nicht gefunden oder App nicht installiert: {fehler}"
        ) from fehler
    verbindung = RepoVerbindung(
        projekt=projekt,
        github_id=info["id"],
        name=info["name"],
        standardbranch=info["standardbranch"],
        installation_id=info["installation_id"],
        schreibend=schreibend,
        aktionen="\n".join(zeilen(aktionen)),
        erlaubte_pfade="\n".join(zeilen(pfade)) or "docs/**",
    )
    if schreibend:
        stand, dateien = gh.workflow_stand(verbindung, verbindung.standardbranch)
        verbindung.workflow_stand = stand
        verbindung.workflow_hinweis = (
            "Workflows im Standardbranch: "
            + ", ".join(dateien)
            + ". Ein Dokument-Pull-Request kann sie auslösen; Trigger und Kosten vor Aktivierung prüfen."
            if dateien
            else "Keine Workflow-Dateien im Standardbranch."
        )
    try:
        with transaction.atomic():
            verbindung.save()
            _ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="GitHub",
                objekt_kennung=verbindung.name,
                ereignis="Repository verbunden" + (" (schreibend)" if schreibend else " (lesend)"),
                anlass=f"GitHub-ID {verbindung.github_id}",
            )
    except IntegrityError as fehler:
        raise DienstFehler(
            "Dieses Repository ist hier schon verbunden oder schreibend einem anderen Projekt "
            "zugeordnet; schreibend ist genau ein Projekt je Repository erlaubt."
        ) from fehler
    return verbindung


def verbindung_widerrufen(akteur: Akteur, v: RepoVerbindung) -> None:
    """Sperrt sofort neue Abrufe und Schreibaktionen; Beobachtungen bleiben für den Verlauf."""
    _owner_erforderlich(akteur, "GitHub-Verbindungen widerrufen")
    with transaction.atomic():
        geaendert = RepoVerbindung.objects.filter(pk=v.pk, widerrufen_am=None).update(
            widerrufen_am=timezone.now()
        )
        if not geaendert:
            raise Konflikt("Die Verbindung ist bereits widerrufen.")
        ExterneAktion.objects.filter(
            verbindung=v, zustand__in=(ExterneAktion.ENTWURF, ExterneAktion.BESTAETIGT)
        ).update(zustand=ExterneAktion.VERWORFEN, fehler="Verbindung widerrufen")
        _ereignis(
            akteur,
            projekt=v.projekt,
            objekt_typ="GitHub",
            objekt_kennung=v.name,
            ereignis="Verbindung widerrufen",
        )


def verbindung_holen(akteur: Akteur, projekt: Projekt, pk: int) -> RepoVerbindung:
    _projekt_sichtbar(akteur, projekt)
    v = RepoVerbindung.objects.filter(projekt=projekt, pk=pk).first()
    if v is None:
        raise KeinZugriff("Nicht gefunden.")
    return v


# --- Lesen -----------------------------------------------------------------------------------

BEREICHE = {
    "issues": ("/issues", {"state": "all", "sort": "updated", "direction": "asc"}),
    "releases": ("/releases", {}),
    "checks": ("/commits/{branch}/check-runs", {}),
    "deployments": ("/deployments", {}),
}


def _zeit(text: str | None):
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _beobachtung(bereich: str, e: dict) -> tuple[str, dict]:
    if bereich == "issues":
        typ = "pull" if "pull_request" in e else "issue"
        return typ, {
            "externe_id": str(e["id"]),
            "nummer": e.get("number"),
            "titel": e.get("title", ""),
            "zustand": e.get("state", ""),
            "url": e.get("html_url", ""),
            "quell_aktualisiert": _zeit(e.get("updated_at")),
            "daten": {
                "autor": (e.get("user") or {}).get("login", ""),
                "labels": [x.get("name", "") for x in e.get("labels", [])][:10],
                "text": (e.get("body") or "")[:2000],
            },
        }
    if bereich == "releases":
        return "release", {
            "externe_id": str(e["id"]),
            "titel": e.get("name") or e.get("tag_name", ""),
            "zustand": "Entwurf" if e.get("draft") else "veröffentlicht",
            "url": e.get("html_url", ""),
            "quell_aktualisiert": _zeit(e.get("published_at") or e.get("created_at")),
            "daten": {"tag": e.get("tag_name", ""), "commit": e.get("target_commitish", "")},
        }
    if bereich == "checks":
        return "check", {
            "externe_id": str(e["id"]),
            "titel": e.get("name", ""),
            "zustand": e.get("conclusion") or e.get("status", ""),
            "url": e.get("html_url", ""),
            "quell_aktualisiert": _zeit(e.get("completed_at") or e.get("started_at")),
            "daten": {"commit": e.get("head_sha", "")},
        }
    return "deployment", {
        "externe_id": str(e["id"]),
        "titel": e.get("environment", ""),
        "zustand": e.get("task", ""),
        "url": "",
        "quell_aktualisiert": _zeit(e.get("updated_at") or e.get("created_at")),
        "daten": {"commit": e.get("sha", ""), "ref": e.get("ref", "")},
    }


def _speichern(v: RepoVerbindung, bereich: str, eintraege: list[dict]) -> tuple[int, object]:
    """Beobachtungen übernehmen; doppelte oder ältere Antworten ändern nichts."""
    geaendert, neueste = 0, None
    jetzt = timezone.now()
    for e in eintraege:
        typ, felder = _beobachtung(bereich, e)
        quelle = felder["quell_aktualisiert"]
        if quelle and (neueste is None or quelle > neueste):
            neueste = quelle
        alt = ExternesObjekt.objects.filter(
            verbindung=v, typ=typ, externe_id=felder["externe_id"]
        ).first()
        if alt is None:
            ExternesObjekt.objects.create(verbindung=v, typ=typ, zuletzt_gesehen=jetzt, **felder)
            geaendert += 1
        elif alt.quell_aktualisiert and quelle and quelle < alt.quell_aktualisiert:
            continue  # ältere Antwort überschreibt keine jüngere Beobachtung
        else:
            neu = {k: w for k, w in felder.items() if getattr(alt, k) != w}
            ExternesObjekt.objects.filter(pk=alt.pk).update(zuletzt_gesehen=jetzt, **neu)
            geaendert += bool(neu)
    return geaendert, neueste


def aktualisieren(akteur: Akteur, v: RepoVerbindung) -> dict:
    """Manuelles „GitHub aktualisieren“: Netz außerhalb der Transaktion, je Bereich eigener
    Abrufstand. Ein abgebrochener Teilabruf rückt die Wasserlinie nicht vor."""
    _projekt_sichtbar(akteur, v.projekt)
    if not v.aktiv:
        raise Konflikt("Die Verbindung ist widerrufen.")
    gh = _gh()
    bericht = {}
    gesamt = 0
    for bereich, (pfad, parameter) in BEREICHE.items():
        stand, _ = Abrufstand.objects.get_or_create(verbindung=v, bereich=bereich)
        parameter = dict(parameter)
        if bereich == "issues" and stand.seit:
            parameter["since"] = (stand.seit - UEBERLAPPUNG).isoformat()
        jetzt = timezone.now()
        try:
            eintraege, etag, status, vollstaendig = gh.seiten(
                v, pfad.format(branch=v.standardbranch), parameter, etag=stand.etag
            )
        except github.GitHubFehler as fehler:
            text = str(fehler) + (f" (erneut in {fehler.warten} s)" if fehler.warten else "")
            if fehler.status in (401, 403, 404):
                text += " – fehlende Berechtigung ist kein Beweis für „nicht vorhanden“"
            Abrufstand.objects.filter(pk=stand.pk).update(letzter_versuch=jetzt, fehler=text[:300])
            bericht[bereich] = {"fehler": text}
            continue
        with transaction.atomic():
            geaendert, neueste = _speichern(v, bereich, eintraege)
            aenderung = {
                "letzter_versuch": jetzt,
                "fehler": ""
                if vollstaendig
                else "Abruf unvollständig (Seitengrenze); Stand nicht vollständig",
                "vollstaendig": vollstaendig,
            }
            if vollstaendig:
                aenderung["letzter_erfolg"] = jetzt
                aenderung["etag"] = etag[:200]
                if neueste and (stand.seit is None or neueste > stand.seit):
                    aenderung["seit"] = neueste
            Abrufstand.objects.filter(pk=stand.pk).update(**aenderung)
        gesamt += geaendert
        bericht[bereich] = {"status": status, "geaendert": geaendert, "vollstaendig": vollstaendig}
    if gesamt:
        with transaction.atomic():
            _ereignis(
                akteur,
                projekt=v.projekt,
                objekt_typ="GitHub",
                objekt_kennung=v.name,
                ereignis=f"GitHub abgeglichen: {gesamt} Änderung(en)",
            )
    return bericht


# --- Schreibaktionen -------------------------------------------------------------------------


def pfad_erlaubt(v: RepoVerbindung, pfad: str) -> str:
    """Dokumentpfad gegen die erlaubten Muster prüfen; nur Markdown und Text."""
    pfad = (pfad or "").strip()
    teile = pfad.split("/")
    if (
        not pfad
        or pfad.startswith("/")
        or "\\" in pfad
        or ".." in teile
        or "" in teile
        or any(t.startswith(".") for t in teile)
        or any(ord(z) < 32 for z in pfad)
    ):
        raise KeinZugriff(f"Pfad „{pfad}“ ist nicht erlaubt.")
    if not pfad.lower().endswith(DOKUMENTENDUNGEN):
        raise KeinZugriff("Nur Markdown- oder Textdateien dürfen geändert werden.")
    if not any(
        fnmatch.fnmatchcase(pfad, m)
        or fnmatch.fnmatchcase(pfad, m.replace("/**", "/*/**"))
        or (m.endswith("/**") and pfad.startswith(m[:-2]))
        for m in v.pfadmuster
    ):
        raise KeinZugriff(f"Pfad „{pfad}“ liegt außerhalb der erlaubten Bereiche.")
    return pfad


def _hash(aktion_art: str, verbindung_id: int, nutzlast: dict, basis: dict) -> str:
    roh = json.dumps(
        {"art": aktion_art, "v": verbindung_id, "n": nutzlast, "b": basis},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()


def _darf_vorschlagen(akteur: Akteur, v: RepoVerbindung, art: str) -> None:
    _projekt_sichtbar(akteur, v.projekt)
    if not (akteur.ist_owner or akteur.bindung is not None):
        raise KeinZugriff("GitHub-Änderungen schlagen der Owner oder seine KI vor.")
    if not v.aktiv or not v.schreibend:
        raise KeinZugriff("Für dieses Repository ist Schreiben nicht eingerichtet.")
    if art not in v.aktionsliste:
        raise KeinZugriff(
            f"Die Aktion „{AKTIONEN.get(art, art)}“ ist für dieses Repository nicht erlaubt."
        )


def _basis_lesen(gh, v: RepoVerbindung, art: str, nutzlast: dict) -> dict:
    """Aktuellen Ausgangsstand bei GitHub lesen; er wird Teil der bestätigten Nutzlast."""
    if art == "issue_anlegen":
        if not str(nutzlast.get("titel", "")).strip():
            raise DienstFehler("Das Issue braucht einen Titel.")
        return {}
    if art in ("kommentar", "issue_aendern"):
        issue = gh.issue(v, int(nutzlast.get("nummer") or 0))
        basis = {
            "titel": issue.get("title", ""),
            "text": issue.get("body") or "",
            "zustand": issue.get("state", ""),
            "aktualisiert": issue.get("updated_at", ""),
        }
        if art == "kommentar" and not str(nutzlast.get("text", "")).strip():
            raise DienstFehler("Der Kommentar darf nicht leer sein.")
        if art == "issue_aendern":
            felder = {k: nutzlast[k] for k in ("titel", "text", "zustand") if k in nutzlast}
            if not felder:
                raise DienstFehler("Mindestens ein Feld (titel, text, zustand) ändern.")
            if felder.get("zustand") not in (None, "open", "closed"):
                raise DienstFehler("zustand ist open oder closed.")
        return basis
    if art == "dokument_pr":
        pfad = pfad_erlaubt(v, str(nutzlast.get("pfad", "")))
        if not str(nutzlast.get("inhalt", "")):
            raise DienstFehler("Der neue Dokumentinhalt fehlt.")
        sha = gh.ref(v, v.standardbranch)
        if sha is None:
            raise DienstFehler(f"Standardbranch {v.standardbranch} nicht gefunden.")
        datei = gh.datei(v, pfad, sha)
        stand, _ = gh.workflow_stand(v, sha)
        return {
            "branch": v.standardbranch,
            "commit": sha,
            "datei_sha": datei["sha"] if datei else None,
            "alter_inhalt": datei["inhalt"] if datei else None,
            "workflow_stand": stand,
        }
    raise DienstFehler("Unbekannte Aktion.")


def aktion_vorbereiten(
    akteur: Akteur, v: RepoVerbindung, art: str, nutzlast: dict
) -> ExterneAktion:
    """Entwurf mit Ausgangsstand anlegen; noch keine Wirkung bei GitHub."""
    _darf_vorschlagen(akteur, v, art)
    nutzlast = {
        k: w
        for k, w in nutzlast.items()
        if k in ("titel", "text", "nummer", "zustand", "pfad", "inhalt", "nachricht")
    }
    try:
        basis = _basis_lesen(_gh(), v, art, nutzlast)
    except github.GitHubFehler as fehler:
        raise DienstFehler(f"Ausgangsstand bei GitHub nicht lesbar: {fehler}") from fehler
    with transaction.atomic():
        a = ExterneAktion.objects.create(
            projekt=v.projekt,
            verbindung=v,
            aktions_id=uuid.uuid4().hex,
            art=art,
            nutzlast=nutzlast,
            basis=basis,
            nutzlast_hash=_hash(art, v.pk, nutzlast, basis),
            vorgeschlagen_von_name=akteur.name,
            bindung=akteur.bindung,
        )
        _ereignis(
            akteur,
            projekt=v.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis=f"{AKTIONEN[art]} vorbereitet",
            anlass=v.name,
            revision="1",
        )
    return a


def aktion_aendern(
    akteur: Akteur, a: ExterneAktion, nutzlast: dict, *, erwartete_revision: int
) -> ExterneAktion:
    """Neue Revision der Nutzlast; eine vorhandene Bestätigung erlischt."""
    _darf_vorschlagen(akteur, a.verbindung, a.art)
    try:
        basis = _basis_lesen(_gh(), a.verbindung, a.art, nutzlast)
    except github.GitHubFehler as fehler:
        raise DienstFehler(f"Ausgangsstand bei GitHub nicht lesbar: {fehler}") from fehler
    with transaction.atomic():
        geaendert = ExterneAktion.objects.filter(
            pk=a.pk,
            revision=erwartete_revision,
            zustand__in=(ExterneAktion.ENTWURF, ExterneAktion.BESTAETIGT, ExterneAktion.KONFLIKT),
            # Hat GitHub schon Schritte ausgeführt, gehört die Nutzlast dazu; dann neue Aktion.
            schritte=[],
        ).update(
            revision=erwartete_revision + 1,
            nutzlast=nutzlast,
            basis=basis,
            nutzlast_hash=_hash(a.art, a.verbindung_id, nutzlast, basis),
            zustand=ExterneAktion.ENTWURF,
            bestaetigt_von=None,
            bestaetigt_am=None,
            bestaetigter_hash="",
            bestaetigte_revision=0,
            fehler="",
        )
        if not geaendert:
            raise Konflikt(
                "Die Aktion wurde inzwischen geändert oder teilweise ausgeführt; für eine andere "
                "Nutzlast bitte eine neue Aktion vorbereiten."
            )
        _ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis="Aktion geändert, Bestätigung erloschen",
            revision=str(erwartete_revision + 1),
        )
    a.refresh_from_db()
    return a


def aktion_bestaetigen(
    akteur: Akteur, a: ExterneAktion, *, revision: int, nutzlast_hash: str
) -> None:
    """Fred bestätigt genau diese Revision und Nutzlast. Ein Chat kann nie bestätigen."""
    _owner_erforderlich(akteur, "GitHub-Aktionen bestätigen")
    if not akteur.ist_mensch:
        raise KeinZugriff("Bestätigen darf nur ein Mensch.")
    with transaction.atomic():
        geaendert = ExterneAktion.objects.filter(
            pk=a.pk,
            revision=revision,
            nutzlast_hash=nutzlast_hash,
            zustand__in=(ExterneAktion.ENTWURF, ExterneAktion.BESTAETIGT),
            verbindung__widerrufen_am=None,
        ).update(
            zustand=ExterneAktion.BESTAETIGT,
            bestaetigt_von=akteur.benutzer,
            bestaetigt_am=timezone.now(),
            bestaetigter_hash=nutzlast_hash,
            bestaetigte_revision=revision,
        )
        if not geaendert:
            raise Konflikt("Die Vorschau ist nicht mehr aktuell; bitte neu ansehen und bestätigen.")
        _ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis="Aktion bestätigt",
            revision=str(revision),
        )


def _schritt(a: ExterneAktion, schritt: str, **werte) -> None:
    a.schritte = [
        *a.schritte,
        {"schritt": schritt, **werte, "zeit": timezone.now().isoformat(timespec="seconds")},
    ]
    ExterneAktion.objects.filter(pk=a.pk).update(schritte=a.schritte)


def _erledigt(a: ExterneAktion, name: str) -> dict | None:
    return next((s for s in a.schritte if s["schritt"] == name), None)


def _ende(
    akteur: Akteur, a: ExterneAktion, zustand: str, *, fehler: str = "", url: str = ""
) -> None:
    with transaction.atomic():
        ExterneAktion.objects.filter(pk=a.pk).update(
            zustand=zustand, fehler=fehler[:2000], ergebnis_url=url[:500]
        )
        _ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis=f"Aktion {dict(ExterneAktion.ZUSTAENDE)[zustand]}",
            anlass=fehler or url,
        )
    a.refresh_from_db()


def marke(a: ExterneAktion) -> str:
    return f"kipm-aktion:{a.aktions_id}"


def aktion_ausfuehren(
    akteur: Akteur, a: ExterneAktion, *, fortsetzen: bool = False
) -> ExterneAktion:
    """Bestätigte Aktion genau einmal ausführen. Doppelklicks und Neustarts treffen auf die
    Reservierung und lösen nichts erneut aus."""
    _owner_erforderlich(akteur, "GitHub-Aktionen ausführen")
    von = ExterneAktion.TEILWEISE if fortsetzen else ExterneAktion.BESTAETIGT
    with transaction.atomic():
        aktuell = ExterneAktion.objects.select_related("verbindung", "projekt").get(pk=a.pk)
        if (
            not fortsetzen
            and aktuell.zustand == ExterneAktion.BESTAETIGT
            and (
                aktuell.bestaetigt_am is None
                or aktuell.bestaetigt_am < timezone.now() - BESTAETIGUNG_GUELTIG
            )
        ):
            ExterneAktion.objects.filter(pk=a.pk).update(
                zustand=ExterneAktion.ENTWURF, bestaetigter_hash=""
            )
            raise Konflikt("Die Bestätigung ist älter als 15 Minuten; bitte erneut bestätigen.")
        bedingung = {"pk": a.pk, "zustand": von}
        if not fortsetzen:
            bedingung["bestaetigter_hash"] = aktuell.nutzlast_hash
            bedingung["bestaetigte_revision"] = aktuell.revision
        if not ExterneAktion.objects.filter(**bedingung).update(
            zustand=ExterneAktion.IN_AUSFUEHRUNG, ausfuehrung_am=timezone.now()
        ):
            raise Konflikt(
                f"Die Aktion ist {aktuell.get_zustand_display()} und wird nicht (erneut) ausgeführt."
            )
    a = aktuell
    v = a.verbindung
    try:
        gh = _gh()
        if not v.aktiv or not v.schreibend or a.art not in v.aktionsliste:
            raise KeinZugriff("Schreiben ist für dieses Repository nicht mehr erlaubt.")
        repo = gh.repo_lesen(v)
        if repo["id"] != v.github_id or repo["name"] != v.name:
            raise Konflikt(f"Das Repository heißt jetzt {repo['name']}; Zuordnung bitte prüfen.")
        if not fortsetzen:
            neu = _basis_lesen(gh, v, a.art, a.nutzlast)
            if a.art == "dokument_pr" and neu["workflow_stand"] != v.workflow_stand:
                raise Konflikt(
                    "Die Workflows im Repository haben sich geändert; erneut bewerten und bestätigen."
                )
            if neu != a.basis:
                raise Konflikt(
                    "Der Ausgangsstand hat sich bei GitHub geändert; neue Vorschau nötig."
                )
        url = _schritte_ausfuehren(gh, v, a)
    except (Konflikt, KeinZugriff, DienstFehler) as fehler:
        _ende(
            akteur,
            a,
            ExterneAktion.KONFLIKT
            if isinstance(fehler, Konflikt)
            else ExterneAktion.FEHLGESCHLAGEN,
            fehler=str(fehler),
        )
        return a
    except github.GitHubFehler as fehler:
        if fehler.unklar:
            _ende(
                akteur,
                a,
                ExterneAktion.UNKLAR,
                fehler=f"{fehler}; Ausgang unbekannt, bitte abgleichen",
            )
        elif a.schritte:
            _ende(akteur, a, ExterneAktion.TEILWEISE, fehler=f"Teilergebnis: {fehler}")
        else:
            _ende(akteur, a, ExterneAktion.FEHLGESCHLAGEN, fehler=str(fehler))
        return a
    _ende(akteur, a, ExterneAktion.ERFOLGREICH, url=url)
    return a


def _schritte_ausfuehren(gh, v: RepoVerbindung, a: ExterneAktion) -> str:
    n = a.nutzlast
    kennzeichen = f"\n\n<!-- {marke(a)} -->"
    if a.art == "issue_anlegen":
        issue = gh.issue_anlegen(v, n["titel"], (n.get("text") or "") + kennzeichen)
        _schritt(a, "issue", nummer=issue["number"], id=issue["id"], url=issue["html_url"])
        return issue["html_url"]
    if a.art == "kommentar":
        k = gh.kommentar(v, int(n["nummer"]), n["text"] + kennzeichen)
        _schritt(a, "kommentar", id=k["id"], url=k["html_url"])
        return k["html_url"]
    if a.art == "issue_aendern":
        felder = {}
        if "titel" in n:
            felder["title"] = n["titel"]
        if "text" in n:
            felder["body"] = n["text"]
        if "zustand" in n:
            felder["state"] = n["zustand"]
        issue = gh.issue_aendern(v, int(n["nummer"]), felder)
        _schritt(a, "issue_geaendert", nummer=issue["number"], url=issue["html_url"])
        return issue["html_url"]
    branch = f"kipm/{a.aktions_id[:12]}"
    if not _erledigt(a, "branch"):
        gh.branch_anlegen(v, branch, a.basis["commit"])
        _schritt(a, "branch", name=branch, von=a.basis["commit"])
    if not _erledigt(a, "datei"):
        commit = gh.datei_schreiben(
            v,
            n["pfad"],
            n["inhalt"],
            branch,
            (n.get("nachricht") or f"Dokument {n['pfad']} aktualisiert") + f" [{marke(a)}]",
            sha=a.basis.get("datei_sha"),
        )
        _schritt(a, "datei", commit=commit)
    if not _erledigt(a, "pr"):
        pr = gh.pr_anlegen(
            v,
            n.get("titel") or f"Dokument {n['pfad']}",
            branch,
            a.basis["branch"],
            (n.get("text") or "Vorgeschlagen über Server-KI-PM.") + kennzeichen,
        )
        _schritt(a, "pr", nummer=pr["number"], url=pr["html_url"])
    return _erledigt(a, "pr")["url"]


def aktion_abgleichen(akteur: Akteur, a: ExterneAktion) -> ExterneAktion:
    """Ungeklärten Ausgang anhand der Aktionsmarke bei GitHub prüfen; nie erneut senden."""
    _owner_erforderlich(akteur, "GitHub-Aktionen abgleichen")
    a = ExterneAktion.objects.select_related("verbindung", "projekt").get(pk=a.pk)
    if a.zustand not in (ExterneAktion.UNKLAR, ExterneAktion.IN_AUSFUEHRUNG):
        raise Konflikt("Nur ungeklärte Aktionen werden abgeglichen.")
    if (
        a.zustand == ExterneAktion.IN_AUSFUEHRUNG
        and a.ausfuehrung_am
        and a.ausfuehrung_am > timezone.now() - timedelta(minutes=10)
    ):
        raise Konflikt("Die Aktion läuft gerade; bitte später abgleichen.")
    gh = _gh()
    v = a.verbindung
    try:
        if a.art == "issue_anlegen":
            treffer = gh.issues_mit_marke(v, marke(a))
            if len(treffer) == 1:
                _schritt(
                    a,
                    "issue",
                    nummer=treffer[0]["number"],
                    id=treffer[0]["id"],
                    url=treffer[0]["html_url"],
                )
                _ende(akteur, a, ExterneAktion.ERFOLGREICH, url=treffer[0]["html_url"])
            else:
                _ende(
                    akteur,
                    a,
                    ExterneAktion.UNKLAR,
                    fehler=f"{len(treffer)} Treffer zur Aktionsmarke; bitte bei GitHub nachsehen",
                )
        elif a.art == "kommentar":
            treffer = [
                k
                for k in gh.kommentare(v, int(a.nutzlast["nummer"]))
                if marke(a) in (k.get("body") or "")
            ]
            if len(treffer) == 1:
                _ende(akteur, a, ExterneAktion.ERFOLGREICH, url=treffer[0]["html_url"])
            else:
                _ende(
                    akteur,
                    a,
                    ExterneAktion.UNKLAR,
                    fehler=f"{len(treffer)} Kommentare mit Aktionsmarke",
                )
        elif a.art == "issue_aendern":
            issue = gh.issue(v, int(a.nutzlast["nummer"]))
            ziel = {
                "titel": issue.get("title"),
                "text": issue.get("body") or "",
                "zustand": issue.get("state"),
            }
            if all(
                ziel[k] == a.nutzlast[k] for k in ("titel", "text", "zustand") if k in a.nutzlast
            ):
                _ende(akteur, a, ExterneAktion.ERFOLGREICH, url=issue.get("html_url", ""))
            else:
                _ende(
                    akteur,
                    a,
                    ExterneAktion.UNKLAR,
                    fehler="Issue weicht vom Ziel ab; Änderung nicht erkennbar",
                )
        else:
            branch = f"kipm/{a.aktions_id[:12]}"
            if gh.ref(v, branch) and not _erledigt(a, "branch"):
                _schritt(a, "branch", name=branch, von=a.basis["commit"])
            pr = gh.pr_fuer_branch(v, branch)
            if pr:
                if not _erledigt(a, "datei"):
                    _schritt(a, "datei", commit="(über Branch bestätigt)")
                _schritt(a, "pr", nummer=pr["number"], url=pr["html_url"])
                _ende(akteur, a, ExterneAktion.ERFOLGREICH, url=pr["html_url"])
            elif _erledigt(a, "branch"):
                _ende(
                    akteur,
                    a,
                    ExterneAktion.TEILWEISE,
                    fehler="Branch vorhanden, Pull Request fehlt; fortsetzen möglich",
                )
            else:
                _ende(
                    akteur,
                    a,
                    ExterneAktion.UNKLAR,
                    fehler="Kein Branch zur Aktion gefunden; bitte prüfen",
                )
    except github.GitHubFehler as fehler:
        raise DienstFehler(f"Abgleich nicht möglich: {fehler}") from fehler
    return a


def aktion_nicht_ausgefuehrt(akteur: Akteur, a: ExterneAktion, grund: str) -> None:
    """Menschliche Klärung: bei GitHub ist nichts entstanden; neuer Versuch nur nach neuer
    Bestätigung."""
    _owner_erforderlich(akteur, "Ungeklärte Aktionen klären")
    if not grund.strip():
        raise DienstFehler("Bitte kurz begründen, wie geprüft wurde.")
    with transaction.atomic():
        if not ExterneAktion.objects.filter(
            pk=a.pk, zustand=ExterneAktion.UNKLAR, schritte=[]
        ).update(
            zustand=ExterneAktion.ENTWURF, bestaetigter_hash="", bestaetigte_revision=0, fehler=""
        ):
            raise Konflikt(
                "Nur ungeklärte Aktionen ohne bekannten Schritt lassen sich zurücksetzen."
            )
        _ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis="Als nicht ausgeführt geklärt",
            anlass=grund.strip(),
        )


def aktion_verwerfen(akteur: Akteur, a: ExterneAktion) -> None:
    _owner_erforderlich(akteur, "GitHub-Aktionen verwerfen")
    with transaction.atomic():
        if not ExterneAktion.objects.filter(
            pk=a.pk,
            zustand__in=(ExterneAktion.ENTWURF, ExterneAktion.BESTAETIGT, ExterneAktion.KONFLIKT),
        ).update(zustand=ExterneAktion.VERWORFEN):
            raise Konflikt("Diese Aktion lässt sich nicht mehr verwerfen.")
        _ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="GitHub",
            objekt_kennung=f"Aktion {a.aktions_id[:8]}",
            ereignis="Aktion verworfen",
        )

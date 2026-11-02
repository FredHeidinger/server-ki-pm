"""Öffentlichen Spiegel FredHeidinger/server-ki-pm aus dem Hauptrepository erzeugen.

Übernimmt aus einem Commit des Hauptrepositorys nur ``02-Anwendung`` und ``03-Server/skripte``,
dazu README und .gitignore des Spiegels aus ``03-Server/spiegel``. Planungsdokumente bleiben
privat. Bricht ab, wenn Heimnetzadressen außer der dokumentierten Serveradresse, Maschinen-
kennungen, Schlüssel oder Laufzeitdaten enthalten wären.

    uv run --no-project python 03-Server/skripte/spiegel.py           # prüfen, Änderungen zeigen
    uv run --no-project python 03-Server/skripte/spiegel.py --push    # committen und pushen

Nur Standardbibliothek; braucht git und für ``--push`` Schreibrecht auf den Spiegel.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SPIEGEL_URL = "https://github.com/FredHeidinger/server-ki-pm.git"
SPIEGEL_ROH = "https://raw.githubusercontent.com/FredHeidinger/server-ki-pm"
BEREICHE = ("02-Anwendung", "03-Server/skripte")
# Quelle im Hauptrepository -> Pfad im Spiegel
ZUSATZ = {"03-Server/spiegel/README.md": "README.md", "03-Server/spiegel/gitignore": ".gitignore"}
# Die Serveradresse steht in der Kommandozeile; jede andere Heimnetzadresse wäre ein Leck
ERLAUBTE_ADRESSEN = {"192.168.254.157"}

_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])")
_GEHEIM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bkipm_[A-Za-z0-9]{6,}_[A-Za-z0-9_-]{20,}"
)
# RFC 1918 und CGNAT (RFC 6598, z. B. Tailscale); als Zahlen, sonst meldet die Prüfung sich selbst
_HEIMNETZE = [
    ipaddress.ip_network((ipaddress.IPv4Address(bytes(start)), praefix))
    for start, praefix in (
        ((10, 0, 0, 0), 8),
        ((172, 16, 0, 0), 12),
        ((192, 168, 0, 0), 16),
        ((100, 64, 0, 0), 10),
    )
]
_VERBOTENE_PFADE = re.compile(
    r"(^|/)daten/|\.sqlite3|(^|/)\.secret_key$|(^|/)\.kipm(/|$)|\.pem$|\.key$|(^|/)\.env$"
)


def git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def dateien_lesen(haupt: Path, commit: str) -> dict[str, tuple[str, bytes]]:
    """Pfad im Spiegel -> (Dateimodus, Inhalt) aus dem Commit, unabhängig von der Arbeitskopie."""
    eintraege = git(
        "ls-tree", "-r", "-z", "--full-tree", commit, "--", *BEREICHE, *ZUSATZ, cwd=haupt
    )
    dateien: dict[str, tuple[str, bytes]] = {}
    for eintrag in eintraege.decode("utf-8").split("\0"):
        if not eintrag:
            continue
        kopf, pfad = eintrag.split("\t", 1)
        modus, art, _ = kopf.split()
        if art != "blob":
            continue
        dateien[ZUSATZ.get(pfad, pfad)] = (modus, git("show", f"{commit}:{pfad}", cwd=haupt))
    fehlend = [z for z in ZUSATZ.values() if z not in dateien]
    if fehlend:
        raise SystemExit(f"Im Commit fehlt die Spiegelvorlage für: {', '.join(fehlend)}")
    return dateien


def pruefen(dateien: dict[str, tuple[str, bytes]]) -> list[str]:
    """Befunde, die eine Veröffentlichung verhindern; leere Liste heißt veröffentlichbar."""
    befunde = []
    for pfad, (_, inhalt) in sorted(dateien.items()):
        if _VERBOTENE_PFADE.search(pfad):
            befunde.append(f"{pfad}: Laufzeitdaten oder Schlüsseldatei")
            continue
        text = inhalt.decode("utf-8", errors="replace")
        for nr, zeile in enumerate(text.splitlines(), 1):
            for treffer in _IPV4.findall(zeile):
                try:
                    adresse = ipaddress.ip_address(treffer)
                except ValueError:
                    continue
                if treffer not in ERLAUBTE_ADRESSEN and any(adresse in n for n in _HEIMNETZE):
                    befunde.append(f"{pfad}:{nr}: Heimnetzadresse {treffer}")
            if _MAC.search(zeile):
                befunde.append(f"{pfad}:{nr}: Maschinenkennung (MAC-Adresse)")
            if _GEHEIM.search(zeile):
                befunde.append(f"{pfad}:{nr}: Schlüssel oder Token")
    return befunde


def spiegel_schreiben(ziel: Path, dateien: dict[str, tuple[str, bytes]]) -> None:
    """Arbeitskopie des Spiegels auf genau diese Dateien setzen und vormerken."""
    for alt in git("ls-files", "-z", cwd=ziel).decode("utf-8").split("\0"):
        if alt and alt not in dateien:
            (ziel / alt).unlink()
    for pfad, (_, inhalt) in dateien.items():
        datei = ziel / pfad
        datei.parent.mkdir(parents=True, exist_ok=True)
        datei.write_bytes(inhalt)
    git("add", "-A", cwd=ziel)
    for pfad, (modus, _) in dateien.items():
        git("update-index", "--chmod=+x" if modus == "100755" else "--chmod=-x", pfad, cwd=ziel)


def spiegel_vorbereiten(ziel: Path, url: str) -> None:
    """Zwischenkopie des Spiegels anlegen oder auf den Stand des Servers zurücksetzen."""
    if not (ziel / ".git").exists():
        ziel.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "-q", url, str(ziel), cwd=ziel.parent)
    git("config", "core.autocrlf", "false", cwd=ziel)
    git("fetch", "-q", "origin", cwd=ziel)
    git("checkout", "-q", "-B", "main", "origin/main", cwd=ziel)
    git("reset", "-q", "--hard", "origin/main", cwd=ziel)
    git("clean", "-q", "-fdx", cwd=ziel)


def main(argv: list[str] | None = None) -> int:
    teil = argparse.ArgumentParser(
        description="Öffentlichen Spiegel aus dem Hauptrepository erzeugen."
    )
    teil.add_argument(
        "--commit", default="HEAD", help="Commit des Hauptrepositorys (Standard HEAD)"
    )
    teil.add_argument("--ziel", type=Path, default=Path(tempfile.gettempdir()) / "kipm-spiegel")
    teil.add_argument("--url", default=SPIEGEL_URL)
    teil.add_argument("--push", action="store_true", help="Änderungen committen und pushen")
    teil.add_argument("--haupt", type=Path, default=Path(__file__).parent, help=argparse.SUPPRESS)
    args = teil.parse_args(argv)

    haupt = Path(git("rev-parse", "--show-toplevel", cwd=args.haupt).decode().strip())
    commit = git("rev-parse", "--short", args.commit, cwd=haupt).decode().strip()
    betreff = git("log", "-1", "--format=%s", commit, cwd=haupt).decode("utf-8").strip()
    offen = git("status", "--porcelain", "--", *BEREICHE, "03-Server/spiegel", cwd=haupt)
    if offen:
        print("Nicht committete Änderungen werden nicht gespiegelt:")
        print(offen.decode("utf-8"), end="")
        if args.push:
            print("Abbruch: erst im Hauptrepository committen.")
            return 1

    dateien = dateien_lesen(haupt, commit)
    befunde = pruefen(dateien)
    if befunde:
        print("Abbruch, nicht veröffentlichbar:")
        print("\n".join(f"  {b}" for b in befunde))
        return 1
    print(
        f"{len(dateien)} Dateien aus {commit} geprüft: keine Heimnetzadressen, Kennungen, Schlüssel"
    )

    spiegel_vorbereiten(args.ziel, args.url)
    spiegel_schreiben(args.ziel, dateien)
    aenderungen = git("status", "--short", cwd=args.ziel).decode("utf-8")
    if not aenderungen:
        stand = git("rev-parse", "--short", "HEAD", cwd=args.ziel).decode().strip()
        print(f"Spiegel ist bereits aktuell (Spiegel-Commit {stand}).")
    else:
        print(aenderungen, end="")
        if not args.push:
            print("Nur geprüft. Mit --push committen und veröffentlichen.")
            return 0
        nachricht = f"{betreff}\n\nAus dem Hauptrepository, Commit {commit}."
        git("commit", "-q", "-m", nachricht, cwd=args.ziel)
        git("push", "-q", "origin", "main", cwd=args.ziel)
        stand = git("rev-parse", "--short", "HEAD", cwd=args.ziel).decode().strip()
        print(f"Spiegel-Commit {stand} gepusht.")
    print("Aktualisierungszeile für die Proxmox-Shell:")
    print(
        f'KIPM_CT=100 sudo -E bash -c "$(curl -fsSL {SPIEGEL_ROH}/{stand}'
        '/03-Server/skripte/proxmox-installieren.sh)"'
    )
    return 0


if __name__ == "__main__":
    getattr(sys.stdout, "reconfigure", lambda **_: None)(encoding="utf-8")
    sys.exit(main())

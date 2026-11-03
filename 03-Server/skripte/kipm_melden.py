"""Meldet einen Arbeitsstand an Server-KI-PM (Meldevertrag ``kipm-stand/1``).

Für lokale Abläufe und CI, nur Python-Standardbibliothek. Der Server liest keine Ordner; gemeldet
wird ausdrücklich mit einem widerrufbaren Projekt- oder PC-Schlüssel.

    KIPM_URL=https://<server> KIPM_SCHLUESSEL=kipm_… python kipm_melden.py senden bericht.json
    python kipm_melden.py nachliefern

Bericht (JSON): ``meldung_id`` (stabil je Meldung, z. B. Commit + Lauf), ``version`` (Commit oder
Hash), ``quelle``, optional ``projekt``, ``modul``, ``komponente``, ``auftrag``, ``revision``,
``arbeitsort``, ``umsetzung`` {umfang}, ``tests`` [{umfang, ergebnis, umgebung}], ``installation``
{umgebung, komponente, version, installiert_am}, ``einschraenkungen``, ``beobachtet_am`` (ISO),
``korrigiert`` (meldung_id einer früheren Meldung).

Die ``meldung_id`` dient als Wiederholungsschutz: Eine Wiederholung nach Zeitüberschreitung oder
aus der Warteschlange wirkt nur einmal; ältere Meldungen verdrängen keinen neueren Stand.
Ist der Server nicht erreichbar, landet die Meldung in der Warteschlange (Schlüssel wird nie
gespeichert). Eine abgewiesene Meldung (z. B. Umgebung noch nicht übernommen) bleibt unter ihrer
``meldung_id`` abgewiesen; nach Behebung mit neuer ``meldung_id`` senden. Fehlt
``beobachtet_am``, gilt die Zeit des ersten Sendeversuchs (Kopfzeile ``X-KIPM-Beobachtet``,
nicht Teil des Inhalts; der Server begrenzt sie auf seine eigene Uhrzeit).
Rückgabe: 0 gespeichert, 2 abgewiesen, 3 in Warteschlange.
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

PFAD = "/api/stand/melden/"


def _anfrage(url: str, schluessel: str, bericht: dict, zeit: float, ca: str | None, erster: str):
    rumpf = {k: v for k, v in bericht.items() if k != "projekt"}
    rumpf["anfrage_kennung"] = bericht["meldung_id"]
    kopf = {
        "Authorization": f"Bearer {schluessel}",
        "Content-Type": "application/json",
        # Zeit des ersten Versuchs: gilt, wenn der Bericht keine Beobachtungszeit nennt, und
        # steht außerhalb des Rumpfs, damit eine Wiederholung denselben Inhalt hat.
        "X-KIPM-Beobachtet": erster,
    }
    if bericht.get("projekt"):
        kopf["X-KIPM-Projekt"] = str(bericht["projekt"])
    req = urllib.request.Request(
        url.rstrip("/") + PFAD,
        data=json.dumps(rumpf).encode("utf-8"),
        headers=kopf,
        method="POST",
    )
    kontext = ssl.create_default_context(cafile=ca) if ca else None
    try:
        with urllib.request.urlopen(req, timeout=zeit, context=kontext) as antwort:
            return antwort.status, json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as fehler:
        try:
            daten = json.loads(fehler.read().decode("utf-8"))
        except ValueError:
            daten = {"ok": False, "fehler": f"HTTP {fehler.code}"}
        return fehler.code, daten


def senden(bericht: dict, *, url: str, schluessel: str, zeit: float, versuche: int, ca, erster):
    """(Ergebnis, Antwort): „gespeichert“, „abgewiesen“ oder „offen“ (Netz/Server)."""
    daten: dict = {"ok": False, "fehler": "kein Versuch"}
    for nr in range(max(1, versuche)):
        try:
            status, daten = _anfrage(url, schluessel, bericht, zeit, ca, erster)
        except (urllib.error.URLError, TimeoutError, OSError) as fehler:
            status, daten = 0, {"ok": False, "fehler": f"nicht erreichbar: {fehler}"}
        if status in (200, 201):
            return "gespeichert", daten
        if status in (400, 401, 403, 404) or (status == 409 and "verarbeitet" not in str(daten)):
            return "abgewiesen", daten
        if nr + 1 < versuche:
            time.sleep(min(2**nr, 10))
    return "offen", daten


def _bericht_pruefen(bericht: dict, herkunft: Path) -> dict:
    for feld in ("meldung_id", "version", "quelle"):
        if not str(bericht.get(feld, "")).strip():
            raise SystemExit(f"{herkunft}: Feld „{feld}“ fehlt.")
    return bericht


def _dateiname(meldung_id) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(meldung_id))[:80]


def _schlange_lesen(pfad: Path) -> tuple[dict, str]:
    """Eintrag der Warteschlange: unveränderter Bericht und Zeit des ersten Versuchs."""
    eintrag = json.loads(pfad.read_text(encoding="utf-8"))
    if "bericht" in eintrag:
        return _bericht_pruefen(eintrag["bericht"], pfad), eintrag.get("erster_versuch", "")
    return _bericht_pruefen(eintrag, pfad), ""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Arbeitsstand an Server-KI-PM melden")
    p.add_argument("befehl", choices=["senden", "nachliefern"])
    p.add_argument("bericht", nargs="?", help="JSON-Datei mit der Meldung (für senden)")
    p.add_argument("--warteschlange", default=".kipm-meldungen")
    p.add_argument("--zeit", type=float, default=20.0, help="Zeitgrenze je Versuch in Sekunden")
    p.add_argument("--versuche", type=int, default=3)
    a = p.parse_args(argv)
    url = os.environ.get("KIPM_URL", "").strip()
    schluessel = os.environ.get("KIPM_SCHLUESSEL", "").strip()
    if not url or not schluessel:
        print("KIPM_URL und KIPM_SCHLUESSEL setzen.", file=sys.stderr)
        return 2
    ca = os.environ.get("KIPM_CA") or None
    schlange = Path(a.warteschlange)
    jetzt = datetime.now(UTC).isoformat(timespec="seconds")
    if a.befehl == "senden":
        if not a.bericht:
            p.error("senden braucht eine Berichtsdatei")
        pfad = Path(a.bericht)
        bericht = _bericht_pruefen(json.loads(pfad.read_text(encoding="utf-8")), pfad)
        # Liegt dieselbe Meldung schon in der Warteschlange, gilt deren erster Versuch weiter.
        frueher = schlange / f"{_dateiname(bericht['meldung_id'])}.json"
        erster = ""
        if frueher.exists():
            try:
                erster = str(
                    json.loads(frueher.read_text(encoding="utf-8")).get("erster_versuch", "")
                )
            except (ValueError, AttributeError):
                erster = ""
        berichte = [(None, bericht, erster or jetzt)]
    else:
        berichte = [(f, *_schlange_lesen(f)) for f in sorted(schlange.glob("*.json"))]
    code = 0
    for datei, bericht, erster in berichte:
        erster = erster or jetzt
        ergebnis, daten = senden(
            bericht,
            url=url,
            schluessel=schluessel,
            zeit=a.zeit,
            versuche=a.versuche,
            ca=ca,
            erster=erster,
        )
        print(
            json.dumps(
                {"meldung_id": bericht["meldung_id"], "ergebnis": ergebnis, **daten},
                ensure_ascii=False,
            )
        )
        if ergebnis == "gespeichert":
            if datei is not None:
                datei.unlink()
        elif ergebnis == "offen":
            schlange.mkdir(parents=True, exist_ok=True)
            ziel = schlange / f"{_dateiname(bericht['meldung_id'])}.json"
            eintrag = {"bericht": bericht, "erster_versuch": erster}
            ziel.write_text(json.dumps(eintrag, ensure_ascii=False, indent=2), encoding="utf-8")
            code = max(code, 3)
        else:
            if datei is not None:
                (schlange / "abgewiesen").mkdir(parents=True, exist_ok=True)
                datei.replace(schlange / "abgewiesen" / datei.name)
            code = 2
    return code


if __name__ == "__main__":
    sys.exit(main())

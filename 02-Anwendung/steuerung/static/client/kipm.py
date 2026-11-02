#!/usr/bin/env python3
"""kipm – Kommandozeile für Projektchats. Spricht nur mit der HTTP-Schnittstelle.

Einrichtung: Datei .kipm im Projektordner (Chat-Schlüssel je Projekt) oder
%USERPROFILE%\.kipm\konfig.ini (PC-Zugang, legt die Einrichtungsdatei aus Server-KI-PM an) mit
  [kipm]
  url = https://<server>/
  schluessel = kipm_...          Zugangsschlüssel der Chat-Bindung, nie in den Chat kopieren
  ca = kipm-root.crt             optional: Zertifikat der internen CA (vom Server: /static/kipm-root.crt)
Umgebungsvariablen KIPM_URL, KIPM_SCHLUESSEL, KIPM_CA_DATEI gehen vor; KIPM_TLS_PRUEFEN=0 schaltet
die Zertifikatsprüfung ab (nur im Heimnetz vertretbar).

Befehle:
  projekt                                Projekt mit Modulen und Arbeitsorten
  auftraege [--alle]                     alle offenen Aufträge des Projekts (mit --alle auch abgeschlossene)
  meine-auftraege                        nur übernehmbare und eigene Aufträge
  auftrag-anlegen --titel "…" --text "…" [--modul kennung] [--prio hoch|mittel|niedrig] [--erzwingen]
                                         Wunsch als Entwurf anlegen; gleicher Titel wie ein offener Auftrag wird abgewiesen
  lesen A-12                             vollständige gebundene Revision
  uebernehmen A-12 --revision 3          exklusive Übernahme
  fortschritt A-12 --revision 3 "Text"   Fortschritt melden
  hindernis A-12 --revision 3 "Text"     Hindernis melden (Auftrag wird BLOCKIERT)
  rueckfrage A-12 --revision 3 "Text"    Rückfrage an den Menschen (blockiert, Antwort steht in "lesen")
  fehler A-12 --revision 3 "Text"        Fehler melden (blockiert)
  vorschlag A-12 --revision 3 "Text"     Änderungsvorschlag, wird nicht wirksam
  pruefbericht A-12 --revision 3 "Text"  Prüfbericht zu fremdem Auftrag
  ergebnis A-12 --revision 3 --zusammenfassung "…" --datei "pfad@commit" --pruefung "befehl: ergebnis" [--luecken "…"]
  entscheidungen                         Entscheidungen des eigenen Projekts
  bestaetigen E-2                        Übernahme einer Entscheidung für das eigene Projekt
  entscheidung-vorschlagen --titel "…" --text "…" [--verweis "…"]

Jede verändernde Anfrage bekommt eine zufällige Anfrage-Kennung; mit --kennung (nach dem
Befehl) lässt sich eine Anfrage gefahrlos wiederholen (gleicher Inhalt = keine zweite Wirkung).
--json gibt die Rohantwort aus.
Nur die Standardbibliothek; keine Abhängigkeiten.
"""

import argparse
import configparser
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

for _strom in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_strom, "reconfigure", None)
    if _reconfigure is not None:
        _reconfigure(encoding="utf-8")


def _konfig() -> dict:
    """Einstellungen aus Datei .kipm (im Arbeitsordner, sonst im Benutzerordner); Umgebung geht vor.

    Beispiel .kipm:
        [kipm]
        url = https://192.168.254.157/
        schluessel = kipm_...
        ca = kipm-root.crt
    """
    werte: dict = {}
    for pfad in (Path.cwd() / ".kipm", Path.home() / ".kipm" / "konfig.ini", Path.home() / ".kipm"):
        if pfad.is_file():
            cp = configparser.ConfigParser()
            cp.read(pfad, encoding="utf-8")
            if cp.has_section("kipm"):
                werte = {k: v for k, v in cp["kipm"].items()}
                werte["_ordner"] = str(pfad.parent)
            break
    for schluessel, env in (
        ("url", "KIPM_URL"),
        ("schluessel", "KIPM_SCHLUESSEL"),
        ("ca", "KIPM_CA_DATEI"),
    ):
        if os.environ.get(env):
            werte[schluessel] = os.environ[env]
    return werte


KONFIG = _konfig()


def _basis() -> str:
    url = (KONFIG.get("url") or "").rstrip("/")
    if not url:
        sys.exit("Adresse fehlt: KIPM_URL setzen oder in .kipm unter [kipm] url eintragen.")
    return url


def _schluessel() -> str:
    schluessel = (KONFIG.get("schluessel") or "").strip()
    if not schluessel:
        sys.exit(
            "Zugangsschlüssel fehlt: KIPM_SCHLUESSEL setzen oder in .kipm unter [kipm] schluessel eintragen."
        )
    return schluessel


def _tls_kontext():
    """Vertrauen in das Serverzertifikat: eigene CA-Datei, oder Prüfung aus (nur im Heimnetz)."""
    if os.environ.get("KIPM_TLS_PRUEFEN") == "0":
        return ssl._create_unverified_context()
    ca = KONFIG.get("ca")
    if ca:
        ca_pfad = Path(ca)
        if not ca_pfad.is_absolute() and KONFIG.get("_ordner"):
            ca_pfad = Path(KONFIG["_ordner"]) / ca_pfad
        return ssl.create_default_context(cafile=str(ca_pfad))
    return ssl.create_default_context()


def _nummer(text: str) -> int:
    text = text.strip().upper()
    for praefix in ("A-", "E-"):
        if text.startswith(praefix):
            text = text[2:]
    if not text.isdigit():
        sys.exit(f"Ungültige Nummer: {text}")
    return int(text)


def anfrage(pfad: str, daten: dict | None = None) -> tuple[int, dict]:
    url = f"{_basis()}/api/{pfad.lstrip('/')}"
    koerper = None
    kopf = {
        "Authorization": f"Bearer {_schluessel()}",
        "Accept": "application/json",
        "X-KIPM-Ordner": os.environ.get("KIPM_ORDNER") or os.getcwd(),
    }
    if daten is not None:
        koerper = json.dumps(daten).encode("utf-8")
        kopf["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=koerper, headers=kopf, method="POST" if daten is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=_tls_kontext()) as antwort:
            return antwort.status, json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as fehler:
        try:
            return fehler.code, json.loads(fehler.read().decode("utf-8"))
        except ValueError:
            return fehler.code, {"ok": False, "fehler": f"HTTP {fehler.code}"}
    except urllib.error.URLError as fehler:
        sys.exit(f"Dienst nicht erreichbar: {fehler.reason}")


def _ausgeben(status: int, antwort: dict, roh: bool) -> int:
    if roh:
        print(json.dumps(antwort, ensure_ascii=False, indent=2))
        return 0 if antwort.get("ok") else 1
    if not antwort.get("ok"):
        print(f"Abgelehnt ({status}): {antwort.get('fehler', 'unbekannt')}")
        return 1
    if antwort.get("wiederholt"):
        print("Hinweis: Anfrage war bereits ausgeführt; gespeicherte Antwort.")
    if "projekt" in antwort and isinstance(antwort["projekt"], dict):
        p = antwort["projekt"]
        print(f"Projekt {p['name']} ({p['kennung']}) · Bindung {antwort['bindung']}")
        print(
            "Module: "
            + (", ".join(f"{m['name']} [{m['kennung']}]" for m in p["module"]) or "keine")
        )
        print(
            "Arbeitsorte: "
            + (", ".join(f"{o['name']} [{o['kennung']}]" for o in p["arbeitsorte"]) or "keine")
        )
    elif "auftraege" in antwort:
        print(f"Projekt {antwort['projekt']} · Bindung {antwort['bindung']}")
        if not antwort["auftraege"]:
            print("Keine Aufträge.")
        for a in antwort["auftraege"]:
            art = "übernehmbar" if a["uebernehmbar"] else f"Bearbeiter {a['bearbeiter']}"
            print(
                f"  {a['nummer']}  Rev {a['revision']}  {a['zustand']:<22} {a['prioritaet']:<7} {a['titel']}  [{art}]"
            )
    elif "auftrag" in antwort:
        a = antwort["auftrag"]
        print(
            f"{a['nummer']}  Rev {a['revision']}  {a['zustand']}  Projekt {a['projekt']}  Modul {a.get('modul') or '–'}"
        )
        print(f"Titel: {a['titel']}")
        if "ziel" in a:
            print(f"Wunsch: {a['beschreibung'] or '–'}")
            print(f"Ziel: {a['ziel'] or '–'}")
            print(f"Nicht-Ziele: {a['nicht_ziele'] or '–'}")
            ort = a.get("arbeitsort")
            print(
                f"Arbeitsort: {ort['name'] + ' (' + ort['rechner'] + ' ' + ort['pfad'] + ')' if ort else '–'}"
            )
            print("Schreibumfang: " + (", ".join(a["schreibumfang"]) or "–"))
            print(f"Quellen: {a['quellen'] or '–'}")
            print("Abnahmekriterien:")
            for i, k in enumerate(a["abnahmekriterien"], 1):
                print(f"  {i}. {k}")
            if a["vorgaenger"]:
                print(
                    "Vorgänger: "
                    + ", ".join(f"{v['nummer']} ({v['zustand']})" for v in a["vorgaenger"])
                )
            print(f"Prüfer: {a.get('pruefer') or '–'}   Bearbeiter: {a.get('bearbeiter') or '–'}")
            for m in a.get("meldungen", [])[:8]:
                print(f"  [{m['art']}] {m['zeit']} {m['von']}: {m['text']}")
            lp = a.get("letzte_pruefung")
            if lp and lp.get("entscheidung"):
                print(f"Letzte Prüfung: {lp['entscheidung']} – {lp.get('begruendung') or ''}")
                for k in lp.get("kriterien", []):
                    print(
                        f"  [{k['bewertung']}] {k['kriterium']} {('– ' + k['anmerkung']) if k['anmerkung'] else ''}"
                    )
    elif "entscheidungen" in antwort:
        if not antwort["entscheidungen"]:
            print("Keine Entscheidungen für dieses Projekt.")
        for e in antwort["entscheidungen"]:
            best = ", ".join(f"{b['projekt']}@{b['stand']}" for b in e["bestaetigt"]) or "keine"
            print(
                f"  {e['nummer']}  {e['zustand']:<12} Stand {e['stand']}  {e['titel']}  betroffen: {', '.join(e['betroffen'])}  bestätigt: {best}"
            )
    elif "entscheidung" in antwort:
        e = antwort["entscheidung"]
        print(f"{e['nummer']}  {e['zustand']}  Stand {e['stand']}  {e['titel']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    gemeinsam = argparse.ArgumentParser(add_help=False)
    gemeinsam.add_argument("--json", action="store_true", help="Rohantwort als JSON ausgeben")
    gemeinsam.add_argument("--kennung", help="Anfrage-Kennung zum gefahrlosen Wiederholen")
    p = argparse.ArgumentParser(prog="kipm", description="Chat-Zugang zu Server-KI-PM")
    unter = p.add_subparsers(dest="befehl", required=True)

    unter.add_parser("projekt", parents=[gemeinsam])
    unter.add_parser("meine-auftraege", parents=[gemeinsam])
    s = unter.add_parser("auftraege", parents=[gemeinsam])
    s.add_argument("--alle", action="store_true")
    s = unter.add_parser("auftrag-anlegen", parents=[gemeinsam])
    s.add_argument("--titel", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--modul", default="")
    s.add_argument("--prio", default="mittel", choices=["hoch", "mittel", "niedrig"])
    s.add_argument("--erzwingen", action="store_true")
    s = unter.add_parser("lesen", parents=[gemeinsam])
    s.add_argument("auftrag")
    for name in (
        "uebernehmen",
        "fortschritt",
        "hindernis",
        "rueckfrage",
        "fehler",
        "vorschlag",
        "pruefbericht",
    ):
        s = unter.add_parser(name, parents=[gemeinsam])
        s.add_argument("auftrag")
        s.add_argument("--revision", type=int, required=True)
        if name != "uebernehmen":
            s.add_argument("text")
    s = unter.add_parser("ergebnis", parents=[gemeinsam])
    s.add_argument("auftrag")
    s.add_argument("--revision", type=int, required=True)
    s.add_argument("--zusammenfassung", required=True)
    s.add_argument("--datei", action="append", default=[], help="pfad@revision, mehrfach")
    s.add_argument("--pruefung", action="append", default=[], help="befehl: ergebnis, mehrfach")
    s.add_argument("--luecken", default="")
    unter.add_parser("entscheidungen", parents=[gemeinsam])
    s = unter.add_parser("bestaetigen", parents=[gemeinsam])
    s.add_argument("entscheidung")
    s = unter.add_parser("entscheidung-vorschlagen", parents=[gemeinsam])
    s.add_argument("--titel", required=True)
    s.add_argument("--text", required=True)
    s.add_argument("--verweis", default="")

    a = p.parse_args(argv)
    kennung = a.kennung or uuid.uuid4().hex

    if a.befehl == "projekt":
        status, antwort = anfrage("projekt/")
    elif a.befehl == "meine-auftraege":
        status, antwort = anfrage("auftraege/?nur_meine=1")
    elif a.befehl == "auftraege":
        status, antwort = anfrage("auftraege/?alle=1" if a.alle else "auftraege/")
    elif a.befehl == "auftrag-anlegen":
        status, antwort = anfrage(
            "auftraege/anlegen/",
            {
                "titel": a.titel,
                "beschreibung": a.text,
                "modul": a.modul,
                "prioritaet": a.prio,
                "erzwingen": a.erzwingen,
                "anfrage_kennung": kennung,
            },
        )
    elif a.befehl == "lesen":
        status, antwort = anfrage(f"auftraege/{_nummer(a.auftrag)}/")
    elif a.befehl == "uebernehmen":
        status, antwort = anfrage(
            f"auftraege/{_nummer(a.auftrag)}/uebernehmen/",
            {"revision": a.revision, "anfrage_kennung": kennung},
        )
    elif a.befehl in (
        "fortschritt",
        "hindernis",
        "rueckfrage",
        "fehler",
        "vorschlag",
        "pruefbericht",
    ):
        status, antwort = anfrage(
            f"auftraege/{_nummer(a.auftrag)}/{a.befehl}/",
            {"revision": a.revision, "text": a.text, "anfrage_kennung": kennung},
        )
    elif a.befehl == "ergebnis":
        status, antwort = anfrage(
            f"auftraege/{_nummer(a.auftrag)}/ergebnis/",
            {
                "revision": a.revision,
                "zusammenfassung": a.zusammenfassung,
                "dateien": a.datei,
                "pruefungen": a.pruefung,
                "luecken": a.luecken,
                "anfrage_kennung": kennung,
            },
        )
    elif a.befehl == "entscheidungen":
        status, antwort = anfrage("entscheidungen/")
    elif a.befehl == "bestaetigen":
        status, antwort = anfrage(
            f"entscheidungen/{_nummer(a.entscheidung)}/bestaetigen/", {"anfrage_kennung": kennung}
        )
    else:
        status, antwort = anfrage(
            "entscheidungen/vorschlagen/",
            {"titel": a.titel, "text": a.text, "verweis": a.verweis, "anfrage_kennung": kennung},
        )
    return _ausgeben(status, antwort, a.json)


if __name__ == "__main__":
    sys.exit(main())

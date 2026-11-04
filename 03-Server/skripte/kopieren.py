#!/opt/kipm/venv/bin/python
"""Kopie der täglichen Sicherung nach SharePoint (A-15, Entscheidung E-11). Läuft im Container als root.

  kipm-kopieren                     verschlüsselt die jüngste Sicherung und lädt sie hoch (Zeitplan 03:45)
  kipm-kopieren --einrichten        richtet alles nach einer einmaligen Anmeldung bei Microsoft selbst ein und prüft es
  kipm-kopieren --einrichten-von-hand  fragt die Angaben ab, falls die Anmeldung per Code gesperrt ist
  kipm-kopieren --pruefen          holt die jüngste Kopie, entschlüsselt sie, kipm-wiederherstellen --pruefen
  kipm-kopieren --fremde-website W  belegt, dass der Zugang eine andere Website nicht erreicht

Verschlüsselt wird mit dem öffentlichen age-Schlüssel: Der Server kann seine Kopien danach nicht mehr lesen; der
private Schlüssel wird nur einmal angezeigt und nie gespeichert. Der Server löscht auf SharePoint nichts.
Das Microsoft-Geheimnis steht nur in /etc/kipm/sharepoint.env (0600 root) und damit in keiner Sicherung; es erscheint
in keiner Ausgabe. Das Ergebnis jedes Laufs steht in kopie-stand.json; daraus warnt die Startseite (steuerung/sicherung.py).
Nur Standardbibliothek. sichern.sh bleibt davon unberührt.
"""

import argparse
import getpass
import gzip
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path

KONFIG = Path("/etc/kipm/sharepoint.env")
EMPFAENGER = Path("/etc/kipm/sicherung-empfaenger.txt")
ABLAGE = Path("/var/backups/kipm")
# Eigener Ordner von root: /var/lib/kipm gehört dem Dienstkonto, das dort Verknüpfungen auslegen und die Warnung fälschen könnte
STAND = Path("/var/lib/kipm-kopie/kopie-stand.json")
LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
AGE = ["age"]
AGE_KEYGEN = ["age-keygen"]
WIEDERHERSTELLEN = ["/usr/local/sbin/kipm-wiederherstellen"]
SICHERN = ["/usr/local/sbin/kipm-sichern"]
ORDNER = "kipm-sicherung"
# Einrichtung: Microsofts eigenes Werkzeug „Microsoft Graph Command Line Tools“, damit keine App vorab nötig ist
WERKZEUG_APP = "14d82eec-204b-4c2f-b7e8-296a70dab67e"
BERECHTIGUNGEN = (
    "User.Read Application.ReadWrite.All AppRoleAssignment.ReadWrite.All "
    "Group.ReadWrite.All Sites.FullControl.All"
)
GRAPH_APP = "00000003-0000-0000-c000-000000000000"
APP_NAME = "KI-PM Sicherung"
WEBSITE_KURZNAME = "kipm-sicherung"
MARKE = "Von kipm-kopieren angelegt: verschlüsselte Sicherung von Server-KI-PM"  # daran erkennt das Skript Eigenes
VERSUCHE = 20
PAUSE = 15
FELDER = ("KOPIE_MANDANT", "KOPIE_APP", "KOPIE_GEHEIMNIS", "KOPIE_WEBSITE")
# Täglich 03:30 gesichert, 03:45 kopiert: Die Vortagsdatei ist dann gut 24 Stunden alt und zählt nicht mehr
MAX_ALTER_STUNDEN = 20
FRIST = 300
GRENZE_HOCHLADEN = 250 * 1024 * 1024  # Grenze des einfachen Hochladens bei Microsoft


class KopieFehler(Exception):
    """Ein Grund in Klartext, ohne Geheimnis; steht im Protokoll und auf der Startseite."""


class _KeineWeiterleitung(urllib.request.HTTPRedirectHandler):
    """urllib nähme den Zugangsschlüssel im Kopf zu jedem Weiterleitungsziel mit."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _oeffner(mit_zugang: bool):
    griffe: list = [urllib.request.ProxyHandler({})]
    if mit_zugang:
        griffe.append(_KeineWeiterleitung())
    return urllib.request.build_opener(*griffe)


def _antwort(was: str, anfrage: urllib.request.Request, mit_zugang: bool = True) -> bytes:
    """Fehler nennen nur Stelle, Status und Microsofts Fehlercode, nie Rohtext oder Kopfzeilen."""
    try:
        with _oeffner(mit_zugang).open(anfrage, timeout=FRIST) as a:
            return a.read()
    except urllib.error.HTTPError as f:
        raise KopieFehler(f"{was}: {_fehlergrund(f)}") from None
    except (OSError, http.client.HTTPException) as f:
        raise KopieFehler(f"{was}: Microsoft nicht erreichbar ({type(f).__name__})") from None


def _fehlergrund(f: urllib.error.HTTPError) -> str:
    code = ""
    try:
        fehler = json.loads(f.read().decode("utf-8", "replace")).get("error")
        code = str(fehler.get("code") if isinstance(fehler, dict) else fehler or "")
    except (ValueError, OSError, AttributeError):
        pass
    return f"Status {f.code} {re.sub(r'[^A-Za-z0-9_.-]', '', code)[:60]}".strip()


def _json(was: str, anfrage: urllib.request.Request) -> dict:
    try:
        roh = _antwort(was, anfrage)
        return json.loads(roh.decode("utf-8")) if roh else {}
    except ValueError:
        raise KopieFehler(f"{was}: Antwort nicht lesbar") from None


def konfig_lesen() -> dict[str, str]:
    werte = {}
    try:
        zeilen = KONFIG.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise KopieFehler(f"{KONFIG} nicht lesbar; zuerst kipm-kopieren --einrichten") from None
    for zeile in zeilen:
        if "=" in zeile and not zeile.lstrip().startswith("#"):
            k, v = zeile.split("=", 1)
            werte[k.strip()] = v.strip()
    fehlt = [f for f in FELDER if not werte.get(f)]
    if fehlt:
        raise KopieFehler(f"In {KONFIG} fehlt: {', '.join(fehlt)}")
    return werte


def zugang(k: dict[str, str]) -> str:
    daten = urllib.parse.urlencode(
        {
            "client_id": k["KOPIE_APP"],
            "client_secret": k["KOPIE_GEHEIMNIS"],
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode()
    mandant = urllib.parse.quote(k["KOPIE_MANDANT"], safe="")
    try:
        antwort = _json(
            "Anmeldung bei Microsoft",
            urllib.request.Request(f"{LOGIN}/{mandant}/oauth2/v2.0/token", data=daten),
        )
    except KopieFehler as f:
        if "Status 400" in str(f) or "Status 401" in str(f):
            raise KopieFehler(
                f"{f}. Anmeldung abgelehnt: Ist das Geheimnis abgelaufen? Neu eintragen mit kipm-kopieren --einrichten."
            ) from None
        raise
    if not antwort.get("access_token"):
        raise KopieFehler("Anmeldung bei Microsoft: kein Zugangsschlüssel erhalten")
    return antwort["access_token"]


def _mit_zugang(url: str, token: str, **rest) -> urllib.request.Request:
    return urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", **rest.pop("kopf", {})}, **rest
    )


def website_kennung(token: str, website: str) -> str:
    pfad = urllib.parse.quote(website, safe=":/")
    kennung = _json("Website lesen", _mit_zugang(f"{GRAPH}/sites/{pfad}", token)).get("id")
    if not kennung:
        raise KopieFehler("Website lesen: keine Kennung erhalten")
    return kennung


def jetzt() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def stand_schreiben(**aenderung) -> None:
    """Teile des Stands bleiben erhalten: Eine gescheiterte Kopie löscht das Datum der letzten geglückten nicht."""
    try:
        stand = json.loads(STAND.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stand = {}
    stand = {**(stand if isinstance(stand, dict) else {}), **aenderung}
    tmp = STAND.with_name(f"{STAND.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(stand, ensure_ascii=False), encoding="utf-8")
    tmp.chmod(0o644)  # liest der Dienst für die Warnung auf der Startseite; enthält kein Geheimnis
    os.replace(tmp, STAND)


def juengste_sicherung() -> Path:
    staende = sorted(ABLAGE.glob("kipm-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    if not staende:
        raise KopieFehler(f"Keine Sicherung in {ABLAGE}")
    alter = (datetime.now().timestamp() - staende[-1].stat().st_mtime) / 3600
    if alter > MAX_ALTER_STUNDEN:
        raise KopieFehler(
            f"Die jüngste Sicherung ist {int(alter)} Stunden alt; kipm-sichern läuft offenbar nicht"
        )
    archiv_pruefen(staende[-1])
    return staende[-1]


def archiv_pruefen(pfad: Path) -> None:
    """Ein abgebrochenes kipm-sichern hinterlässt ein Teilstück: Das darf nicht als Tageskopie gelten."""
    try:
        with gzip.open(pfad) as g:
            while g.read(1024 * 1024):
                pass
        with tarfile.open(pfad, "r:gz") as t:
            namen = {os.path.normpath(n) for n in t.getnames()}
    except (OSError, EOFError, zlib.error, tarfile.TarError):
        raise KopieFehler(f"{pfad.name} ist beschädigt oder unvollständig") from None
    if not {"kipm.sqlite3", "MANIFEST.sha256"} <= namen:
        raise KopieFehler(f"{pfad.name} enthält keine vollständige Sicherung")


def _lauf(befehl: list[str], was: str, eingabe: bytes | None = None) -> None:
    """Keine Rohausgabe weiterreichen: Fehlertexte von age nennen Pfade, wir nennen nur den Schritt."""
    try:
        lauf = subprocess.run(befehl, capture_output=True, check=False, input=eingabe)
    except OSError as f:
        raise KopieFehler(f"{was}: Programm fehlt ({type(f).__name__})") from None
    if lauf.returncode != 0:
        raise KopieFehler(f"{was}: gescheitert (Rückgabe {lauf.returncode})")


def _hochladen(k: dict[str, str], quelle: Path, arbeit: Path) -> tuple[str, int]:
    if not EMPFAENGER.is_file():
        raise KopieFehler(f"{EMPFAENGER} fehlt; zuerst kipm-kopieren --einrichten")
    empfaenger = EMPFAENGER.read_text(encoding="utf-8").strip()
    name = f"{quelle.name}.age"
    verschluesselt = arbeit / name
    _lauf([*AGE, "-r", empfaenger, "-o", str(verschluesselt), str(quelle)], "Verschlüsseln")
    groesse = verschluesselt.stat().st_size
    if groesse >= GRENZE_HOCHLADEN:
        raise KopieFehler(
            f"Die Kopie ist {groesse // (1024 * 1024)} MB groß, einfaches Hochladen geht nur bis 250 MB"
        )
    inhalt = verschluesselt.read_bytes()
    token = zugang(k)
    site = website_kennung(token, k["KOPIE_WEBSITE"])
    ziel = urllib.parse.quote(f"{ORDNER}/{name}")
    antwort = _json(
        "Hochladen",
        _mit_zugang(
            f"{GRAPH}/sites/{site}/drive/root:/{ziel}:/content",
            token,
            data=inhalt,
            method="PUT",
            kopf={"Content-Type": "text/plain"},
        ),
    )
    if antwort.get("size") != len(inhalt):
        raise KopieFehler(
            f"Hochladen: Microsoft meldet {antwort.get('size')} Bytes statt {len(inhalt)}"
        )
    return name, len(inhalt)


def kopieren() -> int:
    if not KONFIG.exists():
        print("Kopie nach SharePoint ist nicht eingerichtet; nichts zu tun.")
        return 0
    try:
        k = konfig_lesen()
        quelle = juengste_sicherung()
        with tempfile.TemporaryDirectory(prefix="kipm-kopie-") as arbeit:
            name, groesse = _hochladen(k, quelle, Path(arbeit))
    except KopieFehler as f:
        stand_schreiben(fehler=str(f), fehler_zeit=jetzt())
        print(f"{jetzt()} FEHLER {f}")
        return 1
    except (
        Exception
    ) as f:  # Grund für die Warnung festhalten, nur die Art: Der Text könnte Werte nennen
        grund = f"Unerwarteter Fehler ({type(f).__name__})"
        stand_schreiben(fehler=grund, fehler_zeit=jetzt())
        print(f"{jetzt()} FEHLER {grund}")
        return 1
    stand_schreiben(
        letzte_kopie=jetzt(), datei=name, groesse=groesse, fehler=None, fehler_zeit=None
    )
    print(f"{jetzt()} OK {name}, {groesse} Bytes")
    return 0


def _frage(text: str, alt: str = "") -> str:
    wert = input(f"{text}{f' [{alt}]' if alt else ''}: ").strip()
    return wert or alt


def _konfig_schreiben(werte: dict[str, str]) -> None:
    tmp = KONFIG.with_name(
        f"{KONFIG.name}.{os.getpid()}.tmp"
    )  # ganz oder gar nicht: Das Geheimnis gibt es nur einmal
    griff = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(griff, "w", encoding="utf-8") as f:
        f.writelines(f"{k}={v}\n" for k, v in werte.items())
    os.chmod(tmp, 0o600)
    os.replace(tmp, KONFIG)
    print(f"Gespeichert in {KONFIG} (nur für root lesbar).")
    stand_schreiben(eingerichtet=jetzt())  # ab jetzt zählt das Ausbleiben der Kopie


def einrichten_von_hand() -> int:
    alt: dict[str, str] = {}
    if KONFIG.exists():
        try:
            alt = konfig_lesen()
        except KopieFehler:
            alt = {}
    werte = {
        "KOPIE_MANDANT": _frage("Mandanten-ID (Verzeichnis-ID)", alt.get("KOPIE_MANDANT", "")),
        "KOPIE_APP": _frage("Anwendungs-ID (Client-ID)", alt.get("KOPIE_APP", "")),
        "KOPIE_WEBSITE": _frage(
            "Website, z. B. firma.sharepoint.com:/sites/kipm-sicherung",
            alt.get("KOPIE_WEBSITE", ""),
        ),
    }
    # Geheimnis verdeckt; leer lassen behält das bisherige
    werte["KOPIE_GEHEIMNIS"] = getpass.getpass(
        "Geheimnis (Eingabe bleibt unsichtbar): "
    ).strip() or alt.get("KOPIE_GEHEIMNIS", "")
    fehlt = [f for f in FELDER if not werte[f]]
    if fehlt:
        print(f"Nichts gespeichert, es fehlt: {', '.join(fehlt)}")
        return 2
    _konfig_schreiben(werte)
    if _schluessel_anlegen() is None:
        return 2
    try:
        token = zugang(werte)
        website_kennung(token, werte["KOPIE_WEBSITE"])
    except KopieFehler as f:
        print(f"FEHLER {f}")
        return 1
    print("Verbindung zu Microsoft und zur Website funktioniert.")
    return 0


def _schluessel_anlegen() -> str | None:
    """Der öffentliche Schlüssel gilt erst, wenn Fred den privaten aus seiner Ablage wieder einfügt: Sonst liefen
    Kopien, die niemand öffnen kann. Rückgabe: neuer privater Schlüssel (nur im Speicher), "" bei bestehendem
    Schlüssel, None ohne Bestätigung."""
    if EMPFAENGER.exists():
        print(f"Der bestehende Schlüssel bleibt ({EMPFAENGER}).")
        return ""
    lauf = subprocess.run(AGE_KEYGEN, capture_output=True, check=True)
    privat = lauf.stdout.decode().strip()
    oeffentlich = re.search(r"age1[0-9a-z]+", lauf.stdout.decode() + lauf.stderr.decode())
    schluessel = re.search(r"AGE-SECRET-KEY-1[0-9A-Z]+", privat)
    if not oeffentlich or not schluessel:
        raise SystemExit("age-keygen lieferte keinen Schlüssel.")
    print("\n================ PRIVATER SCHLÜSSEL: JETZT SICHERN ================")
    print(
        "Kopiere die Zeile AGE-SECRET-KEY-... in deinen Passwortmanager und an einen zweiten Ort."
    )
    print(
        "Sie wird nie wieder angezeigt und nirgends gespeichert. Ohne sie sind alle Kopien wertlos.\n"
    )
    print(privat)
    print("===================================================================\n")
    for _ in range(3):
        eingabe = getpass.getpass(
            "Zur Kontrolle: Schlüssel aus deiner Ablage hier einfügen (bleibt unsichtbar): "
        ).strip()
        if eingabe == schluessel.group(0):
            EMPFAENGER.write_text(oeffentlich.group(0) + "\n", encoding="utf-8")
            EMPFAENGER.chmod(0o644)
            return eingabe
        print("Das stimmt nicht mit dem angezeigten Schlüssel überein.")
    print(
        "Nicht bestätigt: Der Schlüssel gilt nicht, es entstehen keine Kopien. Später erneut --einrichten."
    )
    return None


def anmelden() -> str:
    """Anmeldung per Code: Fred meldet sich im eigenen Browser an; der Server sieht kein Passwort. Das Ergebnis bleibt
    nur im Speicher dieses Laufs."""

    def formular(pfad: str, **werte) -> urllib.request.Request:
        return urllib.request.Request(
            f"{LOGIN}/organizations/oauth2/v2.0/{pfad}",
            data=urllib.parse.urlencode({"client_id": WERKZEUG_APP, **werte}).encode(),
        )

    start = _json("Anmeldung vorbereiten", formular("devicecode?mkt=de-DE", scope=BERECHTIGUNGEN))
    if not start.get("device_code"):
        raise KopieFehler("Anmeldung vorbereiten: kein Code erhalten")
    print(f"\n>>> {start.get('message')}", flush=True)
    print(
        ">>> Melde dich mit einem globalen Administratorkonto an. Das Häkchen „für die Organisation“ NICHT setzen.\n",
        flush=True,
    )
    pause = int(start.get("interval", 5))
    ende = time.monotonic() + int(start.get("expires_in", 900))
    while time.monotonic() < ende:
        time.sleep(pause)
        try:
            antwort = _json(
                "Anmeldung",
                formular(
                    "token",
                    grant_type="urn:ietf:params:oauth:grant-type:device_code",
                    device_code=start["device_code"],
                ),
            )
        except KopieFehler as f:
            if "authorization_pending" in str(f):
                continue
            if "slow_down" in str(f):
                pause += 5
                continue
            raise
        if antwort.get("access_token"):
            return antwort["access_token"]
    raise KopieFehler("Anmeldung: Die Zeit ist abgelaufen")


def _graph(was: str, token: str, pfad: str, daten: dict | None = None) -> dict:
    roh = json.dumps(daten).encode() if daten is not None else None
    kopf = {"Content-Type": "application/json"} if daten is not None else {}
    return _json(was, _mit_zugang(f"{GRAPH}{pfad}", token, data=roh, kopf=kopf))


def _falls_vorhanden(was: str, token: str, pfad: str) -> dict | None:
    try:
        return _graph(was, token, pfad)
    except KopieFehler as f:
        if "Status 404" in str(f):
            return None
        raise


def _wiederholen(schritt):
    """Neue Apps, Websites und Freigaben brauchen bei Microsoft etwas Zeit, bis sie überall gelten."""
    for versuch in range(VERSUCHE):
        try:
            return schritt()
        except KopieFehler:
            if versuch + 1 == VERSUCHE:
                raise
            time.sleep(PAUSE)
    raise KopieFehler("Wiederholen: keine Versuche")


def _filter(pfad: str, bedingung: str) -> str:
    return f"{pfad}?$filter={urllib.parse.quote(bedingung)}"


def _website_einrichten(token: str, host: str) -> dict:
    """Vorhandenes nur übernehmen, wenn das Skript es selbst angelegt hat (MARKE): Fremdes bleibt unangetastet."""
    gruppen = _graph(
        "Gruppe suchen",
        token,
        _filter("/groups", f"mailNickname eq '{WEBSITE_KURZNAME}'")
        + "&$select=id,description,visibility",
    )["value"]
    if gruppen:
        if gruppen[0].get("description") != MARKE or gruppen[0].get("visibility") != "Private":
            raise KopieFehler(
                f"Es gibt schon eine Gruppe {WEBSITE_KURZNAME}, die nicht von diesem Skript stammt; sie bleibt unberührt"
            )
        gruppe = gruppen[0]["id"]
        print(f"Website {WEBSITE_KURZNAME} ist schon da.")
    else:
        if _falls_vorhanden("Website suchen", token, f"/sites/{host}:/sites/{WEBSITE_KURZNAME}"):
            raise KopieFehler(
                f"Es gibt schon eine Website {WEBSITE_KURZNAME}, die nicht von diesem Skript stammt; sie bleibt unberührt"
            )
        ich = _graph("Konto lesen", token, "/me")["id"]
        benutzer = f"{GRAPH}/users/{ich}"
        print("Website wird angelegt, das kann einige Minuten dauern ...")
        gruppe = _graph(
            "Website anlegen",
            token,
            "/groups",
            {
                "displayName": WEBSITE_KURZNAME,
                "description": MARKE,
                "groupTypes": ["Unified"],
                "mailEnabled": True,
                "mailNickname": WEBSITE_KURZNAME,
                "securityEnabled": False,
                "visibility": "Private",
                "owners@odata.bind": [benutzer],
                "members@odata.bind": [benutzer],
            },
        )["id"]
    return _wiederholen(lambda: _graph("Website lesen", token, f"/groups/{gruppe}/sites/root"))


def _app_einrichten(token: str, rolle: str) -> dict:
    """Namen sind nicht eindeutig: Eine fremde App gleichen Namens verlöre sonst ihre Geheimnisse."""
    gefunden = _graph(
        "App suchen",
        token,
        _filter("/applications", f"displayName eq '{APP_NAME}'")
        + "&$select=id,appId,notes,passwordCredentials,requiredResourceAccess",
    )["value"]
    if gefunden:
        if len(gefunden) > 1 or gefunden[0].get("notes") != MARKE:
            raise KopieFehler(
                f"Es gibt schon eine App {APP_NAME}, die nicht von diesem Skript stammt; sie bleibt unberührt"
            )
        print(f"App {APP_NAME} ist schon da.")
        return gefunden[0]
    return _graph(
        "App anlegen",
        token,
        "/applications",
        {
            "displayName": APP_NAME,
            "notes": MARKE,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [
                {"resourceAppId": GRAPH_APP, "resourceAccess": [{"id": rolle, "type": "Role"}]}
            ],
        },
    )


def _freigabe_vorhanden(freigaben: list[dict], app_id: str) -> bool:
    """Gibt es die Freigabe schon? Eine weiter reichende Rolle ist ein Fehler, keine Freigabe."""
    for f in freigaben:
        for identitaet in [*f.get("grantedToIdentitiesV2", []), *f.get("grantedToIdentities", [])]:
            if identitaet.get("application", {}).get("id") == app_id:
                if f.get("roles", ["write"]) != ["write"]:
                    raise KopieFehler(
                        f"Die App hat auf der Website weitere Rechte: {f.get('roles')}"
                    )
                return True
    return False


def _microsoft_einrichten(token: str) -> tuple[dict[str, str], str, dict]:
    """Legt an, was fehlt, und übernimmt nur Eigenes: Ein abgebrochener Lauf lässt sich einfach wiederholen.
    Die App bekommt nur Sites.Selected und nur für diese eine Website. Ein neues Geheimnis entsteht zuletzt."""
    mandant = _graph("Verzeichnis lesen", token, "/organization")["value"][0]["id"]
    host = _graph("Stammwebsite lesen", token, "/sites/root?$select=id,siteCollection")[
        "siteCollection"
    ]["hostname"]
    website = _website_einrichten(token, host)
    adresse = urllib.parse.urlsplit(website["webUrl"])

    graph_dienst = _graph(
        "Microsoft Graph lesen",
        token,
        f"/servicePrincipals(appId='{GRAPH_APP}')?$select=id,appRoles",
    )
    rolle = next(
        (
            r["id"]
            for r in graph_dienst["appRoles"]
            if r.get("value") == "Sites.Selected"
            and "Application" in r.get("allowedMemberTypes", [])
        ),
        None,
    )
    if not rolle:
        raise KopieFehler("Microsoft Graph lesen: Berechtigung Sites.Selected nicht gefunden")

    app = _app_einrichten(token, rolle)
    app_id = app["appId"]
    dienst = _falls_vorhanden(
        "Dienstkonto suchen", token, f"/servicePrincipals(appId='{app_id}')"
    ) or _wiederholen(
        lambda: _graph("Dienstkonto anlegen", token, "/servicePrincipals", {"appId": app_id})
    )
    zuweisungen = _wiederholen(
        lambda: _graph(
            "Berechtigung lesen", token, f"/servicePrincipals/{dienst['id']}/appRoleAssignments"
        )
    )["value"]
    if any(z.get("appRoleId") != rolle for z in zuweisungen):
        raise KopieFehler("Die App hat weitere Rechte als Sites.Selected; sie bleibt unberührt")
    if not zuweisungen:
        _wiederholen(
            lambda: _graph(
                "Berechtigung erteilen",
                token,
                f"/servicePrincipals/{dienst['id']}/appRoleAssignments",
                {"principalId": dienst["id"], "resourceId": graph_dienst["id"], "appRoleId": rolle},
            )
        )
    freigaben = _wiederholen(
        lambda: _graph("Freigabe lesen", token, f"/sites/{website['id']}/permissions")
    )["value"]
    if not _freigabe_vorhanden(freigaben, app_id):
        _wiederholen(
            lambda: _graph(
                "Website freigeben",
                token,
                f"/sites/{website['id']}/permissions",
                {
                    "roles": ["write"],
                    "grantedToIdentities": [
                        {"application": {"id": app_id, "displayName": APP_NAME}}
                    ],
                },
            )
        )
    geheimnis = _graph(
        "Geheimnis erzeugen",
        token,
        f"/applications/{app['id']}/addPassword",
        {"passwordCredential": {"displayName": "kipm-kopieren"}},
    )["secretText"]
    return (
        {
            "KOPIE_MANDANT": mandant,
            "KOPIE_APP": app_id,
            "KOPIE_GEHEIMNIS": geheimnis,
            "KOPIE_WEBSITE": f"{adresse.netloc}:{adresse.path}",
        },
        host,
        app,
    )


def _alte_geheimnisse_entfernen(token: str, app: dict) -> None:
    """Erst nach dem Speichern des neuen. Scheitert es, bleibt nur ein überzähliges Geheimnis gültig."""
    for alt in app.get("passwordCredentials") or []:
        try:
            _graph(
                "Altes Geheimnis entfernen",
                token,
                f"/applications/{app['id']}/removePassword",
                {"keyId": alt["keyId"]},
            )
        except KopieFehler as f:
            print(f"Hinweis: {f}. Der nächste Lauf versucht es erneut.")


def _sicherung_frisch() -> bool:
    """Jede Sicherung verdrängt eine alte: Wiederholte Läufe sollen den Tagesverlauf nicht aufzehren."""
    staende = [p.stat().st_mtime for p in ABLAGE.glob("kipm-*.tar.gz")]
    return bool(staende) and datetime.now().timestamp() - max(staende) < 3600


def _selbstpruefung(werte: dict[str, str], schluessel: str, host: str) -> int:
    print("\nDie App wird bei Microsoft freigeschaltet, das kann einige Minuten dauern ...")
    _wiederholen(lambda: website_kennung(zugang(werte), werte["KOPIE_WEBSITE"]))
    if not _sicherung_frisch():
        print("Frische Sicherung ...")
        _lauf(SICHERN, "Sicherung")
    print("Erste Kopie hochladen ...")
    for versuch in range(VERSUCHE):
        if kopieren() == 0:
            break
        if versuch + 1 == VERSUCHE:
            raise KopieFehler("Die erste Kopie ließ sich nicht hochladen")
        time.sleep(PAUSE)
    print("Kopie zurückholen, entschlüsseln und prüfen ...")
    probe = pruefen(schluessel)
    print("Zugriff auf eine andere Website muss abgewiesen werden ...")
    fremd = fremde_website(host)
    if probe != 0 or fremd != 0:
        print(f"\nNICHT BESTANDEN: Probe Rückgabe {probe}, andere Website Rückgabe {fremd}.")
        return 1
    print(
        "\nFERTIG: Kopie liegt auf SharePoint, die Probe ist bestanden, die Stammwebsite ist gesperrt."
    )
    print(
        "Zum Schluss deine Zustimmung entziehen: Entra-Admin-Center, Unternehmensanwendungen,\n"
        "„Microsoft Graph Command Line Tools“, Berechtigungen. Für die täglichen Kopien wird sie nicht gebraucht."
    )
    return 0


def selbst_einrichten() -> int:
    try:
        token = anmelden()
        werte, host, app = _microsoft_einrichten(token)
        _konfig_schreiben(werte)
        _alte_geheimnisse_entfernen(token, app)
        schluessel = _schluessel_anlegen()
        if schluessel is None:
            return 2
        return _selbstpruefung(werte, schluessel, host)
    except KopieFehler as f:
        print(f"\nFEHLER {f}")
        print(
            "Der Aufruf lässt sich wiederholen; Fremdes bleibt unberührt, das gespeicherte Geheimnis bleibt gültig."
        )
        print(
            "Hat dein Konto globale Administratorrechte? Ist die Anmeldung per Code gesperrt: --einrichten-von-hand."
        )
        return 1
    except (
        KeyError,
        IndexError,
        TypeError,
        OSError,
    ) as f:  # nur die Art: Der Text könnte Werte nennen
        print(f"\nFEHLER Unerwartete Antwort oder Schreibfehler ({type(f).__name__}).")
        print("Der Aufruf lässt sich wiederholen; das gespeicherte Geheimnis bleibt gültig.")
        return 1


def _alle_kopien(token: str, site: str) -> list[dict]:
    url: str | None = f"{GRAPH}/sites/{site}/drive/root:/{ORDNER}:/children"
    kopien: list[dict] = []
    while url:
        seite = _json("Kopien auflisten", _mit_zugang(url, token))
        kopien += seite.get("value", [])
        url = seite.get("@odata.nextLink")
    return [k for k in kopien if str(k.get("name", "")).endswith(".tar.gz.age")]


def pruefen(schluessel: str = "") -> int:
    """Abnahme: Von SharePoint zurückholen, entschlüsseln und mit der vorhandenen Probe prüfen; nichts Produktives."""
    try:
        k = konfig_lesen()
        token = zugang(k)
        kopien = _alle_kopien(token, website_kennung(token, k["KOPIE_WEBSITE"]))
        if not kopien:
            raise KopieFehler("Auf SharePoint liegt noch keine Kopie")
        kopie = max(kopien, key=lambda x: x["name"])
        print(f"Jüngste Kopie: {kopie['name']}, {kopie.get('size')} Bytes")
        # Vorab signierte Adresse: ohne Zugangsschlüssel abrufen, sie kann woandershin weiterleiten
        adresse = kopie.get("@microsoft.graph.downloadUrl")
        if not adresse:
            raise KopieFehler("Kopie laden: keine Abrufadresse erhalten")
        inhalt = _antwort("Kopie laden", urllib.request.Request(adresse), mit_zugang=False)
        schluessel = (
            schluessel
            or getpass.getpass(
                "Privater Schlüssel AGE-SECRET-KEY-... (Eingabe bleibt unsichtbar): "
            ).strip()
        )
        with tempfile.TemporaryDirectory(prefix="kipm-probe-") as arbeit:
            ort = Path(arbeit)
            (ort / "kopie.age").write_bytes(inhalt)
            sicherung = ort / "kipm-probe.tar.gz"
            # Der Schlüssel läuft nur über die Standardeingabe: Er liegt nie als Datei auf dem Server
            _lauf(
                [*AGE, "-d", "-i", "-", "-o", str(sicherung), str(ort / "kopie.age")],
                "Entschlüsseln (falscher Schlüssel?)",
                eingabe=(schluessel + "\n").encode(),
            )
            return subprocess.run(
                [*WIEDERHERSTELLEN, "--pruefen", str(sicherung)], check=False
            ).returncode
    except KopieFehler as f:
        print(f"FEHLER {f}")
        return 1


def fremde_website(website: str) -> int:
    """Abnahme: Der Zugang reicht nur für die eine Website. Erfolg heißt hier: abgewiesen."""
    try:
        token = zugang(konfig_lesen())
    except KopieFehler as f:
        print(f"FEHLER {f}")
        return 1
    pfad = urllib.parse.quote(website, safe=":/")
    try:
        _antwort("Andere Website lesen", _mit_zugang(f"{GRAPH}/sites/{pfad}", token))
    except KopieFehler as f:
        if "Status 403" in str(f):
            print(f"OK: abgewiesen ({f})")
            return 0
        if "Status 404" in str(f):  # ein Tippfehler darf nicht als Beleg gelten
            print(f"UNKLAR: nicht gefunden ({f}). Nimm eine Website, die es nachweislich gibt.")
            return 1
        print(f"UNKLAR: {f}")
        return 1
    print("FEHLER: Der Zugang erreicht auch diese Website. Er ist zu weit gefasst.")
    return 1


def main(argumente: list[str] | None = None) -> int:
    teile = argparse.ArgumentParser(description="Kopie der Sicherung nach SharePoint")
    gruppe = teile.add_mutually_exclusive_group()
    gruppe.add_argument("--einrichten", action="store_true")
    gruppe.add_argument("--einrichten-von-hand", action="store_true")
    gruppe.add_argument("--pruefen", action="store_true")
    gruppe.add_argument("--fremde-website", metavar="WEBSITE")
    a = teile.parse_args(argumente)
    if a.einrichten:
        return selbst_einrichten()
    if a.einrichten_von_hand:
        return einrichten_von_hand()
    if a.pruefen:
        return pruefen()
    if a.fremde_website:
        return fremde_website(a.fremde_website)
    return kopieren()


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # Anmeldecode sofort zeigen, auch ohne Terminal
    # Reißt die Sitzung ab, räumen die Arbeitsordner trotzdem auf (entschlüsselte Probe in --pruefen)
    for zeichen in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(zeichen, lambda *_: sys.exit(1))
    sys.exit(main())

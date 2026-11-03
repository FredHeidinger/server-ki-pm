"""Private Dateiablage für Unterlagen.

Ablauf eines Uploads: begrenzte temporäre Datei (Größe beim Streamen geprüft) → Prüfung von
Name, Endung, Inhaltstyp und Struktur → dauerhafter Blob mit zufälliger ID → Datenbankversion
(Dienst) → Schadsoftwareprüfung → Extraktion in einem eigenen, begrenzten Prozess.

Blobs liegen außerhalb der statischen Dateien und werden nur über eine berechtigte Ansicht als
Anhang ausgeliefert. Originalnamen sind Metadaten, nie Pfade.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload

from .dienst import DienstFehler

MIB = 1024 * 1024
OFFICE_WORD = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OFFICE_EXCEL = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
ERLAUBT = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".docx": OFFICE_WORD,
    ".xlsx": OFFICE_EXCEL,
}
GEFAEHRLICH = {
    "exe",
    "dll",
    "com",
    "bat",
    "cmd",
    "ps1",
    "psm1",
    "sh",
    "bash",
    "js",
    "mjs",
    "vbs",
    "vbe",
    "wsf",
    "hta",
    "html",
    "htm",
    "xhtml",
    "svg",
    "php",
    "py",
    "pl",
    "rb",
    "jar",
    "msi",
    "scr",
    "lnk",
    "iso",
    "zip",
    "7z",
    "rar",
    "docm",
    "xlsm",
    "pptm",
    "dotm",
    "xltm",
    "xlam",
    "reg",
}
_BLOB_ID = re.compile(r"^[0-9a-f]{32}$")
SPERRDATEI = ".sicherung-laeuft"


class Abgewiesen(DienstFehler):
    """Datei passt nicht zu den Uploadregeln; nichts wurde gespeichert."""


def _wurzel() -> Path:
    return Path(settings.KIPM_ABLAGE_DIR)


def _ordner(name: str) -> Path:
    pfad = _wurzel() / name
    pfad.mkdir(parents=True, exist_ok=True)
    return pfad


def datei_hash(pfad: Path) -> str:
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for stueck in iter(lambda: f.read(1024 * 1024), b""):
            h.update(stueck)
    return h.hexdigest()


def blob_pfad(blob_id: str) -> Path:
    if not _BLOB_ID.match(blob_id or ""):
        raise Abgewiesen("Ungültige Datei-ID.")
    return _wurzel() / "blobs" / blob_id


class Grenzwaechter(FileUploadHandler):
    """Bricht einen Upload schon beim Empfang ab, sobald eine Datei die Grenze überschreitet."""

    def __init__(self, request=None):
        super().__init__(request)
        self.empfangen = 0
        self.ueberschritten = False

    def new_file(self, *args, **kwargs):
        self.empfangen = 0
        super().new_file(*args, **kwargs)

    def receive_data_chunk(self, raw_data, start):
        self.empfangen += len(raw_data)
        if self.empfangen > settings.KIPM_UPLOAD_MAX:
            self.ueberschritten = True
            if self.request is not None:
                self.request.upload_zu_gross = True
            raise StopUpload(connection_reset=False)
        return raw_data

    def file_complete(self, file_size):
        return None


def name_pruefen(name: str) -> tuple[str, str]:
    """Nur der letzte Pfadteil zählt; Steuerzeichen raus; Endung erlaubt, keine gefährliche
    Zwischenendung (``bericht.exe.pdf``)."""
    basis = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    basis = "".join(z for z in basis if z.isprintable()).strip().strip(".")
    if not basis or basis in ("..", "."):
        raise Abgewiesen("Der Dateiname ist ungültig.")
    teile = basis.lower().split(".")
    if len(teile) < 2:
        raise Abgewiesen("Die Datei hat keine Endung.")
    endung = "." + teile[-1]
    if endung not in ERLAUBT:
        raise Abgewiesen(
            f"Dateityp {endung} ist nicht erlaubt. Erlaubt: PDF, PNG, JPEG, TXT, Markdown, DOCX, XLSX."
        )
    if any(t in GEFAEHRLICH for t in teile[1:-1]):
        raise Abgewiesen("Doppelte Endung mit ausführbarem oder aktivem Typ ist nicht erlaubt.")
    return basis[:200], endung


@dataclass
class Eingang:
    """Geprüfte, noch temporäre Datei. ``dauerhaft`` verschiebt sie in die Blob-Ablage."""

    pfad: Path
    sha256: str
    groesse: int
    inhaltstyp: str
    originalname: str

    def verwerfen(self) -> None:
        self.pfad.unlink(missing_ok=True)

    def dauerhaft(self) -> str:
        blob = secrets.token_hex(16)
        ziel = _ordner("blobs") / blob
        os.replace(self.pfad, ziel)
        try:
            os.chmod(ziel, 0o600)
        except OSError:
            pass
        return blob


def annehmen(datei) -> Eingang:
    """Hochgeladene Datei begrenzt in den Quarantäneordner schreiben und vollständig prüfen."""
    name, endung = name_pruefen(getattr(datei, "name", ""))
    tmp = _ordner("quarantaene") / secrets.token_hex(16)
    h = hashlib.sha256()
    groesse = 0
    try:
        with open(tmp, "wb") as ziel:
            for stueck in datei.chunks():
                groesse += len(stueck)
                if groesse > settings.KIPM_UPLOAD_MAX:
                    raise Abgewiesen(
                        f"Die Datei ist größer als {settings.KIPM_UPLOAD_MAX // MIB} MiB."
                    )
                h.update(stueck)
                ziel.write(stueck)
        if groesse == 0:
            raise Abgewiesen("Die Datei ist leer.")
        inhaltstyp = inhalt_pruefen(tmp, endung)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Eingang(tmp, h.hexdigest(), groesse, inhaltstyp, name)


def _pdf_pruefen(pfad: Path) -> None:
    """Zweistufig, im Zweifel abweisen: ein schneller Scan auf eindeutige Marker (auch hinter
    Namens-Escapes und in Scheinströmen), danach die Struktur wie ein PDF-Leser in einem eigenen,
    zeit- und speicherbegrenzten Prozess (``extrakt.py pdf-pruefen``)."""
    with open(pfad, "rb") as f:
        kopf = f.read(1024)
        if not kopf.startswith(b"%PDF-"):
            raise Abgewiesen("Inhalt ist kein PDF (falscher Typ für die Endung).")
        rest = kopf + f.read()
    normal = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), rest)
    if re.search(rb"/Encrypt(?![A-Za-z])", normal):
        raise Abgewiesen("Das PDF ist verschlüsselt und lässt sich nicht prüfen.")
    if re.search(rb"/(JavaScript|Launch|EmbeddedFile|RichMedia)(?![A-Za-z])", normal):
        raise Abgewiesen("Das PDF enthält aktive Inhalte oder eingebettete Dateien.")
    ergebnis = _im_kindprozess(pfad, "pdf-pruefen")
    if ergebnis is None:
        raise Abgewiesen("Das PDF lässt sich nicht prüfen (Zeit- oder Speichergrenze).")
    if not ergebnis.get("ok"):
        grund = str(ergebnis.get("grund", ""))
        if grund.startswith(("aktiv", "Aktion", "eingebettet")):
            raise Abgewiesen("Das PDF enthält aktive Inhalte oder eingebettete Dateien.")
        raise Abgewiesen(f"Das PDF lässt sich nicht prüfen: {grund}.")


def _im_kindprozess(pfad: Path, auftrag: str) -> dict | None:
    """``extrakt.py`` isoliert ausführen; ``None`` bei Abbruch, Grenze oder unlesbarer Antwort."""
    skript = Path(__file__).with_name("extrakt.py")
    try:
        lauf = subprocess.run(
            [sys.executable, "-I", str(skript), str(pfad), auftrag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=settings.KIPM_EXTRAKT_SEKUNDEN,
            check=False,
            preexec_fn=_begrenzen if os.name == "posix" else None,
            env={"PYTHONIOENCODING": "utf-8", "PATH": os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired:
        return None
    if lauf.returncode != 0:
        return None
    try:
        return json.loads(lauf.stdout or "")
    except ValueError:
        return None


def _bild_pruefen(pfad: Path, endung: str) -> None:
    with open(pfad, "rb") as f:
        daten = f.read(min(os.path.getsize(pfad), 4 * MIB))
    if endung == ".png":
        if not daten.startswith(b"\x89PNG\r\n\x1a\n") or daten[12:16] != b"IHDR":
            raise Abgewiesen("Inhalt ist kein PNG (falscher Typ für die Endung).")
        breite = int.from_bytes(daten[16:20], "big")
        hoehe = int.from_bytes(daten[20:24], "big")
    else:
        if not daten.startswith(b"\xff\xd8\xff"):
            raise Abgewiesen("Inhalt ist kein JPEG (falscher Typ für die Endung).")
        breite = hoehe = 0
        i = 2
        while i + 9 < len(daten):
            if daten[i] != 0xFF:
                i += 1
                continue
            marke = daten[i + 1]
            laenge = int.from_bytes(daten[i + 2 : i + 4], "big")
            if marke in (
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                hoehe = int.from_bytes(daten[i + 5 : i + 7], "big")
                breite = int.from_bytes(daten[i + 7 : i + 9], "big")
                break
            i += 2 + laenge
        if not (breite and hoehe):
            raise Abgewiesen("Die Bildgröße des JPEG lässt sich nicht bestimmen.")
    if breite * hoehe > settings.KIPM_BILD_MAX_PIXEL:
        raise Abgewiesen(
            f"Das Bild hat {breite}×{hoehe} Pixel; erlaubt sind höchstens "
            f"{settings.KIPM_BILD_MAX_PIXEL // 1_000_000} Megapixel."
        )


def _text_pruefen(pfad: Path) -> None:
    daten = pfad.read_bytes()
    if b"\x00" in daten:
        raise Abgewiesen("Die Textdatei enthält Binärdaten.")
    try:
        daten.decode("utf-8")
    except UnicodeDecodeError as fehler:
        raise Abgewiesen("Die Textdatei ist nicht in UTF-8 kodiert.") from fehler


def office_pruefen(pfad: Path, endung: str) -> None:
    """OOXML ohne Makros, aktive Objekte, externe Beziehungen; Archivgrenzen eingehalten."""
    if not zipfile.is_zipfile(pfad):
        raise Abgewiesen("Inhalt ist kein Office-Dokument (falscher Typ für die Endung).")
    try:
        with zipfile.ZipFile(pfad) as z:
            eintraege = z.infolist()
            if len(eintraege) > settings.KIPM_OFFICE_MAX_EINTRAEGE:
                raise Abgewiesen("Das Dokument hat zu viele Archiveinträge.")
            gesamt = 0
            for e in eintraege:
                name = e.filename
                if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                    raise Abgewiesen("Das Dokument enthält unzulässige Pfade.")
                gesamt += e.file_size
                if e.compress_size and e.file_size / e.compress_size > 200 and e.file_size > MIB:
                    raise Abgewiesen("Das Dokument ist auffällig stark komprimiert (Archivbombe).")
                klein = name.lower()
                if (
                    klein.endswith("vbaproject.bin")
                    or "activex" in klein
                    or ("/embeddings/" in klein and not klein.endswith((".png", ".jpeg", ".jpg")))
                ):
                    raise Abgewiesen(
                        "Das Dokument enthält Makros oder eingebettete aktive Objekte."
                    )
            if gesamt > settings.KIPM_OFFICE_MAX_ENTPACKT:
                raise Abgewiesen("Das Dokument ist entpackt zu groß.")
            namen = {e.filename for e in eintraege}
            haupt = "word/document.xml" if endung == ".docx" else "xl/workbook.xml"
            if "[Content_Types].xml" not in namen or haupt not in namen:
                raise Abgewiesen("Das Dokument hat nicht die erwartete Struktur.")
            typen = _begrenzt_lesen(z, "[Content_Types].xml")
            if b"macroEnabled" in typen or b"vbaProject" in typen:
                raise Abgewiesen("Das Dokument ist makrofähig.")
            for name in namen:
                if name.endswith(".rels"):
                    _beziehungen_pruefen(_begrenzt_lesen(z, name))
    except zipfile.BadZipFile as fehler:
        raise Abgewiesen("Das Office-Dokument ist beschädigt.") from fehler


def _beziehungen_pruefen(daten: bytes) -> None:
    """Beziehungen per XML-Parser lesen (Anführungszeichen, Namensräume egal); nur externe
    Hyperlinks sind erlaubt. DTDs und Entitäten werden gar nicht erst geparst."""
    if b"<!DOCTYPE" in daten or b"<!ENTITY" in daten:
        raise Abgewiesen("Das Dokument enthält eine unzulässige DTD.")
    try:
        wurzel = ET.fromstring(daten)
    except ET.ParseError as fehler:
        raise Abgewiesen("Das Office-Dokument ist beschädigt.") from fehler
    for rel in wurzel.iter():
        if not rel.tag.endswith("Relationship"):
            continue
        if (rel.get("TargetMode") or "").lower() == "external" and not (
            rel.get("Type") or ""
        ).endswith("/hyperlink"):
            raise Abgewiesen("Das Dokument verweist auf externe Inhalte.")


def _begrenzt_lesen(z: zipfile.ZipFile, name: str, grenze: int = 2 * MIB) -> bytes:
    with z.open(name) as f:
        daten = f.read(grenze + 1)
    if len(daten) > grenze:
        raise Abgewiesen("Ein Dokumentteil ist zu groß.")
    return daten


def inhalt_pruefen(pfad: Path, endung: str) -> str:
    if endung == ".pdf":
        _pdf_pruefen(pfad)
    elif endung in (".png", ".jpg", ".jpeg"):
        _bild_pruefen(pfad, endung)
    elif endung in (".txt", ".md"):
        _text_pruefen(pfad)
    else:
        office_pruefen(pfad, endung)
    return ERLAUBT[endung]


# --- Schadsoftwareprüfung --------------------------------------------------------------------


def scannen(pfad: Path) -> tuple[str, str]:
    """(Zustand, Hinweis). Ohne eingerichteten Scanner oder bei Zeitüberschreitung bleibt die
    Datei „ausstehend“: kein Download, keine Extraktion, keine KI-Nutzung."""
    art = settings.KIPM_SCANNER
    if art == "test":
        return "sauber", "Testadapter: keine echte Schadsoftwareprüfung"
    if art != "clamdscan":
        return "ausstehend", "kein Prüfdienst eingerichtet"
    try:
        lauf = subprocess.run(
            ["clamdscan", "--no-summary", "--fdpass", str(pfad)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as fehler:
        return "ausstehend", f"Prüfdienst nicht erreichbar: {type(fehler).__name__}"
    if lauf.returncode == 0:
        return "sauber", "ClamAV: unauffällig"
    if lauf.returncode == 1:
        return "abgewiesen", "ClamAV: Schadsoftware gefunden"
    return "ausstehend", "ClamAV-Fehler: " + (lauf.stderr or lauf.stdout).strip()[:200]


# --- Extraktion ------------------------------------------------------------------------------


@dataclass
class Extrakt:
    zustand: str
    text: str
    hinweis: str


TEXTGRENZE = 200_000


def _begrenzen():  # pragma: no cover - läuft nur im Kindprozess unter POSIX
    import resource

    grenze = settings.KIPM_EXTRAKT_MIB * MIB
    resource.setrlimit(resource.RLIMIT_AS, (grenze, grenze))
    resource.setrlimit(
        resource.RLIMIT_CPU, (settings.KIPM_EXTRAKT_SEKUNDEN, settings.KIPM_EXTRAKT_SEKUNDEN)
    )


def extrahieren(pfad: Path, inhaltstyp: str) -> Extrakt:
    """Text für Vorschau und KI-Kontext; das Original bleibt unverändert. Bilder bleiben Bilder."""
    if inhaltstyp in ("text/plain", "text/markdown"):
        text = pfad.read_bytes().decode("utf-8")
        if len(text) > TEXTGRENZE:
            return Extrakt("teilweise", text[:TEXTGRENZE], f"gekürzt auf {TEXTGRENZE} Zeichen")
        return Extrakt("vollstaendig", text, "")
    if inhaltstyp.startswith("image/"):
        return Extrakt("nicht_auswertbar", "", "Bilder werden nicht ausgelesen (OCR folgt später)")
    daten = _im_kindprozess(pfad, inhaltstyp)
    if daten is None:
        return Extrakt(
            "nicht_auswertbar", "", "Extraktion abgebrochen (Zeit-, Speichergrenze oder Fehler)"
        )
    return Extrakt(daten["zustand"], daten["text"][:TEXTGRENZE], daten.get("hinweis", ""))


# --- Bereinigung -----------------------------------------------------------------------------


def bereinigen(referenziert: set[str], schonfrist_stunden: float = 24) -> list[str]:
    """Unreferenzierte Blobs und alte Quarantänedateien nach Schonfrist entfernen. Läuft eine
    Sicherung (Sperrdatei), passiert nichts. Referenzierte Blobs bleiben immer."""
    if (_wurzel() / SPERRDATEI).exists():
        return []
    grenze = time.time() - schonfrist_stunden * 3600
    entfernt = []
    for ordner, nur_unreferenziert in (("quarantaene", False), ("blobs", True)):
        verzeichnis = _wurzel() / ordner
        if not verzeichnis.is_dir():
            continue
        for datei in verzeichnis.iterdir():
            if not datei.is_file() or datei.stat().st_mtime > grenze:
                continue
            if nur_unreferenziert and datei.name in referenziert:
                continue
            datei.unlink()
            entfernt.append(f"{ordner}/{datei.name}")
    return entfernt


def freier_speicher() -> int:
    return shutil.disk_usage(_ordner("blobs")).free

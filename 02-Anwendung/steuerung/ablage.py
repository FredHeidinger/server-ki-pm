"""Private Dateiablage früher hochgeladener Unterlagen; neue Dateien kommen nicht hinzu.

Blobs liegen außerhalb der statischen Dateien und werden nur über eine berechtigte Ansicht als
Anhang ausgeliefert. Originalnamen sind Metadaten, nie Pfade.
"""

import hashlib
import re
from pathlib import Path

from django.conf import settings

from .dienst import DienstFehler

_BLOB_ID = re.compile(r"^[0-9a-f]{32}$")


def datei_hash(pfad: Path) -> str:
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for stueck in iter(lambda: f.read(1024 * 1024), b""):
            h.update(stueck)
    return h.hexdigest()


def blob_pfad(blob_id: str) -> Path:
    if not _BLOB_ID.match(blob_id or ""):
        raise DienstFehler("Ungültige Datei-ID.")
    return Path(settings.KIPM_ABLAGE_DIR) / "blobs" / blob_id

"""Warnung auf der Startseite, wenn die Kopie der Sicherung ausbleibt (A-15).

Das Skript ``kipm-kopieren`` schreibt ``kopie-stand.json``; der Dienst liest sie nur. Ohne Datei
ist die Kopie nicht eingerichtet, dann gibt es nichts zu melden. Gezählt wird das Alter, nicht der
Fehler: So fällt auch ein Zeitplan auf, der gar nicht mehr läuft; eine einzelne gescheiterte Nacht
meldet sich noch nicht.
"""

import datetime
import json

from django.conf import settings
from django.utils import timezone

GRENZE = datetime.timedelta(days=2)
HILFE = "Protokoll: /var/log/kipm-kopie.log im Container."


def _zeit(wert) -> datetime.datetime | None:
    try:
        zeit = datetime.datetime.fromisoformat(str(wert))
    except ValueError:
        return None
    return zeit if timezone.is_aware(zeit) else None


def kopie_warnung(jetzt: datetime.datetime | None = None) -> str | None:
    """Satz für die Startseite oder ``None``, wenn alles in Ordnung oder nicht eingerichtet ist."""
    try:
        roh = settings.KIPM_KOPIE_STAND.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # ValueError: kein gültiges UTF-8
        return f"Sicherungskopie: Der Stand ist nicht lesbar. {HILFE}"
    try:
        stand = json.loads(roh)
    except ValueError:
        stand = None
    if not isinstance(stand, dict):
        return f"Sicherungskopie: Der Stand ist nicht lesbar. {HILFE}"
    letzte, eingerichtet = _zeit(stand.get("letzte_kopie")), _zeit(stand.get("eingerichtet"))
    bezug = letzte or eingerichtet
    if bezug is None:
        return f"Sicherungskopie: Der Stand ist nicht lesbar. {HILFE}"
    if (jetzt or timezone.now()) - bezug <= GRENZE:
        return None
    was = (
        f"Die letzte Kopie nach SharePoint ist vom {timezone.localtime(letzte):%d.%m.%Y %H:%M}."
        if letzte
        else f"Seit der Einrichtung am {timezone.localtime(eingerichtet):%d.%m.%Y} "
        "gab es noch keine Kopie nach SharePoint."
    )
    fehler = str(stand.get("fehler") or "")[:300].rstrip(". ")
    grund = f" Letzter Fehler: {fehler}." if fehler else ""
    return f"Sicherungskopie: {was}{grund} {HILFE}"

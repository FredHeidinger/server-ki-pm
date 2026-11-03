"""Alle in der Datenbank referenzierten Dateien prüfen: vorhanden und mit passendem SHA-256.

Rückgabe ungleich null, wenn eine Datei fehlt oder abweicht; die Wiederherstellungsprüfung
gilt dann als nicht erfolgreich. ``--manifest`` schreibt Blob-ID und Hash je Zeile.
"""

from django.core.management.base import BaseCommand, CommandError

from steuerung import ablage
from steuerung.models import Unterlagenversion


class Command(BaseCommand):
    help = "Referenzierte Unterlagendateien auf Vorhandensein und Hash prüfen"

    def add_arguments(self, parser):
        parser.add_argument("--manifest", help="Datei für die Liste Blob-ID und SHA-256")

    def handle(self, *args, manifest=None, **optionen):
        zeilen, fehler = [], []
        for v in Unterlagenversion.objects.exclude(blob="").only("blob", "sha256"):
            pfad = ablage.blob_pfad(v.blob)
            if not pfad.exists():
                fehler.append(f"fehlt: {v.blob}")
            elif ablage.datei_hash(pfad) != v.sha256:
                fehler.append(f"Hash weicht ab: {v.blob}")
            zeilen.append(f"{v.sha256}  {v.blob}")
        if manifest:
            with open(manifest, "w", encoding="utf-8") as f:
                f.write("\n".join(zeilen) + ("\n" if zeilen else ""))
        if fehler:
            raise CommandError(f"{len(fehler)} Datei(en) fehlerhaft: " + "; ".join(fehler[:10]))
        self.stdout.write(f"Dateien geprüft: {len(zeilen)}, alle vorhanden und unverändert.")

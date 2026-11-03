"""Unreferenzierte Blobs und alte Quarantänedateien nach einer Schonfrist entfernen.

Referenzierte Dateien bleiben immer. Läuft eine Sicherung (Sperrdatei), passiert nichts.
Ohne ``--ausfuehren`` wird nur angezeigt, was entfernt würde.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from steuerung import ablage
from steuerung.models import Unterlagenversion


class Command(BaseCommand):
    help = "Unreferenzierte Uploaddateien nach Schonfrist bereinigen"

    def add_arguments(self, parser):
        parser.add_argument("--stunden", type=float, default=24)
        parser.add_argument("--ausfuehren", action="store_true")

    def handle(self, *args, stunden=24, ausfuehren=False, **optionen):
        referenziert = set(
            Unterlagenversion.objects.exclude(blob="").values_list("blob", flat=True)
        )
        if not ausfuehren:
            wurzel = settings.KIPM_ABLAGE_DIR
            grenze = time.time() - stunden * 3600
            kandidaten = [
                f"{o}/{d.name}"
                for o in ("quarantaene", "blobs")
                if (wurzel / o).is_dir()
                for d in (wurzel / o).iterdir()
                if d.is_file()
                and d.stat().st_mtime <= grenze
                and not (o == "blobs" and d.name in referenziert)
            ]
            self.stdout.write(f"Würde entfernen: {len(kandidaten)}")
            for k in kandidaten:
                self.stdout.write(f"  {k}")
            return
        entfernt = ablage.bereinigen(referenziert, stunden)
        self.stdout.write(f"Entfernt: {len(entfernt)}")

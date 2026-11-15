"""Altbestand an Werkbank-Lebenszeichen verdichten (KIPM-02, Freds Entscheidung 15.11.2026).

Ohne Zusatz nur Probelauf: zählt, was gelöscht würde, ändert nichts. ``--ausfuehren`` löscht je
Ausführung alle Lebenszeichen außer dem jüngsten samt ihren Operationsnachweisen, schreibt einen
Verlaufseintrag und verkleinert danach die Datei (VACUUM). Vorher ``kipm-sichern`` laufen lassen;
die Rücknahme geht nur über diese Sicherung.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection

from steuerung import dienst, werkbank_lauf

ANLASS = "Freds Entscheidung 15.11.2026: „A, verdichten“."


class Command(BaseCommand):
    help = "Alte Werkbank-Lebenszeichen verdichten (ohne --ausfuehren nur zählen)"

    def add_arguments(self, parser):
        parser.add_argument("--ausfuehren", action="store_true", help="wirklich löschen")

    def handle(self, *args, ausfuehren=False, **optionen):
        akteur = None
        if ausfuehren:
            owner = get_user_model().objects.filter(is_superuser=True, is_active=True).first()
            if owner is None:
                raise CommandError(
                    "Kein aktives Owner-Konto für den Verlaufseintrag; nichts gelöscht."
                )
            akteur = dienst.Akteur.aus_benutzer(owner)
        e = werkbank_lauf.altbestand_lebenszeichen(
            ausfuehren=ausfuehren, akteur=akteur, anlass=ANLASS
        )
        for ausf_id, anzahl in sorted(e["je_ausfuehrung"].items()):
            self.stdout.write(f"Ausführung {ausf_id}: {anzahl} ältere Lebenszeichen")
        if not ausfuehren:
            self.stdout.write(
                f"Probelauf: {e['ereignisse']} Lebenszeichen und {e['operationen']} "
                "Operationsnachweise würden gelöscht. Nichts geändert."
            )
            return
        self.stdout.write(
            f"Gelöscht: {e['ereignisse']} Lebenszeichen und {e['operationen']} Operationsnachweise."
        )
        if e["ereignisse"]:
            try:
                with connection.cursor() as c:
                    c.execute("VACUUM")
                self.stdout.write("Datei verkleinert (VACUUM).")
            except OperationalError as fehler:
                self.stdout.write(f"Verkleinern übersprungen: {fehler}. Später erneut versuchen.")

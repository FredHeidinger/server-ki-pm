"""Owner-Konto anlegen: ``manage.py konto_anlegen fred`` fragt das Passwort ab.

Nicht interaktiv: Passwort über die Umgebungsvariable ``KIPM_PASSWORT`` übergeben.
Das Passwort wird nie ausgegeben oder gespeichert, nur als Hash.
"""

import getpass
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Legt das Owner-Konto an (Mensch mit allen Rechten)."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Anmeldename, z. B. fred")

    def handle(self, *args, **optionen):
        name = optionen["name"].strip()
        Benutzer = get_user_model()
        if Benutzer.objects.filter(username=name).exists():
            raise CommandError(f"Das Konto „{name}“ gibt es schon.")
        passwort = os.environ.get("KIPM_PASSWORT") or getpass.getpass("Passwort: ")
        if not os.environ.get("KIPM_PASSWORT"):
            if passwort != getpass.getpass("Passwort wiederholen: "):
                raise CommandError("Die Passwörter stimmen nicht überein.")
        try:
            validate_password(passwort)
        except ValidationError as fehler:
            raise CommandError(" ".join(fehler.messages)) from fehler
        Benutzer.objects.create_superuser(username=name, password=passwort, email="")
        self.stdout.write(self.style.SUCCESS(f"Owner-Konto „{name}“ angelegt."))

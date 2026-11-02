"""Beim ersten Start: Owner-Konto anlegen, falls noch keines existiert. Sonst nichts tun."""

import os

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Legt beim ersten Start das Owner-Konto an (fragt Name und Passwort ab)."

    def handle(self, *args, **optionen):
        Benutzer = get_user_model()
        if Benutzer.objects.filter(is_superuser=True).exists():
            return
        name = os.environ.get("KIPM_KONTO", "").strip()
        if not name:
            self.stdout.write(
                "Noch kein Konto vorhanden. Bitte Anmeldename und Passwort festlegen."
            )
            name = input("Anmeldename [fred]: ").strip() or "fred"
        call_command("konto_anlegen", name)

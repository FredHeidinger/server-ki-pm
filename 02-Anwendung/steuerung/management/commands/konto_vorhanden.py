"""Exit 0, wenn ein Owner-Konto existiert, sonst Exit 1. Für Installationsskripte."""

import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Prüft, ob ein Owner-Konto existiert (Exit 0) oder nicht (Exit 1)."

    def handle(self, *args, **optionen):
        if get_user_model().objects.filter(is_superuser=True).exists():
            return
        sys.exit(1)

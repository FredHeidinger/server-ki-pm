"""Ein projektbezogenes Prüfrecht in der autorisierten Serververwaltung pflegen."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from steuerung import dienst
from steuerung.models import ChatBindung, Projekt, UebernahmePruefrecht


class Command(BaseCommand):
    help = (
        "Prüfrecht für zwei konkrete Chat-Bindungen und genau ein Projekt erteilen oder entziehen."
    )

    def add_arguments(self, parser):
        parser.add_argument("projekt")
        parser.add_argument("pruefbindung_id", type=int)
        parser.add_argument("zielbindung_id", type=int)
        parser.add_argument("--entziehen", action="store_true")

    @transaction.atomic
    def handle(self, *args, **optionen):
        projekt = Projekt.objects.filter(kennung=optionen["projekt"]).first()
        if projekt is None:
            raise CommandError("Projekt nicht gefunden.")
        quelle_id = optionen["pruefbindung_id"]
        ziel_id = optionen["zielbindung_id"]
        if not all(1 <= wert <= 2**63 - 1 for wert in (quelle_id, ziel_id)):
            raise CommandError("Bindungskennungen müssen positive Ganzzahlen sein.")
        filter_ = {"projekt": projekt, "pruefbindung_id": quelle_id, "zielbindung_id": ziel_id}
        if optionen["entziehen"]:
            UebernahmePruefrecht.objects.filter(**filter_).delete()
            self.stdout.write("Prüfrecht entzogen.")
            return
        if not projekt.aktiv or quelle_id == ziel_id:
            raise CommandError("Zwei unterschiedliche Bindungen eines aktiven Projekts angeben.")
        bindungen = ChatBindung.objects.in_bulk([quelle_id, ziel_id])
        if len(bindungen) != 2 or not all(
            dienst._chatbindung_im_projekt_gilt(bindung, projekt) for bindung in bindungen.values()
        ):
            raise CommandError("Beide Bindungen müssen im angegebenen Projekt gültig sein.")
        UebernahmePruefrecht.objects.get_or_create(**filter_)
        self.stdout.write("Prüfrecht eingerichtet.")

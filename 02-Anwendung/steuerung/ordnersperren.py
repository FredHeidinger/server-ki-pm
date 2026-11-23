"""Zeitlich begrenzte, pfadbezogene Schreibberechtigung für Chat-Aufträge."""

import ntpath
import posixpath
from datetime import timedelta

from django.utils import timezone

from .models import Ordnersperre


def pfadschluessel(arbeitsort):
    pfad = arbeitsort.pfad.strip() if arbeitsort else ""
    if not pfad:
        return ""
    if ntpath.splitdrive(pfad)[0] or "\\" in pfad:
        return "windows:" + ntpath.normcase(ntpath.normpath(pfad))
    return "posix:" + posixpath.normpath(pfad)


def belegung(schluessel, jetzt=None):
    if not schluessel:
        return None
    return (
        Ordnersperre.objects.filter(pfad=schluessel, ablauf__gt=jetzt or timezone.now())
        .select_related("auftrag__projekt")
        .first()
    )


def fremde_belegung(auftrag):
    """Aktive Belegung desselben Arbeitsorts durch einen anderen Auftrag."""
    sperre = belegung(pfadschluessel(auftrag.aktuelle_revision.arbeitsort))
    if sperre is not None and sperre.auftrag_id != auftrag.pk:
        return sperre
    return None


def sichern(auftrag, *, ersetzen=False):
    """Nur innerhalb der Schreibtransaktion. Liefert eine etwaige fremde Belegung."""
    schluessel = pfadschluessel(auftrag.aktuelle_revision.arbeitsort)
    if not schluessel:
        return None
    jetzt = timezone.now()
    vorher = belegung(schluessel, jetzt)
    fremd = vorher if vorher is not None and vorher.auftrag_id != auftrag.pk else None
    if fremd is None or ersetzen:
        Ordnersperre.objects.update_or_create(
            pfad=schluessel, defaults={"auftrag": auftrag, "ablauf": jetzt + timedelta(hours=1)}
        )
    return fremd


def freigeben(auftrag):
    Ordnersperre.objects.filter(auftrag=auftrag).delete()

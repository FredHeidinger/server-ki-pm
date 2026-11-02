"""Kontext für alle Seiten: Projektliste und aktiver Projektfilter aus der Sitzung."""

from django.http import HttpRequest

from .models import Projekt

SITZUNGSSCHLUESSEL = "projektfilter"


def aktives_projekt(request: HttpRequest) -> Projekt | None:
    kennung = request.session.get(SITZUNGSSCHLUESSEL)
    if not kennung:
        return None
    projekt = Projekt.objects.filter(kennung=kennung).first()
    if projekt is None:
        request.session.pop(SITZUNGSSCHLUESSEL, None)
    return projekt


def projektfilter(request: HttpRequest) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {
        "alle_projekte": Projekt.objects.filter(aktiv=True),
        "aktives_projekt": aktives_projekt(request),
    }

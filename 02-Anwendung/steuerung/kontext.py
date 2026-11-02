"""Kontext für alle Seiten: Projekte in der Seitenleiste, aktives Projekt aus der Adresse,
Anzahl der Posteingangseinträge."""

from django.http import HttpRequest

from .models import Projekt


def aktives_projekt(request: HttpRequest) -> Projekt | None:
    teile = request.path.split("/")
    if len(teile) > 2 and teile[1] == "projekt" and teile[2] not in ("", "neu"):
        return Projekt.objects.filter(kennung=teile[2]).first()
    return None


def projektfilter(request: HttpRequest) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from . import dienst

    return {
        "seitenleiste_projekte": Projekt.objects.filter(aktiv=True),
        "aktives_projekt": aktives_projekt(request),
        "posteingang_anzahl": len(dienst.posteingang(request.user)),
    }

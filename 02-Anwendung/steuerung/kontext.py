"""Kontext für alle Seiten: das gewählte Projekt und der Bereich aus der Adresse, dazu die Zahl
neuer Arbeitsordner, die einem Projekt zugeordnet werden müssen.

Das Projekt ergibt sich aus ``/projekt/<kennung>/``, aus einem Auftrag (``/auftraege/<id>/``)
oder aus ``?projekt=`` beim Anlegen. Nie wird ein Projekt geraten.
"""

from django.http import HttpRequest

from .models import Auftrag, OrdnerAnfrage, Projekt

BEREICHE = {
    "": "ueberblick",
    "profil": "ueberblick",
    "kontext": "ueberblick",
    "gesehen": "ueberblick",
    "funktionen": "funktionen",
    "meilensteine": "funktionen",
    "auftraege": "auftraege",
    "pruefung": "auftraege",
    "betrieb": "betrieb",
    "umgebung": "betrieb",
    "github": "betrieb",
    "wissen": "wissen",
    "entscheidungen": "wissen",
    "verlauf": "verlauf",
}


def aktives_projekt(request: HttpRequest) -> tuple[Projekt | None, str]:
    teile = request.path.split("/")
    if len(teile) > 2 and teile[1] == "projekt" and teile[2] not in ("", "neu"):
        bereich = BEREICHE.get(teile[3] if len(teile) > 3 else "", "")
        return Projekt.objects.filter(kennung=teile[2]).first(), bereich
    if len(teile) > 2 and teile[1] == "auftraege" and teile[2].isdigit():
        auftrag = Auftrag.objects.select_related("projekt").filter(pk=int(teile[2])).first()
        return (auftrag.projekt if auftrag else None), "auftraege"
    kennung = request.GET.get("projekt", "")
    if kennung and teile[1] in ("auftraege", "entscheidungen"):
        return Projekt.objects.filter(kennung=kennung).first(), (
            "auftraege" if teile[1] == "auftraege" else "wissen"
        )
    return None, ""


def projektfilter(request: HttpRequest) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    projekt, bereich = aktives_projekt(request)
    return {
        "aktives_projekt": projekt,
        "bereich": bereich,
        "ordneranfragen_anzahl": OrdnerAnfrage.objects.count(),
    }

"""Kontext für alle Seiten: das gewählte Projekt und der Bereich aus der Adresse, dazu die Zahl
neuer Arbeitsordner, die einem Projekt zugeordnet werden müssen.

Das Projekt ergibt sich aus ``/projekt/<kennung>/``, aus einem Auftrag (``/auftraege/<id>/``)
oder aus ``?projekt=`` beim Anlegen. Nie wird ein Projekt geraten.
"""

from django.http import HttpRequest

from .models import Auftrag, OrdnerAnfrage, Projekt

# Vier Bereiche je Projekt; die Projekteinstellungen sind eine Nebenaktion ohne eigenen Bereich.
BEREICHE = {
    "": "ueberblick",
    "profil": "ueberblick",
    "meilensteine": "ueberblick",
    "version": "ueberblick",
    "auftraege": "auftraege",
    "wissen": "wissen",
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
    if kennung and teile[1] == "auftraege":
        return Projekt.objects.filter(kennung=kennung).first(), "auftraege"
    return None, ""


def modulwahl(projekt: Projekt, wert: str) -> tuple[dict | None, dict]:
    """Auswahl im Modulbaum: Filter für Listen (``None`` = Gesamtes Projekt) und Vorlagendaten.
    Ein Modul umfasst seine Untermodule; „ohne“ heißt nur projektweite Einträge."""
    from . import dienst

    baum = dienst.modulbaum(projekt) if projekt.module.exists() else None
    gewaehlt = None
    filter_ = None
    if wert == "ohne":
        filter_ = {"modul__isnull": True}
    elif wert:
        gewaehlt = projekt.module.filter(kennung=wert).first()
        if gewaehlt is not None:
            filter_ = {"modul_id__in": dienst.modul_teilbaum(gewaehlt)}
        else:
            wert = ""
    offen = set(dienst._vorfahren(gewaehlt)) if gewaehlt else set()
    return filter_, {
        "baum": baum,
        "modul": wert,
        "modul_gewaehlt": gewaehlt,
        "baum_offen": offen,
    }


def projektfilter(request: HttpRequest) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    projekt, bereich = aktives_projekt(request)
    return {
        "aktives_projekt": projekt,
        "bereich": bereich,
        "ordneranfragen_anzahl": OrdnerAnfrage.objects.count(),
    }

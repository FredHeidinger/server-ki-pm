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
    return filter_, {
        "baum": baum,
        "modul": wert,
        "modul_gewaehlt": gewaehlt,
        "modul_reihen": _modul_reihen(baum, gewaehlt) if baum else [],
    }


def _modul_eintrag(name: str, kennung: str, zahlen: dict, im_pfad: bool = False) -> dict:
    """Ein Feld der Modulleiste: die Zahl zählt Aufträge und Issues, der Titel sagt es in Worten."""
    teile = [
        f"{zahlen[schluessel]} {einzahl if zahlen[schluessel] == 1 else mehrzahl}"
        for schluessel, einzahl, mehrzahl in (
            ("auftraege", "Auftrag", "Aufträge"),
            ("issues", "Issue", "Issues"),
        )
        if zahlen[schluessel]
    ]
    return {
        "name": name,
        "kennung": kennung,
        "zahl": zahlen["auftraege"] + zahlen["issues"],
        "titel": ", ".join(teile) or "keine Einträge",
        "im_pfad": im_pfad,
    }


def _modul_reihen(baum: dict, gewaehlt) -> list[list[dict]]:
    """Erste Reihe: alle Hauptmodule. Untermodule erscheinen erst, wenn ihr Modul gewählt ist."""
    from . import dienst

    pfad = set(dienst._vorfahren(gewaehlt))

    def felder(knoten):
        return [
            _modul_eintrag(k.modul.name, k.modul.kennung, k.zahlen, k.modul.pk in pfad)
            for k in knoten
        ]

    reihen = [
        [
            _modul_eintrag("Alle Module", "", baum["gesamt"]),
            *felder(baum["knoten"]),
            _modul_eintrag("Ohne Modul", "ohne", baum["projektweit"]),
        ]
    ]
    knoten = baum["knoten"]
    while True:
        offen = next((k for k in knoten if k.modul.pk in pfad and k.kinder), None)
        if offen is None:
            return reihen
        reihen.append(felder(offen.kinder))
        knoten = offen.kinder


def projektfilter(request: HttpRequest) -> dict:
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    projekt, bereich = aktives_projekt(request)
    return {
        "aktives_projekt": projekt,
        "bereich": bereich,
        "ordneranfragen_anzahl": OrdnerAnfrage.objects.count(),
    }

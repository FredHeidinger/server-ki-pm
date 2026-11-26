"""Lesezugang „Projektdaten“ für die Werkbank (KIPM-37, KW-14; Vertrag
``vertrag/kipm-werkbank-projektdaten-1``).

Alles, was KI-PM zum Projekt einer Werkbank-Projektbindung speichert, seitenweise und nur
lesend: kein Kontaktvermerk, keine Zuweisung, keine Ausführung, kein Ereignis. Die Einträge
nutzen dieselben Darstellungen wie die Chat-Schnittstelle (``api``); Geheimnisse entfernt
``akte._bereinigt`` aus jeder Zeichenkette der Seite. Der Stand (Inhaltsrevision und letztes
Ereignis) steht im Seitenzeiger; ändert er sich zwischen zwei Seiten, wird die Folgeseite mit
``STAND_GEAENDERT`` abgelehnt, damit keine gemischte Kopie entsteht.
"""

import base64
import binascii
import json
import re

from . import akte, api, werkbank
from .models import (
    Entscheidung,
    Ereignis,
    Projekt,
    Unterlage,
    WerkbankChatDelegation,
    WerkbankInstanz,
    WerkbankProjektbindung,
)

SCHEMA_VERSION = 1
SEITE_MAX = werkbank.SEITE_MAX
TEILE = (
    "module",
    "arbeitsorte",
    "auftraege",
    "entscheidungen",
    "meilensteine",
    "texte",
    "links",
    "verlauf",
    "erlaubnisse",
)
_TEXTARTEN = (Unterlage.TEXT, Unterlage.DATEI)
_LINKARTEN = (Unterlage.LINK, Unterlage.REPO)
# Zugangsdaten in Adressen (https://name:kennwort@host); Repository-Adressen der Arbeitsorte
# prüft der Dienst beim Anlegen nicht. Wie ein URL-Parser bis zum letzten @ vor Pfad, Abfrage
# oder Anker (ein @ im Kennwort verrät sonst dessen Rest); ohne Wortgrenze davor. Nur die Zeichen,
# die in HTML, JSON oder Markdown eine Adresse umschließen, beenden sie zusätzlich; jedes weitere
# ließe ein Kennwort mit diesem Zeichen ungefiltert hinaus.
_ADRESSE_MIT_ZUGANG = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^\s/?#\"<>\[\]]*@")
_HANDLUNG = "Abruf der Projektdaten nach Vertrag kipm-werkbank-projektdaten/1 korrigieren."


def _ungueltig(text: str) -> werkbank.WerkbankFehler:
    return werkbank.WerkbankFehler(400, "UNGUELTIGE_NACHRICHT", text, _HANDLUNG)


def _nicht_gefunden() -> werkbank.WerkbankFehler:
    return werkbank.WerkbankFehler(
        404,
        "NICHT_GEFUNDEN",
        "Diese Projektbindung gibt es für diese Werkbank nicht.",
        "Eine Projektbindung aus dem Abgleich dieser Werkbank verwenden.",
    )


def _stand_geaendert() -> werkbank.WerkbankFehler:
    return werkbank.WerkbankFehler(
        409,
        "STAND_GEAENDERT",
        "Die Projektdaten haben sich während des Abrufs geändert.",
        "Den Abruf später ohne Seitenzeiger von vorn beginnen.",
    )


def _bindung(instanz: WerkbankInstanz, roh: str | None) -> WerkbankProjektbindung:
    if not roh or not (roh.isascii() and roh.isdigit()):
        raise _ungueltig("projektbindung_id fehlt oder ist keine Zahl.")
    pb = (
        WerkbankProjektbindung.objects.filter(pk=int(roh), instanz=instanz)
        .select_related("projekt")
        .first()
    )
    if pb is None or not werkbank.projekt_erlaubt(instanz, pb.projekt):
        raise _nicht_gefunden()
    return pb


def _stand(p: Projekt) -> dict:
    letztes = Ereignis.objects.filter(projekt=p).order_by("-id").first()
    zeit = letztes.zeitpunkt if letztes else p.angelegt_am
    return {
        "inhalt_revision": p.inhalt_revision,
        "ereignis_bis": letztes.pk if letztes else 0,
        # Zeitpunkt des Stands, nicht der Antwort: auf allen Seiten derselbe.
        "erzeugt": zeit.isoformat(timespec="seconds"),
    }


def _zeiger_lesen(text: str | None) -> tuple[list[int] | None, dict[str, int]]:
    if not text:
        return None, {}
    try:
        daten = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        stand, pos = daten["s"], daten["p"]
    except (ValueError, KeyError, TypeError, binascii.Error) as fehler:
        raise _ungueltig("Der Seitenzeiger „seite“ ist ungültig.") from fehler
    if not (
        isinstance(stand, list)
        and len(stand) == 2
        and all(isinstance(z, int) for z in stand)
        and isinstance(pos, dict)
        and set(pos) <= set(TEILE)
        and all(isinstance(z, int) and z >= 0 for z in pos.values())
    ):
        raise _ungueltig("Der Seitenzeiger „seite“ ist ungültig.")
    return stand, pos


def _zeiger_schreiben(stand: dict, pos: dict[str, int]) -> str:
    roh = json.dumps(
        {"s": [stand["inhalt_revision"], stand["ereignis_bis"]], "p": pos}, separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(roh).decode().rstrip("=")


def _projekt(p: Projekt) -> dict:
    return {
        "kennung": p.kennung,
        "name": p.name,
        "kuerzel": p.kuerzel,
        "beschreibung": p.beschreibung,
        "aktiv": p.aktiv,
        **{feld: getattr(p, feld) for feld in akte.PROFILFELDER},
        "profil_revision": p.profil_revision,
        "angelegt": p.angelegt_am.isoformat(timespec="seconds"),
    }


def _modul(m) -> dict:
    return {"id": m.pk, **api._modul_json(m), "aktiv": m.aktiv}


def _ort(o) -> dict:
    return {"id": o.pk, **api._ort_json(o)}


def _auftrag(a) -> dict:
    daten = {"id": a.pk, **api._auftrag_json(a, mit_details=True)}
    daten["meldungen"] = [
        {
            "id": m.pk,
            "art": m.art,
            "von": m.akteur_name,
            "zeit": m.zeitpunkt.isoformat(timespec="minutes"),
            "text": m.text,
        }
        for m in a.meldungen.all()
    ]
    return daten


def _entscheidung(e: Entscheidung) -> dict:
    return {
        "id": e.pk,
        "titel": akte._entscheidungstitel(e.text) if e.text.strip() else "",
        **api._entscheidung_json(e),
        "verweis": e.verweis,
        "vorgeschlagen_von": e.vorgeschlagen_von_name,
        "vorgeschlagen": e.vorgeschlagen_am.isoformat(timespec="minutes"),
    }


def _meilenstein(m) -> dict:
    return {"id": m.pk, **api._meilenstein_json(akte.meilenstein_gezaehlt(m))}


def _unterlage(u: Unterlage) -> dict:
    v = akte.aktuelle_fassung(u)
    kopf = {"id": u.pk, "titel": u.titel, "version": v.nummer if v else 0}
    if v is None or not akte.ki_lesbar(v):
        return {**kopf, "ki_lesbar": False}
    daten = {
        **kopf,
        "ki_lesbar": True,
        "kennung": u.kennung,
        "art": u.art,
        "beschreibung": u.beschreibung,
        "modul": u.modul.kennung if u.modul_id else None,
        "geaendert": v.zeitpunkt.isoformat(timespec="minutes"),
    }
    if u.art in _LINKARTEN:
        daten.update({"adresse": v.uri, "fixierter_stand": v.quellversion or None})
    else:
        daten["inhalt"] = v.text or v.extrakt
    return daten


def _ereignis(e: Ereignis) -> dict:
    return {
        "id": e.pk,
        "zeit": e.zeitpunkt.isoformat(timespec="seconds"),
        "beobachtet": e.beobachtet_am.isoformat(timespec="seconds") if e.beobachtet_am else None,
        "akteur": e.akteur_name,
        "objekt": f"{e.objekt_typ} {e.objekt_kennung}",
        "art": e.ereignis,
        "text": e.anlass,
        "revision": e.revision,
    }


def _erlaubnis(d: WerkbankChatDelegation) -> dict:
    stand = d.fassungen.get(revision=d.revision)
    return {
        "id": d.pk,
        "zugang": d.bindung.name,
        "revision": d.revision,
        "aktionen": stand.aktionen,
        "ziele": stand.ziele,
        "standard_ziel": stand.standard_ziel,
        "budget": stand.budget,
        "reserviert": d.reserviert,
        "gueltig_ab": stand.gueltig_ab.isoformat(),
        "gueltig_bis": stand.gueltig_bis.isoformat(),
        "widerrufen": d.widerrufen_am is not None,
        "widerrufen_am": d.widerrufen_am.isoformat() if d.widerrufen_am else None,
    }


def _quellen(p: Projekt, ereignis_bis: int):
    """Je Teil die Auswahl (nach ``id`` geordnet) und die Darstellung eines Eintrags."""
    return {
        "module": (p.module.all(), _modul),
        "arbeitsorte": (p.arbeitsorte.all(), _ort),
        "auftraege": (p.auftraege.all(), _auftrag),
        "entscheidungen": (
            Entscheidung.objects.filter(betroffene_projekte=p).distinct(),
            _entscheidung,
        ),
        "meilensteine": (p.meilensteine.all(), _meilenstein),
        "texte": (p.unterlagen.filter(art__in=_TEXTARTEN), _unterlage),
        "links": (p.unterlagen.filter(art__in=_LINKARTEN), _unterlage),
        "verlauf": (Ereignis.objects.filter(projekt=p, pk__lte=ereignis_bis), _ereignis),
        "erlaubnisse": (
            WerkbankChatDelegation.objects.filter(projekt=p).select_related("bindung"),
            _erlaubnis,
        ),
    }


def _bereinigt(wert):
    if isinstance(wert, str):
        # Erst die Adressen: Ein schon ersetztes Token enthält Leerzeichen, dann bliebe der Name.
        return akte._bereinigt(_ADRESSE_MIT_ZUGANG.sub(r"\1[Zugangsdaten entfernt]@", wert))
    if isinstance(wert, list):
        return [_bereinigt(w) for w in wert]
    if isinstance(wert, dict):
        return {k: _bereinigt(v) for k, v in wert.items()}
    return wert


def seite(instanz: WerkbankInstanz, projektbindung_id: str | None, zeiger: str | None, *, vertrag):
    """Eine Seite der Projektdaten; schreibt nichts."""
    pb = _bindung(instanz, projektbindung_id)
    p = Projekt.objects.get(pk=pb.projekt_id)
    stand = _stand(p)
    gebunden, pos = _zeiger_lesen(zeiger)
    if gebunden is not None and gebunden != [stand["inhalt_revision"], stand["ereignis_bis"]]:
        raise _stand_geaendert()
    teile, weiter, neu = {}, False, {}
    for name, (auswahl, darstellung) in _quellen(p, stand["ereignis_bis"]).items():
        eintraege = list(auswahl.filter(pk__gt=pos.get(name, 0)).order_by("pk")[: SEITE_MAX + 1])
        weiter = weiter or len(eintraege) > SEITE_MAX
        eintraege = eintraege[:SEITE_MAX]
        neu[name] = eintraege[-1].pk if eintraege else pos.get(name, 0)
        teile[name] = [darstellung(e) for e in eintraege]
    # Jede Abfrage hat ihren eigenen Lesestand: Hat sich das Projekt geändert, während die Seite
    # entstand, ginge sonst eine gemischte Seite unter dem alten Stand hinaus (Gegenprüfung).
    if _stand(Projekt.objects.get(pk=p.pk)) != stand:
        raise _stand_geaendert()
    return _bereinigt(
        {
            "vertrag": vertrag,
            "art": "projektdaten",
            "schema_version": SCHEMA_VERSION,
            "projektbindung_id": pb.pk,
            "stand": stand,
            "projekt": _projekt(p) if gebunden is None else None,
            "teile": teile,
            "seite": {"naechste": _zeiger_schreiben(stand, neu) if weiter else None},
        }
    )

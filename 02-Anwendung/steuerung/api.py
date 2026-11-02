"""HTTP-Schnittstelle für Projektchats. Einzige Stelle, an der Chats den Dienst erreichen.

- Anmeldung mit ``Authorization: Bearer kipm_…`` (Chat-Bindung); keine Sitzung, kein CSRF.
- Jede verändernde Anfrage trägt ``anfrage_kennung``: gleiche Kennung mit gleichem Inhalt
  liefert die gespeicherte Antwort erneut, mit anderem Inhalt wird sie abgewiesen.
- Antworten sind JSON mit ``ok`` und entweder Daten oder ``fehler``.
"""

import hashlib
import json
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import dienst
from .models import (
    AnfrageKennung,
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Entscheidung,
    Modul,
    Projekt,
)


def _fehler(status: int, text: str) -> JsonResponse:
    return JsonResponse({"ok": False, "fehler": text}, status=status)


def bindung_erforderlich(view):
    @wraps(view)
    def innen(request: HttpRequest, *args, **kwargs):
        kopf = request.headers.get("Authorization", "")
        if not kopf.startswith("Bearer "):
            return _fehler(401, "Kein Zugangsschlüssel.")
        bindung = dienst.bindung_pruefen(kopf[7:])
        if bindung is None:
            return _fehler(401, "Zugangsschlüssel ungültig, abgelaufen oder widerrufen.")
        request.bindung = bindung
        ordner = request.headers.get("X-KIPM-Ordner", "").strip()
        projekt = bindung.projekt
        arbeitsort = None
        kennung = request.headers.get("X-KIPM-Projekt", "").strip()
        if bindung.art == bindung.ART_PC and kennung:
            projekt = Projekt.objects.filter(kennung=kennung, aktiv=True).first()
            if projekt is None:
                return _fehler(404, f"Projekt „{kennung}“ gibt es nicht; kipm_projekte zeigt alle.")
        elif bindung.art == bindung.ART_PC and ordner:
            projekt, arbeitsort = dienst.projekt_fuer_ordner(ordner)
            if projekt is None:
                dienst.ordner_unbekannt_melden(bindung, ordner)
                return _fehler(
                    409,
                    f"Der Ordner „{ordner}“ ist noch keinem Projekt zugeordnet. Server-KI-PM hat "
                    "den Menschen im Posteingang gefragt; nach der Zuordnung erneut versuchen.",
                )
        request.projekt = projekt
        request.arbeitsort = arbeitsort
        ChatBindung.objects.filter(pk=bindung.pk).update(
            letzter_ordner=ordner[:300], letztes_projekt=projekt, letzter_pfad=request.path[:120]
        )
        request.akteur = dienst.Akteur.aus_bindung(bindung, projekt=projekt, ordner=ordner)
        return view(request, *args, **kwargs)

    return innen


def _projekt_noetig(request: HttpRequest) -> Projekt:
    """Projekt der Anfrage; der PC-Zugang nennt es per Kennung oder Arbeitsordner."""
    if request.projekt is None:
        kennungen = ", ".join(Projekt.objects.filter(aktiv=True).values_list("kennung", flat=True))
        raise dienst.DienstFehler(
            "Welches Projekt? „projekt“ (Kennung) oder „ordner“ angeben. "
            f"Vorhanden: {kennungen or 'keine'}."
        )
    return request.projekt


def _json(request: HttpRequest) -> dict:
    if not request.body:
        return {}
    try:
        daten = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as fehler:
        raise dienst.DienstFehler("Anfrage ist kein gültiges JSON.") from fehler
    if not isinstance(daten, dict):
        raise dienst.DienstFehler("Anfrage muss ein JSON-Objekt sein.")
    return daten


def _mit_wiederholungsschutz(view):
    """Verändernde Anfragen: Kennung und Inhalt binden, Wiederholung erkennen."""

    @wraps(view)
    def innen(request: HttpRequest, *args, **kwargs):
        try:
            daten = _json(request)
        except dienst.DienstFehler as fehler:
            return _fehler(400, str(fehler))
        kennung = str(daten.get("anfrage_kennung", "")).strip()
        if not kennung or len(kennung) > 80:
            return _fehler(400, "anfrage_kennung fehlt oder ist zu lang (max. 80 Zeichen).")
        inhalt = {k: v for k, v in daten.items() if k != "anfrage_kennung"}
        inhalt_hash = hashlib.sha256(
            json.dumps({"pfad": request.path, "inhalt": inhalt}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        # Kennung VOR der Ausführung reservieren: eine gleichzeitige Wiederholung trifft auf die
        # Reservierung und wird abgewiesen, statt die Wirkung ein zweites Mal auszulösen.
        try:
            with transaction.atomic():
                reservierung = AnfrageKennung.objects.create(
                    bindung=request.bindung,
                    kennung=kennung,
                    inhalt_hash=inhalt_hash,
                    antwort={},
                    status=0,
                )
        except IntegrityError:
            fruehere = AnfrageKennung.objects.get(bindung=request.bindung, kennung=kennung)
            if fruehere.inhalt_hash != inhalt_hash:
                return _fehler(
                    409, f"anfrage_kennung „{kennung}“ wurde bereits mit anderem Inhalt verwendet."
                )
            if fruehere.status == 0:
                return _fehler(
                    409,
                    f"anfrage_kennung „{kennung}“ wird gerade verarbeitet; später erneut abfragen.",
                )
            antwort = dict(fruehere.antwort)
            antwort["wiederholt"] = True
            return JsonResponse(antwort, status=fruehere.status)
        try:
            antwort = view(request, daten, *args, **kwargs)
        except BaseException:
            AnfrageKennung.objects.filter(pk=reservierung.pk).delete()
            raise
        AnfrageKennung.objects.filter(pk=reservierung.pk).update(
            antwort=json.loads(antwort.content.decode("utf-8")), status=antwort.status_code
        )
        return antwort

    return innen


def _behandeln(fn):
    """DienstFehler in HTTP-Status übersetzen."""

    @wraps(fn)
    def innen(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except dienst.KeinZugriff as fehler:
            return _fehler(404 if str(fehler) == "Nicht gefunden." else 403, str(fehler))
        except dienst.Konflikt as fehler:
            return _fehler(409, str(fehler))
        except dienst.DienstFehler as fehler:
            return _fehler(400, str(fehler))

    return innen


def _auftrag_json(auftrag: Auftrag, mit_details: bool = False) -> dict:
    rev = auftrag.aktuelle_revision
    daten = {
        "nummer": auftrag.nummer,
        "id": auftrag.pk,
        "titel": auftrag.titel,
        "projekt": auftrag.projekt.kennung,
        "modul": str(auftrag.modul.kennung) if auftrag.modul else None,
        "art": auftrag.typ,
        "zustand": auftrag.zustand,
        "prioritaet": auftrag.prioritaet,
        "revision": auftrag.revision_nr,
        "bearbeiter": auftrag.bearbeiter_name or None,
        "uebernehmbar": auftrag.zustand == Auftrag.FREIGEGEBEN and not auftrag.hat_bearbeiter,
    }
    if mit_details:
        daten.update(
            {
                "beschreibung": rev.beschreibung,
                "ziel": rev.ziel,
                "nicht_ziele": rev.nicht_ziele,
                "arbeitsort": (
                    {
                        "kennung": rev.arbeitsort.kennung,
                        "name": rev.arbeitsort.name,
                        "rechner": rev.arbeitsort.rechner,
                        "pfad": rev.arbeitsort.pfad,
                    }
                    if rev.arbeitsort
                    else None
                ),
                "schreibumfang": rev.muster,
                "quellen": rev.quellen,
                "abnahmekriterien": rev.kriterien,
                "vorgaenger": [
                    {"nummer": a.vorgaenger.nummer, "zustand": a.vorgaenger.zustand}
                    for a in auftrag.abhaengigkeiten.select_related("vorgaenger")
                ],
                "pruefer": auftrag.pruefer.get_username() if auftrag.pruefer else None,
                "meldungen": [
                    {
                        "art": m.art,
                        "von": m.akteur_name,
                        "zeit": m.zeitpunkt.isoformat(timespec="minutes"),
                        "text": m.text,
                    }
                    for m in auftrag.meldungen.all()[:20]
                ],
                "letzte_pruefung": _letzte_pruefung(auftrag),
                "fuer_freigabe_fehlt": [
                    p.text for p in dienst.freigabepruefung(auftrag) if not p.erfuellt
                ],
            }
        )
    return daten


def _letzte_pruefung(auftrag: Auftrag) -> dict | None:
    ergebnis = auftrag.ergebnisse.first()
    if ergebnis is None:
        return None
    pruefung = ergebnis.pruefungen.first()
    if pruefung is None:
        return {"ergebnis_revision": ergebnis.revision_nr, "entscheidung": None}
    return {
        "ergebnis_revision": ergebnis.revision_nr,
        "entscheidung": pruefung.entscheidung,
        "begruendung": pruefung.begruendung,
        "kriterien": [
            {"kriterium": b.kriterium, "bewertung": b.bewertung, "anmerkung": b.anmerkung}
            for b in pruefung.bewertungen.all()
        ],
    }


@csrf_exempt
@require_GET
@bindung_erforderlich
@_behandeln
def projekt_lesen(request: HttpRequest) -> JsonResponse:
    """Das Projekt der Anfrage (aus Bindung, Kennung oder Arbeitsordner) mit Modulen und Orten."""
    p = _projekt_noetig(request)
    return JsonResponse(
        {
            "ok": True,
            "projekt": {
                "kennung": p.kennung,
                "name": p.name,
                "beschreibung": p.beschreibung,
                "module": [
                    {"kennung": m.kennung, "name": m.name} for m in p.module.filter(aktiv=True)
                ],
                "arbeitsorte": [
                    {
                        "kennung": o.kennung,
                        "name": o.name,
                        "rechner": o.rechner,
                        "pfad": o.pfad,
                        "repository": o.repository,
                    }
                    for o in p.arbeitsorte.filter(aktiv=True)
                ],
            },
            "bindung": request.bindung.name,
            "zugang": request.bindung.art,
            "arbeitsort": request.arbeitsort.kennung if request.arbeitsort else None,
        }
    )


@csrf_exempt
@require_GET
@bindung_erforderlich
@_behandeln
def meine_auftraege(request: HttpRequest) -> JsonResponse:
    """Alle offenen Aufträge des Projekts; ``?nur_meine=1`` nur übernehmbare und eigene,
    ``?alle=1`` auch abgeschlossene."""
    if request.projekt is None and not request.akteur.ist_assistent:
        raise dienst.KeinZugriff("Nicht gefunden.")
    auftraege = dienst.auftraege_fuer_bindung(
        request.bindung,
        request.projekt,
        nur_meine=request.GET.get("nur_meine") == "1",
        alle=request.GET.get("alle") == "1",
        typ=request.GET.get("art") or None,
    )
    return JsonResponse(
        {
            "ok": True,
            "projekt": request.projekt.kennung if request.projekt else "alle",
            "bindung": request.bindung.name,
            "auftraege": [_auftrag_json(a) for a in auftraege],
        }
    )


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def auftrag_anlegen(request: HttpRequest, daten: dict) -> JsonResponse:
    """Chat legt einen Wunsch als Entwurf an; Freigabe bleibt beim Owner.
    Gleicher Titel wie ein offener Auftrag ergibt 409 mit dessen Nummer, außer ``erzwingen``.
    ``art`` „issue“ legt ein Issue (Fehler oder Problem) an."""
    projekt = _projekt_noetig(request)
    modul = None
    if daten.get("modul"):
        modul = Modul.objects.filter(projekt=projekt, kennung=str(daten["modul"])).first()
        if modul is None:
            raise dienst.DienstFehler(f"Modul „{daten['modul']}“ gibt es in diesem Projekt nicht.")
    prioritaet = str(daten.get("prioritaet") or "mittel")
    if prioritaet not in dict(Auftrag.PRIO):
        raise dienst.DienstFehler("prioritaet muss hoch, mittel oder niedrig sein.")
    auftrag = dienst.wunsch_erfassen(
        request.akteur,
        projekt,
        titel=str(daten.get("titel", "")),
        beschreibung=str(daten.get("beschreibung", "")),
        modul=modul,
        prioritaet=prioritaet,
        erzwingen=bool(daten.get("erzwingen", False)),
        typ=str(daten.get("art") or Auftrag.TYP_AUFTRAG),
    )
    return JsonResponse(
        {"ok": True, "auftrag": _auftrag_json(auftrag, mit_details=True)}, status=201
    )


@csrf_exempt
@require_GET
@bindung_erforderlich
@_behandeln
def auftrag_lesen(request: HttpRequest, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag, mit_details=True)})


def _revision(daten: dict) -> int:
    try:
        return int(daten.get("revision", ""))
    except (TypeError, ValueError) as fehler:
        raise dienst.DienstFehler("revision fehlt oder ist keine Zahl.") from fehler


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def uebernehmen(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    auftrag = dienst.auftrag_uebernehmen(request.akteur, auftrag, _revision(daten))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag, mit_details=True)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def fortschritt(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.fortschritt_melden(request.akteur, auftrag, _revision(daten), str(daten.get("text", "")))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def hindernis(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.hindernis_melden(request.akteur, auftrag, _revision(daten), str(daten.get("text", "")))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def rueckfrage(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.rueckfrage_melden(request.akteur, auftrag, _revision(daten), str(daten.get("text", "")))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def fehler(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.fehler_melden(request.akteur, auftrag, _revision(daten), str(daten.get("text", "")))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def vorschlag(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.vorschlag_einreichen(
        request.akteur, auftrag, _revision(daten), str(daten.get("text", ""))
    )
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def pruefbericht(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.pruefbericht_einreichen(
        request.akteur, auftrag, _revision(daten), str(daten.get("text", ""))
    )
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


def _liste(wert) -> str:
    if isinstance(wert, list):
        return "\n".join(str(z) for z in wert)
    return str(wert or "")


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def ergebnis(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.ergebnis_einreichen(
        request.akteur,
        auftrag,
        _revision(daten),
        zusammenfassung=str(daten.get("zusammenfassung", "")),
        dateien=_liste(daten.get("dateien")),
        pruefungen=_liste(daten.get("pruefungen")),
        luecken=str(daten.get("luecken", "")),
    )
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag)})


def _entscheidung_json(e: Entscheidung) -> dict:
    return {
        "nummer": e.nummer,
        "titel": e.titel,
        "text": e.text,
        "verweis": e.verweis,
        "zustand": e.zustand,
        "stand": e.stand,
        "betroffen": [p.kennung for p in e.betroffene_projekte.all()],
        "bestaetigt": [
            {"projekt": b.projekt.kennung, "stand": b.stand, "von": b.bestaetigt_von_name}
            for b in e.bestaetigungen.select_related("projekt")
        ],
    }


@csrf_exempt
@require_GET
@bindung_erforderlich
@_behandeln
def entscheidungen(request: HttpRequest) -> JsonResponse:
    liste = (
        Entscheidung.objects.filter(betroffene_projekte=_projekt_noetig(request))
        .exclude(zustand=Entscheidung.ZURUECKGEZOGEN)
        .distinct()
    )
    return JsonResponse({"ok": True, "entscheidungen": [_entscheidung_json(e) for e in liste]})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def entscheidung_vorschlagen(request: HttpRequest, daten: dict) -> JsonResponse:
    # Ein Chat schlägt nur für sein eigenes Projekt vor; weitere betroffene Projekte trägt der
    # Owner in der Oberfläche ein. So lassen sich fremde Projektkennungen nicht abfragen.
    e = dienst.entscheidung_vorschlagen(
        request.akteur,
        titel=str(daten.get("titel", "")),
        text=str(daten.get("text", "")),
        verweis=str(daten.get("verweis", "")),
        projekte=[_projekt_noetig(request)],
    )
    return JsonResponse({"ok": True, "entscheidung": _entscheidung_json(e)}, status=201)


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def entscheidung_bestaetigen(
    request: HttpRequest, daten: dict, entscheidung_id: int
) -> JsonResponse:
    projekt = _projekt_noetig(request)
    e = Entscheidung.objects.filter(pk=entscheidung_id).first()
    if e is None or not e.betroffene_projekte.filter(pk=projekt.pk).exists():
        raise dienst.KeinZugriff("Nicht gefunden.")
    dienst.uebernahme_bestaetigen(request.akteur, e, projekt)
    return JsonResponse({"ok": True, "entscheidung": _entscheidung_json(e)})


# --- Assistent (PC-Zugang): Projekte anlegen, Aufträge planen, Rückfragen beantworten --------


def _projekt_json(p: Projekt) -> dict:
    return {
        "kennung": p.kennung,
        "name": p.name,
        "beschreibung": p.beschreibung,
        "module": [{"kennung": m.kennung, "name": m.name} for m in p.module.filter(aktiv=True)],
        "arbeitsorte": [
            {"kennung": o.kennung, "name": o.name, "pfad": o.pfad}
            for o in p.arbeitsorte.filter(aktiv=True)
        ],
        "offene_auftraege": p.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE).count(),
    }


@csrf_exempt
@require_GET
@bindung_erforderlich
@_behandeln
def projekte(request: HttpRequest) -> JsonResponse:
    """Alle Projekte (Assistent) oder das eine Projekt des Projektschlüssels."""
    if request.akteur.ist_assistent:
        liste = list(Projekt.objects.filter(aktiv=True).order_by("name"))
    else:
        liste = [request.projekt]
    return JsonResponse({"ok": True, "projekte": [_projekt_json(p) for p in liste]})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def projekt_anlegen(request: HttpRequest, daten: dict) -> JsonResponse:
    name = str(daten.get("name", ""))
    p = dienst.projekt_registrieren(
        request.akteur,
        kennung=str(daten.get("kennung") or slugify(name)),
        name=name,
        beschreibung=str(daten.get("beschreibung", "")),
        module=dienst.module_aus_text(_liste(daten.get("module"))),
    )
    return JsonResponse({"ok": True, "projekt": _projekt_json(p)}, status=201)


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def modul_anlegen(request: HttpRequest, daten: dict) -> JsonResponse:
    projekt = _projekt_noetig(request)
    name = str(daten.get("name", ""))
    dienst.modul_anlegen(
        request.akteur, projekt, kennung=str(daten.get("kennung") or slugify(name)), name=name
    )
    return JsonResponse({"ok": True, "projekt": _projekt_json(projekt)}, status=201)


PLANFELDER = ("beschreibung", "ziel", "nicht_ziele", "schreibumfang", "quellen", "abnahmekriterien")


def _nach_kennung(modell, projekt: Projekt, wert, was: str):
    if not wert:
        return None
    treffer = modell.objects.filter(projekt=projekt, kennung=str(wert)).first()
    if treffer is None:
        raise dienst.DienstFehler(f"{was} „{wert}“ gibt es in diesem Projekt nicht.")
    return treffer


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def planen(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    """Auftrag zum Arbeitspaket planen oder ändern; nur angegebene Felder ändern sich. Eine
    bestehende Freigabe erlischt dabei. Freigeben und Abnehmen gehen nur in der Oberfläche."""
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    rev = auftrag.aktuelle_revision
    werte = {feld: getattr(rev, feld) for feld in PLANFELDER}
    for feld in PLANFELDER:
        if feld in daten:
            werte[feld] = _liste(daten[feld])
    modul = auftrag.modul
    if "modul" in daten:
        modul = _nach_kennung(Modul, auftrag.projekt, daten["modul"], "Modul")
    arbeitsort = rev.arbeitsort
    if "arbeitsort" in daten:
        arbeitsort = _nach_kennung(Arbeitsort, auftrag.projekt, daten["arbeitsort"], "Arbeitsort")
    prioritaet = str(daten.get("prioritaet") or auftrag.prioritaet)
    if prioritaet not in dict(Auftrag.PRIO):
        raise dienst.DienstFehler("prioritaet muss hoch, mittel oder niedrig sein.")
    if set(daten) - {"pruefer", "anfrage_kennung"}:
        dienst.auftrag_bearbeiten(
            request.akteur,
            auftrag,
            titel=str(daten.get("titel") or auftrag.titel),
            arbeitsort=arbeitsort,
            prioritaet=prioritaet,
            modul=modul,
            anlass=str(daten.get("anlass") or "Geplant durch KI-Assistent"),
            typ=str(daten["art"]) if daten.get("art") else None,
            **werte,
        )
    if daten.get("pruefer"):
        pruefer = get_user_model().objects.filter(username=str(daten["pruefer"])).first()
        if pruefer is None:
            raise dienst.DienstFehler(f"Benutzer „{daten['pruefer']}“ gibt es nicht.")
        dienst.pruefer_festlegen(request.akteur, auftrag, pruefer)
    auftrag.refresh_from_db()
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag, mit_details=True)})


@csrf_exempt
@require_POST
@bindung_erforderlich
@_mit_wiederholungsschutz
@_behandeln
def antworten(request: HttpRequest, daten: dict, auftrag_id: int) -> JsonResponse:
    """Antwort des Owners (über seinen Assistenten) auf Rückfrage, Hindernis oder Fehler."""
    auftrag = dienst.auftrag_holen(request.akteur, auftrag_id)
    dienst.rueckfrage_beantworten(request.akteur, auftrag, str(daten.get("text", "")))
    return JsonResponse({"ok": True, "auftrag": _auftrag_json(auftrag, mit_details=True)})

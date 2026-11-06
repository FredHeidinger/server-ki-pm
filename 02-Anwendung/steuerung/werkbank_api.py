"""HTTP-Schnittstelle der Werkbank (Vertrag kipm-werkbank/1) unter ``/api/werkbank/v1/``.

Eigene Anmeldung nur für die Bindungsart „werkbank“: Bearer im Kopf, kein Ordner, kein
Besuchsvermerk der Chat-Wege. Die Ansichten übersetzen nur; alle Regeln stehen in
``steuerung.werkbank``. Fehler kommen im Vertragsformat (``fehler.schema.json``), nie mit
Geheimnissen oder Stacktraces.
"""

import logging
import re
from functools import wraps

from django.core.exceptions import SuspiciousOperation
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import dienst, werkbank, werkbank_vertrag

logger = logging.getLogger(__name__)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _antwort(daten: dict, status: int = 200) -> JsonResponse:
    antwort = JsonResponse(daten, status=status)
    antwort["Cache-Control"] = "no-store"
    return antwort


def _fehler(fehler: werkbank.WerkbankFehler) -> JsonResponse:
    return _antwort(fehler.koerper(), fehler.status)


def _anmelden(request: HttpRequest):
    kopf = request.headers.get("Authorization", "")
    schluessel = kopf[7:].strip() if kopf.startswith("Bearer ") else ""
    instanz = werkbank.instanz_zu_schluessel(schluessel) if schluessel else None
    if instanz is not None:
        return instanz
    if schluessel and dienst.bindung_finden(schluessel) is not None:
        raise werkbank.WerkbankFehler(
            403,
            "FALSCHE_ZUGANGSART",
            "Dieser Schlüssel gehört einem Chat und gilt nicht für die Werkbank-Schnittstelle.",
            "Für die Werkbank unter „Konto & KI verbinden“ einen eigenen Zugang anlegen.",
        )
    raise werkbank.WerkbankFehler(
        401,
        "NICHT_ANGEMELDET",
        "Der Zugang fehlt, ist ungültig, abgelaufen oder widerrufen.",
        "Zugang der Werkbank in KI-PM prüfen und bei Bedarf einen neuen anlegen.",
    )


def _vertrag_pruefen(request: HttpRequest) -> None:
    erhalten = request.headers.get("X-KIPM-Vertrag", "")
    if erhalten != werkbank_vertrag.VERTRAG:
        raise werkbank.WerkbankFehler(
            426,
            "VERTRAG_INKOMPATIBEL",
            f"Der Server spricht {werkbank_vertrag.VERTRAG}.",
            f"Die Werkbank auf {werkbank_vertrag.VERTRAG} aktualisieren; bis dahin startet nichts.",
            {"erwartet": werkbank_vertrag.VERTRAG, "erhalten": erhalten[:64]},
        )


def _roh(request: HttpRequest) -> bytes:
    try:
        laenge = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        laenge = 0
    if laenge > werkbank.MAX_NACHRICHT_BYTES:
        return b"x" * (werkbank.MAX_NACHRICHT_BYTES + 1)
    try:
        return request.body
    except SuspiciousOperation:  # Django-Grenze für den Körper überschritten
        return b"x" * (werkbank.MAX_NACHRICHT_BYTES + 1)


def werkbank_ansicht(methode: str):
    """Methode, Anmeldung, Vertragskopf, Kontakt und Fehlerübersetzung für jede Ansicht."""

    def dekorator(view):
        @csrf_exempt
        @wraps(view)
        def innen(request: HttpRequest, *args, **kwargs):
            if request.method != methode:
                return _fehler(
                    werkbank.WerkbankFehler(
                        405,
                        "METHODE_NICHT_ERLAUBT",
                        f"Dieser Endpunkt kennt nur {methode}.",
                        f"Mit {methode} aufrufen.",
                    )
                )
            try:
                instanz = _anmelden(request)
                _vertrag_pruefen(request)
                if view.__name__ != "operationen":
                    werkbank.kontakt_setzen(instanz)
                return view(request, instanz, *args, **kwargs)
            except werkbank.WerkbankFehler as fehler:
                return _fehler(fehler)
            except Exception:  # nie Details nach außen; die Ursache steht im Serverprotokoll
                logger.exception("Werkbank-API: unerwarteter Fehler")
                return _fehler(
                    werkbank.WerkbankFehler(
                        500,
                        "INTERNER_FEHLER",
                        "Der Server konnte die Anfrage nicht verarbeiten.",
                        "Später mit derselben operation_id wiederholen; "
                        "GET operationen/<uuid> zeigt, ob etwas gespeichert wurde.",
                    )
                )

        return innen

    return dekorator


@werkbank_ansicht("GET")
def abgleich(request: HttpRequest, instanz) -> JsonResponse:
    return _antwort(
        werkbank.abgleich_seite(instanz, request.GET.get("seite"), request.GET.get("ausfuehrungen"))
    )


@werkbank_ansicht("GET")
def operationen(request: HttpRequest, instanz, operation_id: str) -> JsonResponse:
    if not _UUID.match(operation_id):
        raise werkbank.WerkbankFehler(
            400,
            "UNGUELTIGE_NACHRICHT",
            "Die operation_id ist keine kleingeschriebene UUID.",
            "Die operation_id der gesendeten Nachricht verwenden.",
        )
    return _antwort(werkbank.operation_abfragen(instanz, operation_id))


def _aendern(art: str, request: HttpRequest, instanz) -> JsonResponse:
    nachricht = werkbank.nachricht_pruefen(art, _roh(request))
    ausgang = werkbank.operation_ausfuehren(instanz, art, nachricht)
    return _antwort(ausgang.body, ausgang.status)


@werkbank_ansicht("POST")
def uebernehmen(request: HttpRequest, instanz) -> JsonResponse:
    return _aendern("uebernehmen", request, instanz)


@werkbank_ansicht("POST")
def schritt(request: HttpRequest, instanz) -> JsonResponse:
    return _aendern("schritt", request, instanz)


@werkbank_ansicht("POST")
def ereignisse(request: HttpRequest, instanz) -> JsonResponse:
    return _aendern("ereignisse", request, instanz)

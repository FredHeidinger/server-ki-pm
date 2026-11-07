"""Oberfläche der Werkbank-Anbindung: Verwaltung unter „Konto & KI verbinden“, Ausführungsweg und
Zielwerkbank am Auftrag, Bauplan und Aktionen. Nur Dienstaufrufe (``werkbank``) und Darstellung.

Zustände stehen als Wort da, nicht erlaubte Aktionen sind abgeschaltet mit Grund, alles geht ohne
JavaScript. Zahlen sind gezählte Stände der Werkbank, keine erfundenen Prozentwerte.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import dienst, werkbank
from .models import (
    AUSFUEHRUNG_AKTIV,
    Auftrag,
    Projekt,
    WerkbankAktion,
    WerkbankAusfuehrung,
    WerkbankInstanz,
    WerkbankOperation,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)

ZUSTAND_TEXT = {
    "plant": "plant",
    "laeuft": "läuft",
    "wartet_antwort": "wartet auf deine Antwort",
    "wartet_freigabe": "wartet auf deine Freigabe",
    "fehler": "Fehler, wartet auf dich",
    "unklar": "Ausgang unklar, wartet auf dich",
    "offline": "offline (Netz weg)",
    "halt_angefordert": "Halt angefordert, noch nicht bestätigt",
    "angehalten": "angehalten",
    "abbruch_angefordert": "Abbruch angefordert, noch nicht bestätigt",
    "abgebrochen": "Abbruch bestätigt",
    "abschluss_gemeldet": "Abschluss gemeldet",
    "abgeschlossen": "abgeschlossen",
    "beendet": "beendet (Bearbeitung geändert)",
}
AKTION_TEXT = {
    "ausstehend": "gesendet, noch nicht angewendet",
    "angenommen": "von der Werkbank angenommen",
    "ausgefuehrt": "angewendet",
    "veraltet": "veraltet",
    "fehlgeschlagen": "fehlgeschlagen",
    "unklar": "Ausgang unklar",
}
AKTION_NAME = {
    "antworten": "Antwort",
    "start_freigeben": "Start freigegeben",
    "uebernahme_freigeben": "Übernahme freigegeben",
    "weiterbauen": "Weiterbauen",
    "bearbeitung_beenden": "Bearbeitung ändern",
    "abbrechen": "Abbrechen",
}


def _akteur(request: HttpRequest) -> dienst.Akteur:
    return dienst.Akteur.aus_benutzer(request.user)


def _fehler(request: HttpRequest, fehler: Exception) -> None:
    messages.error(request, str(fehler))


# --- Verwaltung unter „Konto & KI verbinden“ -------------------------------------------------


def verwaltung(request: HttpRequest) -> dict:
    """Kontext für die Werkbank-Verwaltung; der neue Schlüssel steht genau einmal da."""
    neu = request.session.pop("neue_werkbank", None)
    basis = (settings.KIPM_OEFFENTLICHE_URL or f"{request.scheme}://{request.get_host()}/").rstrip(
        "/"
    )
    instanzen = []
    for i in (
        WerkbankInstanz.objects.select_related("bindung")
        .prefetch_related("erlaubte_projekte", "projekte__projekt")
        .order_by("name")
    ):
        i.gesperrt = i.bindung is None or i.bindung.widerrufen_am is not None
        i.loeschbar = not werkbank.instanz_benutzt(i)
        i.keine_antwort = werkbank.nicht_erreichbar(i)
        i.erlaubt_ids = {p.pk for p in i.erlaubte_projekte.all()}
        i.bindungen = list(
            i.projekte.select_related("projekt", "arbeitsort").order_by("projekt__name")
        )
        instanzen.append(i)
    return {
        "werkbaenke": instanzen,
        "neue_werkbank": neu,
        "werkbank_adresse": f"{basis}/api/werkbank/v1/",
        "werkbank_server_id": werkbank.server_id(),
        "werkbank_projekte": Projekt.objects.filter(aktiv=True).order_by("name"),
    }


def _projekte_aus(request: HttpRequest) -> list[Projekt]:
    ids = [int(x) for x in request.POST.getlist("projekt") if x.isdigit()]
    return list(Projekt.objects.filter(pk__in=ids, aktiv=True))


@login_required
@require_POST
def werkbank_anlegen(request: HttpRequest) -> HttpResponse:
    try:
        instanz, schluessel = werkbank.instanz_anlegen(
            _akteur(request),
            name=request.POST.get("name", ""),
            projekte=_projekte_aus(request),
            gueltig_tage=int(request.POST.get("tage") or 90),
        )
        request.session["neue_werkbank"] = {"name": instanz.name, "schluessel": schluessel}
    except (dienst.DienstFehler, ValueError) as fehler:
        _fehler(request, fehler)
    return redirect(reverse("verbindungen") + "#werkbank")


def _instanz(pk: int) -> WerkbankInstanz:
    return get_object_or_404(WerkbankInstanz.objects.select_related("bindung"), pk=pk)


@login_required
@require_POST
def werkbank_pflegen(request: HttpRequest, instanz_id: int, wahl: str) -> HttpResponse:
    instanz = _instanz(instanz_id)
    akteur = _akteur(request)
    try:
        if wahl == "aktiv":
            werkbank.instanz_aktiv_setzen(akteur, instanz, request.POST.get("aktiv") == "1")
        elif wahl == "projekte":
            werkbank.instanz_projekte_setzen(akteur, instanz, _projekte_aus(request))
            messages.success(request, f"Erlaubte Projekte von „{instanz.name}“ gespeichert.")
        elif wahl == "widerrufen":
            werkbank.instanz_widerrufen(akteur, instanz, request.POST.get("grund", ""))
            messages.success(request, f"Der Zugang von „{instanz.name}“ ist widerrufen.")
        elif wahl == "loeschen":
            name = instanz.name
            werkbank.instanz_loeschen(akteur, instanz)
            messages.success(request, f"Werkbank „{name}“ ist gelöscht.")
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler(request, fehler)
    return redirect(reverse("verbindungen") + "#werkbank")


# --- Am Auftrag ------------------------------------------------------------------------------


def auftrag_daten(auftrag: Auftrag) -> dict | None:
    """Alles für den Abschnitt „Ausführung“ am Auftrag, oder None, wenn er nichts zeigen muss."""
    zuweisung = (
        WerkbankZuweisung.objects.select_related("instanz", "projektbindung")
        .filter(auftrag=auftrag)
        .first()
    )
    ziele = list(
        WerkbankProjektbindung.objects.select_related("instanz")
        .filter(
            projekt=auftrag.projekt,
            instanz__aktiv=True,
            instanz__erlaubte_projekte=auftrag.projekt,
        )
        .order_by("instanz__name")
    )
    ausf = auftrag.ausfuehrungen.order_by("-id").first()
    if zuweisung is None and (not ziele or auftrag.ist_endzustand):
        return None
    aktiv = ausf is not None and ausf.zustand in AUSFUEHRUNG_AKTIV
    daten: dict = {
        "zuweisung": zuweisung,
        "ziele": ziele,
        "darf_zuweisen": (
            auftrag.zustand in (Auftrag.ENTWURF, Auftrag.FREIGEGEBEN)
            and not aktiv
            and not auftrag.hat_bearbeiter
            and bool(ziele)
        ),
        "darf_aufheben": zuweisung is not None and not auftrag.hat_bearbeiter and ausf is None,
        "kontext_gebunden": bool(zuweisung and zuweisung.kontext_sha256),
        "warten_auf_werkbank": (
            zuweisung is not None
            and auftrag.zustand == Auftrag.FREIGEGEBEN
            and bool(zuweisung.kontext_sha256)
        ),
        "ausfuehrung": ausf,
        "aktiv": aktiv,
    }
    if zuweisung is not None:
        daten["keine_antwort"] = werkbank.nicht_erreichbar(zuweisung.instanz)
        daten["bereit"] = zuweisung.projektbindung
    if ausf is not None:
        daten.update(_ausfuehrung_daten(ausf))
    return daten


def _ausfuehrung_daten(ausf: WerkbankAusfuehrung) -> dict:
    pakete = [dict(p) for p in ausf.pakete]
    autorisiert = WerkbankOperation.objects.filter(ausfuehrung=ausf, art="schritt")
    for p in pakete:
        p["schritte"] = autorisiert.filter(paket=p["paket"]).count()
    bedarf = [b for b in ausf.bedarf if b.get("offen")]
    aktionen = list(ausf.aktionen.order_by("-id")[:20])
    for a in aktionen:
        a.name_text = AKTION_NAME.get(a.art, a.art)
        a.zustand_text = AKTION_TEXT.get(a.zustand, a.zustand)
    abschluss = next((n for n in reversed(ausf.nachweise) if n.get("typ") == "abschluss"), None)
    haelt = ausf.halt_zweck != ""
    abgeschlossen = ausf.zustand in ("abschluss_gemeldet", "abgeschlossen")
    grund_halt = ""
    if ausf.zustand not in AUSFUEHRUNG_AKTIV:
        grund_halt = "Die Ausführung ist beendet."
    elif abgeschlossen:
        grund_halt = "Der Abschluss ist schon gespeichert."
    elif haelt:
        grund_halt = "Es ist schon ein Halt angefordert."
    return {
        "zustand_text": ZUSTAND_TEXT.get(ausf.zustand, ausf.zustand),
        "pakete": pakete,
        "pakete_erledigt": sum(1 for p in pakete if p.get("zustand") == "erledigt"),
        "schritte_gesamt": autorisiert.count(),
        "bedarf": bedarf,
        "aktionen": aktionen,
        "abschluss": abschluss,
        "halt_grund": grund_halt,
        "halt_moeglich": not grund_halt,
        "kann_weiterbauen": any(b.get("typ") in ("fehler", "unklar") for b in bedarf),
    }


@login_required
@require_POST
def auftrag_zuweisen(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = get_object_or_404(Auftrag, pk=auftrag_id)
    ziel = WerkbankProjektbindung.objects.filter(pk=request.POST.get("ziel") or 0).first()
    akteur = _akteur(request)
    if request.POST.get("wahl") == "aufheben":
        try:
            werkbank.zuweisung_aufheben(akteur, auftrag)
            messages.success(request, f"{auftrag.nummer} gehört wieder keinem Werkbank-Ziel.")
        except dienst.DienstFehler as fehler:
            _fehler(request, fehler)
    elif ziel is None:
        messages.error(request, "Bitte eine Werkbank wählen.")
    else:
        try:
            werkbank.zuweisen(akteur, auftrag, ziel, freigeben=request.POST.get("freigeben") == "1")
            messages.success(
                request,
                f"{auftrag.nummer} ist {ziel.instanz.name} zugewiesen"
                + (" und freigegeben." if request.POST.get("freigeben") == "1" else "."),
            )
        except dienst.DienstFehler as fehler:
            _fehler(request, fehler)
    return redirect(reverse("auftrag_detail", kwargs={"auftrag_id": auftrag.pk}) + "#werkbank")


@login_required
@require_POST
def ausfuehrung_aktion(request: HttpRequest, ausfuehrung_id: int) -> HttpResponse:
    ausf = get_object_or_404(
        WerkbankAusfuehrung.objects.select_related("auftrag"), pk=ausfuehrung_id
    )
    p = request.POST
    try:
        werkbank.aktion_erstellen(
            _akteur(request),
            ausf,
            p.get("art", ""),
            erwartet_zustand=p.get("erwartet") or None,
            frage_id=p.get("frage_id", ""),
            text=p.get("text", ""),
            freigabe_id=p.get("freigabe_id", ""),
            paket=p.get("paket", ""),
        )
        messages.success(request, "Gesendet. Die Werkbank bestätigt, sobald sie es angewendet hat.")
    except dienst.DienstFehler as fehler:
        _fehler(request, fehler)
    return redirect(reverse("auftrag_detail", kwargs={"auftrag_id": ausf.auftrag_id}) + "#werkbank")


__all__ = [
    "AKTION_NAME",
    "WerkbankAktion",
    "auftrag_daten",
    "ausfuehrung_aktion",
    "auftrag_zuweisen",
    "verwaltung",
    "werkbank_anlegen",
    "werkbank_pflegen",
]

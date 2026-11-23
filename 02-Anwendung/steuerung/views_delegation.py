"""W1-Erlaubnisse nur im angemeldeten Ownerkonto verwalten; niemals aus Chattext."""

from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import dienst, werkbank
from .forms_delegation import AKTIONSTEXTE, W1ErlaubnisForm
from .models import WerkbankChatDelegation, WerkbankProjektbindung


def verwaltung(request):
    owner = request.user.is_active and request.user.is_superuser
    result = {"w1_owner": owner}
    if not owner:
        return result
    erlaubnisse = []
    for d in (
        WerkbankChatDelegation.objects.select_related("projekt", "bindung", "aussteller")
        .prefetch_related("fassungen")
        .order_by("-id")
    ):
        stand = next(f for f in d.fassungen.all() if f.revision == d.revision)
        d.stand = stand
        d.aktionstexte = [AKTIONSTEXTE.get(a, a) for a in stand.aktionen]
        d.geld = Decimal(stand.budget["geld_mikro"]) / 1_000_000
        d.geld_reserviert = Decimal(d.reserviert["geld_mikro"]) / 1_000_000
        d.minuten = Decimal(stand.budget["zeit_ms"]) / 60_000
        d.status_text = (
            "widerrufen"
            if d.widerrufen_am
            else "abgelaufen"
            if stand.gueltig_bis <= timezone.now()
            else "gültig"
        )
        if d.status_text == "gültig" and (
            not d.bindung.aktiv
            or not d.projekt.aktiv
            or not d.aussteller.is_active
            or not d.aussteller.is_superuser
        ):
            d.status_text = "gesperrt"
        d.zieltexte = []
        for ziel in stand.ziele:
            pb = (
                WerkbankProjektbindung.objects.select_related("instanz", "arbeitsort")
                .filter(pk=ziel["id"])
                .first()
            )
            text = (
                f"{pb.instanz.name} · {pb.arbeitsort.name}"
                if pb and pb.arbeitsort
                else "Ziel nicht mehr vorhanden"
            )
            if ziel["id"] == stand.standard_ziel:
                text += " (Standardziel)"
            try:
                werkbank.delegation_ziel_pruefen(stand, ziel["id"])
            except dienst.DienstFehler:
                text += " (geändert oder gesperrt, nicht verwendbar)"
            d.zieltexte.append(text)
        erlaubnisse.append(d)
    result.update(
        {
            "w1_erlaubnisse": erlaubnisse,
            "w1_form": getattr(request, "w1_form", None) or W1ErlaubnisForm(prefix="w1"),
        }
    )
    return result


@login_required
@require_POST
def erteilen(request):
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("W1-Erlaubnisse verwaltet nur der angemeldete Owner.")
    form = W1ErlaubnisForm(request.POST, prefix="w1")
    if form.is_valid():
        try:
            werkbank.delegation_erteilen(
                dienst.Akteur.aus_benutzer(request.user), **form.dienst_daten()
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, "W1-Erlaubnis gespeichert. Es wurde kein Lauf gestartet.")
            return redirect(reverse("verbindungen") + "#w1-erlaubnisse")
    request.w1_form = form
    from . import views

    antwort = views.verbindungen(request)
    antwort.status_code = 400
    return antwort


@login_required
@require_POST
def widerrufen(request, delegation_id):
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("W1-Erlaubnisse verwaltet nur der angemeldete Owner.")
    d = get_object_or_404(WerkbankChatDelegation, pk=delegation_id)
    try:
        revision = int(request.POST.get("revision", ""))
        werkbank.delegation_widerrufen(
            dienst.Akteur.aus_benutzer(request.user), d, erwartet_revision=revision
        )
    except (dienst.DienstFehler, ValueError) as fehler:
        messages.error(request, str(fehler))
    else:
        messages.success(
            request,
            "W1-Erlaubnis widerrufen. Neue Schritte sind gesperrt; "
            "laufende Wirkung braucht die lokale Endquittung.",
        )
    return redirect(reverse("verbindungen") + "#w1-erlaubnisse")

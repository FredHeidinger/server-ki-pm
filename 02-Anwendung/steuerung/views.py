"""Oberfläche. Views rufen nur den Dienst und rendern; keine Geschäftsregeln hier."""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import dienst
from .forms import (
    ArbeitsortForm,
    ArbeitspaketForm,
    BindungForm,
    EigenesErgebnisForm,
    EntscheidenForm,
    EntscheidungForm,
    ModulForm,
    ProjektForm,
    WunschForm,
    listen_zusammenfuehren,
)
from .kontext import SITZUNGSSCHLUESSEL, aktives_projekt
from .models import (
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
    Ergebnis,
    Kriteriumsbewertung,
    Meldung,
    Projekt,
)

# --- Anmeldung mit Begrenzung ------------------------------------------------------------

FEHLVERSUCHE_MAX = 8
SPERRE_MINUTEN = 15


class Anmelden(auth_views.LoginView):
    template_name = "steuerung/anmelden.html"
    redirect_authenticated_user = True

    def _schluessel(self) -> str:
        name = (self.request.POST.get("username") or "").strip().lower()[:150]
        adresse = self.request.META.get("REMOTE_ADDR", "?")
        return f"anmeldung:{adresse}:{name}"

    def post(self, request, *args, **kwargs):
        versuche = cache.get(self._schluessel(), 0)
        if versuche >= FEHLVERSUCHE_MAX:
            form = self.get_form()
            form.add_error(
                None,
                f"Zu viele Fehlversuche. Bitte in {SPERRE_MINUTEN} Minuten erneut versuchen.",
            )
            return self.form_invalid(form)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        schluessel = self._schluessel()
        cache.set(schluessel, cache.get(schluessel, 0) + 1, SPERRE_MINUTEN * 60)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._schluessel())
        return super().form_valid(form)


class Abmelden(auth_views.LogoutView):
    pass


def _akteur(request: HttpRequest) -> dienst.Akteur:
    return dienst.Akteur.aus_benutzer(request.user)


def _fehler_anzeigen(request: HttpRequest, fehler: Exception) -> None:
    messages.error(request, str(fehler))


# --- Übersicht und Projekte -------------------------------------------------------------


@login_required
def uebersicht(request: HttpRequest) -> HttpResponse:
    projekt = aktives_projekt(request)
    projekte = Projekt.objects.filter(aktiv=True).prefetch_related("module")
    if projekt is not None:
        projekte = projekte.filter(pk=projekt.pk)
    karten = []
    for p, zeile in zip(projekte, dienst.projektzeilen(projekte, mit_modulen=False), strict=True):
        zeile["beschreibung"] = p.beschreibung
        zeile["module"] = [m.name for m in p.module.all() if m.aktiv]
        karten.append(zeile)
    return render(
        request,
        "steuerung/uebersicht.html",
        {"kacheln": dienst.kacheln(projekt), "projektkarten": karten},
    )


@login_required
@require_POST
def projekt_filter(request: HttpRequest) -> HttpResponse:
    kennung = request.POST.get("projekt", "")
    if kennung and Projekt.objects.filter(kennung=kennung).exists():
        request.session[SITZUNGSSCHLUESSEL] = kennung
    else:
        request.session.pop(SITZUNGSSCHLUESSEL, None)
    ziel = request.POST.get("zurueck") or "/"
    if not ziel.startswith("/") or ziel.startswith("//"):
        ziel = "/"
    return redirect(ziel)


@login_required
def projekt_neu(request: HttpRequest) -> HttpResponse:
    form = ProjektForm(
        listen_zusammenfuehren(request.POST, "module") if request.method == "POST" else None
    )
    if request.method == "POST" and form.is_valid():
        daten = form.cleaned_data
        try:
            module = dienst.module_aus_text(daten["module"])
            projekt = dienst.projekt_registrieren(
                _akteur(request),
                kennung=daten["kennung"] or slugify(daten["name"]),
                name=daten["name"],
                beschreibung=daten["beschreibung"],
                module=module,
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"Projekt „{projekt.name}“ ist registriert.")
            return redirect("projekt_detail", kennung=projekt.kennung)
    return render(request, "steuerung/projekt_neu.html", {"form": form})


@login_required
def projekt_detail(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = get_object_or_404(Projekt, kennung=kennung)
    neuer_schluessel = request.session.pop("neuer_schluessel", None)
    return render(
        request,
        "steuerung/projekt_detail.html",
        {
            "projekt": projekt,
            "module": projekt.module.all(),
            "arbeitsorte": projekt.arbeitsorte.all(),
            "bindungen": projekt.bindungen.all(),
            "spalten": _spalten(projekt.auftraege.select_related("projekt", "modul")),
            "modul_form": ModulForm(prefix="modul"),
            "arbeitsort_form": ArbeitsortForm(prefix="ort"),
            "bindung_form": BindungForm(prefix="bindung"),
            "neuer_schluessel": neuer_schluessel,
            "jetzt": timezone.now(),
        },
    )


@login_required
@require_POST
def modul_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = get_object_or_404(Projekt, kennung=kennung)
    form = ModulForm(request.POST, prefix="modul")
    if form.is_valid():
        try:
            name = form.cleaned_data["name"]
            dienst.modul_anlegen(_akteur(request), projekt, kennung=slugify(name), name=name)
            messages.success(request, "Modul angelegt.")
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(request, "Modul: Der Name ist erforderlich.")
    return redirect("projekt_detail", kennung=kennung)


@login_required
@require_POST
def arbeitsort_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = get_object_or_404(Projekt, kennung=kennung)
    form = ArbeitsortForm(request.POST, prefix="ort")
    if form.is_valid():
        try:
            d = form.cleaned_data
            dienst.arbeitsort_anlegen(_akteur(request), projekt, kennung=slugify(d["name"]), **d)
            messages.success(request, "Arbeitsort angelegt.")
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(request, "Arbeitsort: Der Name ist erforderlich.")
    return redirect("projekt_detail", kennung=kennung)


@login_required
@require_POST
def bindung_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = get_object_or_404(Projekt, kennung=kennung)
    form = BindungForm(request.POST, prefix="bindung")
    if form.is_valid():
        try:
            bindung, schluessel = dienst.bindung_anlegen(
                _akteur(request),
                projekt,
                name=form.cleaned_data["name"],
                gueltig_tage=form.cleaned_data["gueltig_tage"],
            )
            request.session["neuer_schluessel"] = {"name": bindung.name, "schluessel": schluessel}
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(request, "Bindung: Name und Gültigkeit (1 bis 365 Tage) sind erforderlich.")
    return redirect("projekt_detail", kennung=kennung)


@login_required
@require_POST
def bindung_widerrufen(request: HttpRequest, bindung_id: int) -> HttpResponse:
    bindung = get_object_or_404(ChatBindung, pk=bindung_id)
    try:
        dienst.bindung_widerrufen(_akteur(request), bindung, request.POST.get("grund", ""))
        messages.success(request, f"Bindung „{bindung.name}“ ist widerrufen.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("projekt_detail", kennung=bindung.projekt.kennung)


# --- Aufträge --------------------------------------------------------------------------


def _reifegrad(auftrag: Auftrag) -> str:
    rev = auftrag.aktuelle_revision
    if rev.ist_wunsch:
        return "Wunsch – unvollständig"
    if auftrag.zustand == Auftrag.ENTWURF:
        return (
            "Arbeitspaket – freigabefähig"
            if dienst.freigabefaehig(auftrag)
            else "Arbeitspaket – nicht freigabefähig"
        )
    return "Arbeitspaket"


SPALTEN = [
    ("entwurf", "Entwurf", Auftrag.ENTWURF, (Auftrag.ENTWURF,)),
    ("freigegeben", "Freigegeben", Auftrag.FREIGEGEBEN, (Auftrag.FREIGEGEBEN,)),
    (
        "arbeit",
        "In Arbeit",
        Auftrag.IN_ARBEIT,
        (
            Auftrag.IN_ARBEIT,
            Auftrag.NACHARBEIT,
            Auftrag.BLOCKIERT,
            Auftrag.PAUSIERT,
            Auftrag.KLAERUNG_ERFORDERLICH,
        ),
    ),
    ("pruefung", "Zur Prüfung", Auftrag.ZUR_PRUEFUNG, (Auftrag.ZUR_PRUEFUNG,)),
    ("abgenommen", "Abgenommen", Auftrag.ABGENOMMEN, (Auftrag.ABGENOMMEN,)),
]
PRIO_RANG = {"hoch": 0, "mittel": 1, "niedrig": 2}


def _spalten(auftraege) -> list[dict]:
    """Aufträge auf die Board-Spalten verteilen; Abgenommene nur die letzten zehn."""
    spalten = []
    for klasse, titel, haupt, zustaende in SPALTEN:
        karten = [a for a in auftraege if a.zustand in zustaende]
        karten.sort(key=lambda a: (PRIO_RANG[a.prioritaet], -a.pk))
        if haupt == Auftrag.ABGENOMMEN:
            karten = sorted(karten, key=lambda a: -a.pk)[:10]
        spalten.append({"klasse": klasse, "titel": titel, "hauptzustand": haupt, "karten": karten})
    return spalten


@login_required
def auftraege(request: HttpRequest) -> HttpResponse:
    projekt = aktives_projekt(request)
    liste = Auftrag.objects.select_related("projekt", "modul").prefetch_related("revisionen")
    if projekt is not None:
        liste = liste.filter(projekt=projekt)
    ansicht = "liste" if request.GET.get("ansicht") == "liste" else "board"
    if ansicht == "board":
        alle = list(liste)
        return render(
            request,
            "steuerung/auftraege.html",
            {
                "ansicht": ansicht,
                "spalten": _spalten(alle),
                "abgebrochen": sum(1 for a in alle if a.zustand == Auftrag.ABGEBROCHEN),
            },
        )
    zustand = request.GET.get("zustand", "")
    if zustand == "offen":
        liste = liste.exclude(zustand__in=Auftrag.ENDZUSTAENDE)
    elif zustand:
        liste = liste.filter(zustand=zustand)
    bearbeiter = request.GET.get("bearbeiter", "")
    if bearbeiter == "niemand":
        liste = liste.filter(bearbeiter_name="")
    elif bearbeiter:
        liste = liste.filter(bearbeiter_name=bearbeiter)
    reifegrad = request.GET.get("reifegrad", "")
    zeilen = []
    for a in liste:
        stufe = _reifegrad(a)
        if reifegrad == "wunsch" and not stufe.startswith("Wunsch"):
            continue
        if reifegrad == "paket" and stufe.startswith("Wunsch"):
            continue
        zeilen.append({"a": a, "reifegrad": stufe})
    bearbeiter_namen = sorted(
        {
            n
            for n in Auftrag.objects.exclude(bearbeiter_name="").values_list(
                "bearbeiter_name", flat=True
            )
        }
    )
    return render(
        request,
        "steuerung/auftraege.html",
        {
            "ansicht": ansicht,
            "zeilen": zeilen,
            "zustaende": Auftrag.ZUSTAENDE,
            "filter": {"zustand": zustand, "bearbeiter": bearbeiter, "reifegrad": reifegrad},
            "bearbeiter_namen": bearbeiter_namen,
        },
    )


@login_required
def wunsch_neu(request: HttpRequest) -> HttpResponse:
    projekt = aktives_projekt(request)
    if request.GET.get("projekt"):
        projekt = Projekt.objects.filter(kennung=request.GET["projekt"]).first() or projekt
    form = WunschForm(request.POST or None, projekt=projekt)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            auftrag = dienst.wunsch_erfassen(
                _akteur(request),
                d["projekt"],
                titel=d["titel"],
                beschreibung=d["beschreibung"],
                modul=d["modul"],
                prioritaet=d["prioritaet"],
                erzwingen=d["erzwingen"],
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(
                request,
                f"Wunsch {auftrag.nummer} ist erfasst. Zum Arbeitspaket konkretisieren, "
                "bevor jemand daran arbeitet.",
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    return render(request, "steuerung/wunsch_neu.html", {"form": form})


def _auftrag_oder_404(auftrag_id: int) -> Auftrag:
    auftrag = (
        Auftrag.objects.select_related(
            "projekt", "modul", "pruefer", "bearbeiter_bindung", "abgeleitet_von", "entscheidung"
        )
        .filter(pk=auftrag_id)
        .first()
    )
    if auftrag is None:
        raise Http404
    return auftrag


def _aktionen(auftrag: Auftrag, benutzer) -> tuple[list[dict], bool, list]:
    """Welche Aktionen gerade erlaubt sind, und warum nicht. Reihenfolge ist die Anzeige."""
    z = auftrag.zustand
    pruefpunkte = (
        dienst.freigabepruefung(auftrag)
        if z in (Auftrag.ENTWURF, Auftrag.KLAERUNG_ERFORDERLICH)
        else []
    )
    offen = [p.text for p in pruefpunkte if not p.erfuellt]
    aktionen = []

    def a(name, text, erlaubt, grund="", gefahr=False, begruendung=False):
        aktionen.append(
            {
                "name": name,
                "text": text,
                "erlaubt": erlaubt,
                "grund": grund,
                "gefahr": gefahr,
                "begruendung": begruendung,
            }
        )

    bearbeitbar = z in dienst.BEARBEITBARE_ZUSTAENDE
    if z in (Auftrag.ENTWURF, Auftrag.KLAERUNG_ERFORDERLICH):
        a("freigeben", "Freigeben", not offen, "; ".join(offen))
    if z == Auftrag.FREIGEGEBEN:
        a(
            "selbst",
            "Ich bearbeite das selbst",
            auftrag.pruefer_id != benutzer.pk,
            "Du bist der Prüfer; zuerst den Prüfer ändern.",
        )
        a("zuruecknehmen", "Freigabe zurücknehmen", True)
    if z == Auftrag.BLOCKIERT:
        a("hindernis_aufheben", "Hindernis aufgehoben, weiter", True)
    if z in Auftrag.BEARBEITER_ZUSTAENDE:
        a("unklar", "Bearbeitung ist unklar", True, "", True, True)
    if z in (Auftrag.FREIGEGEBEN, Auftrag.IN_ARBEIT, Auftrag.NACHARBEIT, Auftrag.BLOCKIERT):
        a("pausieren", "Pausieren", True)
    if z == Auftrag.PAUSIERT:
        a("fortsetzen", "Fortsetzen", True)
    if not auftrag.ist_endzustand:
        a("abbrechen", "Abbrechen", True, "", True, True)
    return aktionen, bearbeitbar, pruefpunkte


@login_required
def auftrag_detail(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    aktionen, bearbeitbar, pruefpunkte = _aktionen(auftrag, request.user)
    ergebnis = auftrag.ergebnisse.first()
    return render(
        request,
        "steuerung/auftrag_detail.html",
        {
            "auftrag": auftrag,
            "rev": auftrag.aktuelle_revision,
            "reifegrad": _reifegrad(auftrag),
            "pruefpunkte": pruefpunkte,
            "aktionen": aktionen,
            "bearbeitbar": bearbeitbar,
            "vorgaenger": auftrag.abhaengigkeiten.select_related("vorgaenger"),
            "nachfolger": auftrag.nachfolger.select_related("auftrag"),
            "meldungen": auftrag.meldungen.all()[:30],
            "vorschlaege": auftrag.meldungen.filter(
                art=Meldung.VORSCHLAG, vorschlag_status=Meldung.OFFEN
            ),
            "ergebnis": ergebnis,
            "ergebnisse": auftrag.ergebnisse.all(),
            "pruefung": ergebnis.pruefungen.select_related("pruefer").first() if ergebnis else None,
            "ereignisse": Ereignis.objects.filter(
                objekt_typ="Auftrag", objekt_kennung=auftrag.nummer
            )[:50],
            "ist_bearbeiter": auftrag.bearbeiter_benutzer_id == request.user.pk,
            "revisionen": auftrag.revisionen.all(),
            "abgeleitete": auftrag.abgeleitete.all(),
        },
    )


@login_required
def auftrag_bearbeiten(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    rev = auftrag.aktuelle_revision
    anfang = {
        "titel": auftrag.titel,
        "prioritaet": auftrag.prioritaet,
        "modul": auftrag.modul,
        "beschreibung": rev.beschreibung,
        "ziel": rev.ziel,
        "nicht_ziele": rev.nicht_ziele,
        "arbeitsort": rev.arbeitsort,
        "schreibumfang": rev.schreibumfang,
        "quellen": rev.quellen,
        "abnahmekriterien": rev.abnahmekriterien,
        "pruefer": auftrag.pruefer or request.user,
    }
    form = ArbeitspaketForm(
        listen_zusammenfuehren(request.POST, "schreibumfang", "abnahmekriterien")
        if request.method == "POST"
        else None,
        initial=anfang,
        auftrag=auftrag,
        menschen=dienst.menschen(),
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        akteur = _akteur(request)
        try:
            dienst.auftrag_bearbeiten(
                akteur,
                auftrag,
                titel=d["titel"],
                beschreibung=d["beschreibung"],
                ziel=d["ziel"],
                nicht_ziele=d["nicht_ziele"],
                arbeitsort=d["arbeitsort"],
                schreibumfang=d["schreibumfang"],
                quellen=d["quellen"],
                abnahmekriterien=d["abnahmekriterien"],
                prioritaet=d["prioritaet"],
                modul=d["modul"],
                anlass=d["anlass"],
            )
            if d["pruefer"] is not None and d["pruefer"].pk != auftrag.pruefer_id:
                dienst.pruefer_festlegen(akteur, auftrag, d["pruefer"])
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            auftrag.refresh_from_db()
            messages.success(
                request, f"Revision {auftrag.revision_nr} von {auftrag.nummer} ist angelegt."
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    return render(
        request,
        "steuerung/auftrag_bearbeiten.html",
        {"auftrag": auftrag, "form": form, "rev": rev, "folge": _folge_der_bearbeitung(auftrag)},
    )


def _folge_der_bearbeitung(auftrag: Auftrag) -> str:
    if auftrag.zustand == Auftrag.FREIGEGEBEN:
        return "Die Freigabe erlischt; der Auftrag geht zurück in den Entwurf."
    if auftrag.zustand in Auftrag.BEARBEITER_ZUSTAENDE:
        return (
            f"Die Übernahme von {auftrag.bearbeiter_name} erlischt; der Auftrag wird "
            "„Klärung erforderlich“ und muss neu freigegeben und erneut übernommen werden."
        )
    if auftrag.zustand == Auftrag.PAUSIERT:
        return "Die Pause endet; der Auftrag geht in Entwurf oder Klärung."
    return ""


@login_required
@require_POST
def auftrag_aktion(request: HttpRequest, auftrag_id: int, aktion: str) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    akteur = _akteur(request)
    grund = request.POST.get("grund", "")
    try:
        if aktion == "freigeben":
            dienst.auftrag_freigeben(akteur, auftrag)
            messages.success(
                request, f"{auftrag.nummer} ist freigegeben (Revision {auftrag.revision_nr})."
            )
        elif aktion == "zuruecknehmen":
            dienst.freigabe_zuruecknehmen(akteur, auftrag, grund)
            messages.success(request, "Freigabe zurückgenommen.")
        elif aktion == "pausieren":
            dienst.auftrag_pausieren(akteur, auftrag, grund)
            messages.success(request, "Pausiert.")
        elif aktion == "fortsetzen":
            dienst.auftrag_fortsetzen(akteur, auftrag)
            messages.success(request, "Fortgesetzt.")
        elif aktion == "abbrechen":
            dienst.auftrag_abbrechen(akteur, auftrag, grund)
            messages.success(request, "Abgebrochen.")
        elif aktion == "unklar":
            dienst.bearbeitung_unklar(akteur, auftrag, grund)
            messages.success(
                request, "Als „Klärung erforderlich“ markiert; die Übernahme ist erloschen."
            )
        elif aktion == "hindernis_aufheben":
            dienst.hindernis_aufheben(akteur, auftrag, grund)
            messages.success(request, "Hindernis aufgehoben.")
        elif aktion == "selbst":
            dienst.auftrag_uebernehmen(akteur, auftrag, auftrag.revision_nr)
            messages.success(
                request,
                f"Du bearbeitest {auftrag.nummer} jetzt selbst. "
                "Dein Ergebnis prüft ein anderer Mensch.",
            )
        elif aktion == "vorgaenger":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_eintragen(akteur, auftrag, vorgaenger, grund)
            messages.success(request, f"{vorgaenger.nummer} ist als Vorgänger eingetragen.")
        elif aktion == "vorgaenger_entfernen":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_entfernen(akteur, auftrag, vorgaenger)
            messages.success(request, "Vorgänger entfernt.")
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("auftrag_detail", auftrag_id=auftrag.pk)


@login_required
def auftrag_teilen(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    form = WunschForm(request.POST or None, projekt=auftrag.projekt)
    form.fields["projekt"].queryset = Projekt.objects.filter(pk=auftrag.projekt_id)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            neu = dienst.wunsch_erfassen(
                _akteur(request),
                auftrag.projekt,
                titel=d["titel"],
                beschreibung=d["beschreibung"],
                modul=d["modul"],
                prioritaet=d["prioritaet"],
                abgeleitet_von=auftrag,
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"{neu.nummer} ist aus {auftrag.nummer} abgeleitet.")
            return redirect("auftrag_detail", auftrag_id=neu.pk)
    return render(request, "steuerung/auftrag_teilen.html", {"auftrag": auftrag, "form": form})


@login_required
def eigenes_ergebnis(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    form = EigenesErgebnisForm(
        listen_zusammenfuehren(request.POST, "dateien", "pruefungen")
        if request.method == "POST"
        else None
    )
    if request.method == "POST" and form.is_valid():
        try:
            dienst.ergebnis_einreichen(
                _akteur(request), auftrag, auftrag.revision_nr, **form.cleaned_data
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"Ergebnis zu {auftrag.nummer} ist eingereicht.")
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    return render(request, "steuerung/eigenes_ergebnis.html", {"auftrag": auftrag, "form": form})


@login_required
@require_POST
def vorschlag_bewerten(
    request: HttpRequest, auftrag_id: int, meldung_id: int, wahl: str
) -> HttpResponse:
    meldung = get_object_or_404(Meldung, pk=meldung_id, auftrag_id=auftrag_id)
    try:
        dienst.vorschlag_bewerten(_akteur(request), meldung, uebernommen=(wahl == "uebernehmen"))
        if wahl == "uebernehmen":
            messages.success(
                request,
                "Vorschlag als übernommen markiert. Trage die Änderung jetzt über „Bearbeiten“ "
                "in eine neue Revision ein.",
            )
            return redirect("auftrag_bearbeiten", auftrag_id=auftrag_id)
        messages.success(request, "Vorschlag abgelehnt.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("auftrag_detail", auftrag_id=auftrag_id)


# --- Prüfung ---------------------------------------------------------------------------


@login_required
def pruefung(request: HttpRequest) -> HttpResponse:
    projekt = aktives_projekt(request)
    liste = Auftrag.objects.filter(zustand=Auftrag.ZUR_PRUEFUNG).select_related(
        "projekt", "modul", "pruefer"
    )
    if projekt is not None:
        liste = liste.filter(projekt=projekt)
    zeilen = []
    for a in liste:
        e = a.ergebnisse.first()
        if e is not None:
            zeilen.append({"a": a, "e": e, "selbst": e.benutzer_id == request.user.pk})
    zeilen.sort(key=lambda z: z["e"].eingereicht_am)
    return render(request, "steuerung/pruefung.html", {"zeilen": zeilen})


@login_required
def pruefung_detail(request: HttpRequest, ergebnis_id: int) -> HttpResponse:
    ergebnis = get_object_or_404(
        Ergebnis.objects.select_related("auftrag", "auftrag__projekt"), pk=ergebnis_id
    )
    auftrag = ergebnis.auftrag
    rev = auftrag.revisionen.get(nummer=ergebnis.revision_nr)
    kriterien = rev.kriterien
    bisherige = ergebnis.pruefungen.select_related("pruefer").first()
    pruefbar = (
        auftrag.zustand == Auftrag.ZUR_PRUEFUNG
        and bisherige is None
        and auftrag.ergebnisse.first().pk == ergebnis.pk
    )
    selbst = ergebnis.benutzer_id == request.user.pk
    fehler = None
    eingaben = {}
    if request.method == "POST" and pruefbar:
        bewertungen = []
        for i in range(len(kriterien)):
            bewertung = request.POST.get(f"k{i}", "")
            anmerkung = request.POST.get(f"a{i}", "")
            eingaben[i] = (bewertung, anmerkung)
            bewertungen.append((bewertung, anmerkung))
        entscheidung = request.POST.get("entscheidung", "")
        try:
            dienst.ergebnis_pruefen(
                _akteur(request),
                ergebnis,
                bewertungen=bewertungen,
                entscheidung=entscheidung,
                begruendung=request.POST.get("begruendung", ""),
                integration_offen=request.POST.get("integration_offen") == "1",
            )
        except dienst.DienstFehler as f:
            fehler = str(f)
        else:
            auftrag.refresh_from_db()
            messages.success(request, f"{auftrag.nummer}: {auftrag.get_zustand_display()}.")
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    zeilen = [
        {
            "i": i,
            "text": k,
            "bewertung": eingaben.get(i, ("", ""))[0],
            "anmerkung": eingaben.get(i, ("", ""))[1],
        }
        for i, k in enumerate(kriterien)
    ]
    return render(
        request,
        "steuerung/pruefung_detail.html",
        {
            "ergebnis": ergebnis,
            "auftrag": auftrag,
            "rev": rev,
            "kriterien": zeilen,
            "pruefbar": pruefbar,
            "selbst": selbst,
            "fehler": fehler,
            "bisherige": bisherige,
            "pruefberichte": auftrag.meldungen.filter(art=Meldung.PRUEFBERICHT)[:5],
            "bewertungen": Kriteriumsbewertung.BEWERTUNGEN,
        },
    )


# --- Entscheidungen ----------------------------------------------------------------------


@login_required
def entscheidungen(request: HttpRequest) -> HttpResponse:
    projekt = aktives_projekt(request)
    liste = Entscheidung.objects.prefetch_related("betroffene_projekte", "bestaetigungen__projekt")
    if projekt is not None:
        liste = liste.filter(betroffene_projekte=projekt).distinct()
    zeilen = []
    for e in liste:
        bestaetigt = {b.projekt_id for b in e.bestaetigungen.all() if b.stand == e.stand}
        zeilen.append(
            {
                "e": e,
                "projekte": [(p, p.pk in bestaetigt) for p in e.betroffene_projekte.all()],
                "offen": [p for p in e.betroffene_projekte.all() if p.pk not in bestaetigt],
            }
        )
    return render(request, "steuerung/entscheidungen.html", {"zeilen": zeilen})


@login_required
def entscheidung_neu(request: HttpRequest) -> HttpResponse:
    form = EntscheidungForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        akteur = _akteur(request)
        try:
            e = dienst.entscheidung_vorschlagen(
                akteur,
                titel=d["titel"],
                text=d["text"],
                verweis=d["verweis"],
                projekte=list(d["projekte"]),
            )
            if d["sofort_entscheiden"]:
                dienst.entscheidung_treffen(akteur, e)
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"{e.nummer} ist angelegt.")
            return redirect("entscheidung_detail", entscheidung_id=e.pk)
    return render(request, "steuerung/entscheidung_neu.html", {"form": form})


@login_required
def entscheidung_detail(request: HttpRequest, entscheidung_id: int) -> HttpResponse:
    e = get_object_or_404(
        Entscheidung.objects.prefetch_related("betroffene_projekte"), pk=entscheidung_id
    )
    bestaetigungen = {
        b.projekt_id: b for b in e.bestaetigungen.select_related("projekt") if b.stand == e.stand
    }
    projekte = [(p, bestaetigungen.get(p.pk)) for p in e.betroffene_projekte.all()]
    return render(
        request,
        "steuerung/entscheidung_detail.html",
        {
            "e": e,
            "projekte": projekte,
            "auftraege": e.auftraege.select_related("projekt"),
            "entscheiden_form": EntscheidenForm(initial={"text": ""}),
            "ereignisse": Ereignis.objects.filter(
                objekt_typ="Entscheidung", objekt_kennung=e.nummer
            )[:30],
        },
    )


@login_required
@require_POST
def entscheidung_aktion(request: HttpRequest, entscheidung_id: int, aktion: str) -> HttpResponse:
    e = get_object_or_404(Entscheidung, pk=entscheidung_id)
    akteur = _akteur(request)
    try:
        if aktion == "entscheiden":
            dienst.entscheidung_treffen(akteur, e, text=request.POST.get("text", ""))
            messages.success(
                request,
                f"{e.nummer} ist entschieden (Stand {e.stand}). "
                "Betroffene Projekte müssen die Übernahme bestätigen.",
            )
        elif aktion == "zurueckziehen":
            dienst.entscheidung_zurueckziehen(akteur, e, request.POST.get("grund", ""))
            messages.success(request, "Zurückgezogen.")
        elif aktion == "bestaetigen":
            projekt = get_object_or_404(Projekt, pk=request.POST.get("projekt"))
            dienst.uebernahme_bestaetigen(akteur, e, projekt)
            messages.success(request, f"Übernahme für {projekt.name} bestätigt.")
        elif aktion == "umsetzungsauftrag":
            projekt = get_object_or_404(Projekt, pk=request.POST.get("projekt"))
            auftrag = dienst.umsetzungsauftrag_anlegen(akteur, e, projekt)
            messages.success(
                request,
                f"Umsetzungsauftrag {auftrag.nummer} für {projekt.name} ist als Entwurf angelegt.",
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("entscheidung_detail", entscheidung_id=e.pk)


# --- Verlauf -----------------------------------------------------------------------------


@login_required
def verlauf(request: HttpRequest) -> HttpResponse:
    ereignisse = Ereignis.objects.select_related("projekt")
    projekt = aktives_projekt(request)
    if projekt is not None:
        ereignisse = ereignisse.filter(projekt=projekt)
    objekt = request.GET.get("objekt", "")
    if objekt:
        ereignisse = ereignisse.filter(objekt_typ=objekt)
    akteur = request.GET.get("akteur", "")
    if akteur:
        ereignisse = ereignisse.filter(akteur_name=akteur)
    tage = request.GET.get("tage", "")
    if tage.isdigit():
        ereignisse = ereignisse.filter(zeitpunkt__gte=timezone.now() - timedelta(days=int(tage)))
    kennung = request.GET.get("kennung", "").strip()
    if kennung:
        ereignisse = ereignisse.filter(objekt_kennung__iexact=kennung)
    akteure = sorted(set(Ereignis.objects.values_list("akteur_name", flat=True)))
    objekte = sorted(set(Ereignis.objects.values_list("objekt_typ", flat=True)))
    return render(
        request,
        "steuerung/verlauf.html",
        {
            "ereignisse": ereignisse[:300],
            "filter": {"objekt": objekt, "akteur": akteur, "tage": tage, "kennung": kennung},
            "akteure": akteure,
            "objekte": objekte,
        },
    )

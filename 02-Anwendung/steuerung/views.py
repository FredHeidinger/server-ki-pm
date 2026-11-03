"""Oberfläche. Views rufen nur den Dienst und rendern; keine Geschäftsregeln hier.

Aufbau: persönliche Ebene (Posteingang, Meine Aufträge), projektübergreifende Listen, Projekte
als Arbeitsbereiche mit Reitern, Verbindungen als Katalog. Auftragsdetails erscheinen als
Seitenpanel (Fragment) oder als eigene Seite.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import dienst
from .forms import (
    ArbeitsortForm,
    ArbeitspaketForm,
    EigenesErgebnisForm,
    EntscheidenForm,
    EntscheidungForm,
    ModulForm,
    ModulNeuForm,
    ProjektForm,
    WunschForm,
    listen_zusammenfuehren,
)
from .models import (
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
    Ergebnis,
    Kriteriumsbewertung,
    Meldung,
    Modul,
    OrdnerAnfrage,
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


def _zurueck(request: HttpRequest, standard: str) -> HttpResponse:
    """Nach einer Aktion dorthin zurück, wo der Nutzer war (Panel in Liste oder Board)."""
    ziel = request.POST.get("zurueck") or request.GET.get("zurueck") or ""
    if ziel.startswith("/") and not ziel.startswith("//"):
        return redirect(ziel)
    return redirect(standard)


# --- Persönliche Ebene ---------------------------------------------------------------------


@login_required
def posteingang(request: HttpRequest) -> HttpResponse:
    return render(
        request,
        "steuerung/posteingang.html",
        {
            "eintraege": dienst.posteingang(request.user),
            "projekte_vorhanden": Projekt.objects.exists(),
            "projekte": Projekt.objects.filter(aktiv=True).order_by("name"),
        },
    )


@login_required
def meine(request: HttpRequest) -> HttpResponse:
    return render(
        request, "steuerung/meine.html", {"auftraege": dienst.meine_auftraege(request.user)}
    )


# --- Projekte -------------------------------------------------------------------------------


@login_required
def projekte(request: HttpRequest) -> HttpResponse:
    projekte_qs = Projekt.objects.filter(aktiv=True).prefetch_related("module")
    karten = []
    for p, zeile in zip(
        projekte_qs, dienst.projektzeilen(projekte_qs, mit_modulen=False), strict=True
    ):
        zeile["beschreibung"] = p.beschreibung
        zeile["module"] = [m.name for m in p.module.all() if m.aktiv]
        karten.append(zeile)
    return render(request, "steuerung/projekte.html", {"projektkarten": karten})


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


def _projekt(kennung: str) -> Projekt:
    return get_object_or_404(Projekt, kennung=kennung)


def _projekt_kontext(projekt: Projekt, reiter: str) -> dict:
    return {"projekt": projekt, "reiter": reiter}


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


def _reifegrad(auftrag: Auftrag) -> str:
    """Planungsstand eines Entwurfs; nach der Freigabe zählt nur noch der Zustand."""
    if auftrag.zustand != Auftrag.ENTWURF:
        return ""
    if auftrag.aktuelle_revision.ist_wunsch:
        return "noch nicht geplant"
    return "geplant – freigabebereit" if dienst.freigabefaehig(auftrag) else "in Planung"


def _auftragsliste(request: HttpRequest, liste, vorlage: str, kontext: dict) -> HttpResponse:
    """Board oder Liste mit Filtern; gemeinsam für projektübergreifend und je Projekt."""
    ansicht = "liste" if request.GET.get("ansicht") == "liste" else "board"
    kontext["ansicht"] = ansicht
    kontext["zeige_projekt"] = kontext.get("projekt") is None
    art = request.GET.get("art", "")
    if art in dict(Auftrag.TYPEN):
        liste = liste.filter(typ=art)
    kontext["art"] = art
    modul = request.GET.get("modul", "")
    if modul == "ohne":
        liste = liste.filter(modul__isnull=True)
    elif modul:
        liste = liste.filter(modul__kennung=modul)
    kontext["modul"] = modul
    if kontext.get("projekt") is not None:
        kontext["module_filter"] = kontext["projekt"].module.filter(aktiv=True)
    if ansicht == "board":
        alle = list(liste)
        for a in alle:
            a.stand = _reifegrad(a)
        kontext["spalten"] = _spalten(alle)
        kontext["abgebrochen"] = sum(1 for a in alle if a.zustand == Auftrag.ABGEBROCHEN)
        return render(request, vorlage, kontext)
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
        if reifegrad and not stufe.startswith(reifegrad):
            continue
        zeilen.append({"a": a, "reifegrad": stufe})
    kontext.update(
        {
            "zeilen": zeilen,
            "zustaende": Auftrag.ZUSTAENDE,
            "filter": {"zustand": zustand, "bearbeiter": bearbeiter, "reifegrad": reifegrad},
            "bearbeiter_namen": sorted(
                set(
                    Auftrag.objects.exclude(bearbeiter_name="").values_list(
                        "bearbeiter_name", flat=True
                    )
                )
            ),
        }
    )
    return render(request, vorlage, kontext)


@login_required
def auftraege(request: HttpRequest) -> HttpResponse:
    liste = Auftrag.objects.select_related("projekt", "modul").prefetch_related("revisionen")
    return _auftragsliste(request, liste, "steuerung/auftraege.html", {"projekt": None})


@login_required
def projekt_uebersicht(request: HttpRequest, kennung: str) -> HttpResponse:
    """Erster Reiter: Kennzahlen, Module als Karten, Arbeitsorte, verbundene KI, Aktivität."""
    projekt = _projekt(kennung)
    offen = projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE)
    karten, archiv = [], []
    for m in projekt.module.all():
        eigene = offen.filter(modul=m)
        eintrag = {
            "m": m,
            "auftraege": eigene.filter(typ=Auftrag.TYP_AUFTRAG).count(),
            "issues": eigene.filter(typ=Auftrag.TYP_ISSUE).count(),
        }
        (karten if m.aktiv else archiv).append(eintrag)
    kontext = _projekt_kontext(projekt, "uebersicht")
    kontext.update(
        {
            "kennzahlen": {
                "auftraege": offen.filter(typ=Auftrag.TYP_AUFTRAG).count(),
                "issues": offen.filter(typ=Auftrag.TYP_ISSUE).count(),
                "pruefung": offen.filter(zustand=Auftrag.ZUR_PRUEFUNG).count(),
                "blockiert": offen.filter(
                    zustand__in=(Auftrag.BLOCKIERT, Auftrag.KLAERUNG_ERFORDERLICH)
                ).count(),
            },
            "modulkarten": karten,
            "modularchiv": archiv,
            "ohne_modul": offen.filter(modul__isnull=True).count(),
            "arbeitsorte": projekt.arbeitsorte.filter(aktiv=True),
            "ki": ChatBindung.objects.filter(letztes_projekt=projekt).order_by("-zuletzt_aktiv")[
                :5
            ],
            "ereignisse": Ereignis.objects.filter(projekt=projekt).order_by("-zeitpunkt")[:8],
            "modul_form": ModulForm(prefix="modul"),
        }
    )
    return render(request, "steuerung/projekt_uebersicht.html", kontext)


@login_required
def projekt_auftraege(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    liste = projekt.auftraege.select_related("projekt", "modul").prefetch_related("revisionen")
    return _auftragsliste(
        request, liste, "steuerung/projekt_auftraege.html", _projekt_kontext(projekt, "auftraege")
    )


def _pruefzeilen(liste, benutzer) -> list[dict]:
    zeilen = []
    for a in liste:
        e = a.ergebnisse.first()
        if e is not None:
            zeilen.append({"a": a, "e": e, "selbst": e.benutzer_id == benutzer.pk})
    zeilen.sort(key=lambda z: z["e"].eingereicht_am)
    return zeilen


@login_required
def pruefung(request: HttpRequest) -> HttpResponse:
    liste = Auftrag.objects.filter(zustand=Auftrag.ZUR_PRUEFUNG).select_related(
        "projekt", "modul", "pruefer"
    )
    return render(
        request,
        "steuerung/pruefung.html",
        {"zeilen": _pruefzeilen(liste, request.user), "projekt": None},
    )


@login_required
def projekt_pruefung(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    liste = projekt.auftraege.filter(zustand=Auftrag.ZUR_PRUEFUNG).select_related(
        "projekt", "modul", "pruefer"
    )
    kontext = _projekt_kontext(projekt, "pruefung")
    kontext["zeilen"] = _pruefzeilen(liste, request.user)
    return render(request, "steuerung/projekt_pruefung.html", kontext)


def _entscheidungszeilen(liste) -> list[dict]:
    zeilen = []
    for e in liste:
        bestaetigt = {b.projekt_id for b in e.bestaetigungen.all() if b.stand == e.stand}
        zeilen.append(
            {
                "e": e,
                "projekte": [(p, p.pk in bestaetigt) for p in e.betroffene_projekte.all()],
            }
        )
    return zeilen


@login_required
def entscheidungen(request: HttpRequest) -> HttpResponse:
    liste = Entscheidung.objects.prefetch_related("betroffene_projekte", "bestaetigungen__projekt")
    return render(
        request,
        "steuerung/entscheidungen.html",
        {"zeilen": _entscheidungszeilen(liste), "projekt": None},
    )


@login_required
def projekt_entscheidungen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    liste = (
        Entscheidung.objects.filter(betroffene_projekte=projekt)
        .prefetch_related("betroffene_projekte", "bestaetigungen__projekt")
        .distinct()
    )
    kontext = _projekt_kontext(projekt, "entscheidungen")
    kontext["zeilen"] = _entscheidungszeilen(liste)
    return render(request, "steuerung/projekt_entscheidungen.html", kontext)


def _verlauf(request: HttpRequest, ereignisse, vorlage: str, kontext: dict) -> HttpResponse:
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
    kontext.update(
        {
            "ereignisse": ereignisse[:300],
            "filter": {"objekt": objekt, "akteur": akteur, "tage": tage, "kennung": kennung},
            "akteure": sorted(set(Ereignis.objects.values_list("akteur_name", flat=True))),
            "objekte": sorted(set(Ereignis.objects.values_list("objekt_typ", flat=True))),
        }
    )
    return render(request, vorlage, kontext)


@login_required
def verlauf(request: HttpRequest) -> HttpResponse:
    return _verlauf(
        request,
        Ereignis.objects.select_related("projekt"),
        "steuerung/verlauf.html",
        {"projekt": None},
    )


@login_required
def projekt_verlauf(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    return _verlauf(
        request,
        Ereignis.objects.filter(projekt=projekt).select_related("projekt"),
        "steuerung/projekt_verlauf.html",
        _projekt_kontext(projekt, "verlauf"),
    )


@login_required
def projekt_einstellungen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    kontext = _projekt_kontext(projekt, "einstellungen")
    kontext.update(
        {
            "arbeitsorte": projekt.arbeitsorte.all(),
            "bindungen": projekt.bindungen.all(),
            "arbeitsort_form": ArbeitsortForm(prefix="ort"),
            "jetzt": timezone.now(),
        }
    )
    return render(request, "steuerung/projekt_einstellungen.html", kontext)


@login_required
@require_POST
def modul_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = ModulForm(request.POST, prefix="modul")
    if form.is_valid():
        try:
            name = form.cleaned_data["name"]
            dienst.modul_anlegen(_akteur(request), projekt, kennung=slugify(name), name=name)
            messages.success(request, f"Modul „{name}“ angelegt.")
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(request, "Modul: Der Name ist erforderlich.")
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#module")


def _modul(kennung: str, modul: str) -> Modul:
    return get_object_or_404(Modul, projekt__kennung=kennung, kennung=modul)


@login_required
@require_POST
def modul_aendern(request: HttpRequest, kennung: str, modul: str) -> HttpResponse:
    m = _modul(kennung, modul)
    try:
        dienst.modul_aendern(_akteur(request), m, name=request.POST.get("name", ""))
        messages.success(request, f"Modul heißt jetzt „{m.name}“.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#module")


@login_required
@require_POST
def modul_archivieren(request: HttpRequest, kennung: str, modul: str) -> HttpResponse:
    m = _modul(kennung, modul)
    aktiv = request.POST.get("aktiv") == "1"
    try:
        dienst.modul_archivieren(_akteur(request), m, aktiv=aktiv)
        messages.success(
            request, f"Modul „{m.name}“ ist {'wieder aktiv' if aktiv else 'archiviert'}."
        )
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#module")


@login_required
def modul_neu_seite(request: HttpRequest) -> HttpResponse:
    """„+ Neu → Modul“ ohne aktuelles Projekt: Projekt wählen und Modul anlegen."""
    projekt = Projekt.objects.filter(kennung=request.GET.get("projekt", "")).first()
    form = ModulNeuForm(request.POST or None, projekt=projekt)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            dienst.modul_anlegen(
                _akteur(request), d["projekt"], kennung=slugify(d["name"]), name=d["name"]
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"Modul „{d['name']}“ angelegt.")
            return redirect(
                reverse("projekt_detail", kwargs={"kennung": d["projekt"].kennung}) + "#module"
            )
    return render(request, "steuerung/modul_neu.html", {"form": form})


@login_required
@require_POST
def arbeitsort_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = ArbeitsortForm(request.POST, prefix="ort")
    if form.is_valid():
        try:
            d = form.cleaned_data
            dienst.arbeitsort_anlegen(_akteur(request), projekt, kennung=slugify(d["name"]), **d)
            messages.success(request, "Arbeitsort angelegt.")
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(
            request, "Arbeitsort: Der Name ist erforderlich, das Repository muss eine Adresse sein."
        )
    return redirect("projekt_einstellungen", kennung=kennung)


# --- Aufträge: Anlegen, Detail, Aktionen ---------------------------------------------------


@login_required
def wunsch_neu(request: HttpRequest) -> HttpResponse:
    projekt = None
    if request.GET.get("projekt"):
        projekt = Projekt.objects.filter(kennung=request.GET["projekt"]).first()
    art = request.GET.get("art", "")
    form = WunschForm(request.POST or None, projekt=projekt, typ=art)
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
                typ=d["typ"],
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            wort = "Issue" if auftrag.typ == Auftrag.TYP_ISSUE else "Auftrag"
            messages.success(
                request,
                f"{wort} {auftrag.nummer} ist angelegt. Als Nächstes planen: Ziel, Umfang, Abnahme.",
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    titel = "Issue anlegen" if art == Auftrag.TYP_ISSUE else "Auftrag anlegen"
    return render(
        request, "steuerung/wunsch_neu.html", {"form": form, "projekt": projekt, "titel": titel}
    )


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
    aktionen: list[dict] = []

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
        a("hindernis_aufheben", "Blockade aufheben, weiter", True)
    if z in Auftrag.BEARBEITER_ZUSTAENDE:
        a("unklar", "Bearbeitung ist unklar", True, "", True, True)
    if z in (Auftrag.FREIGEGEBEN, Auftrag.IN_ARBEIT, Auftrag.NACHARBEIT, Auftrag.BLOCKIERT):
        a("pausieren", "Pausieren", True)
    if z == Auftrag.PAUSIERT:
        a("fortsetzen", "Fortsetzen", True)
    if not auftrag.ist_endzustand:
        a("abbrechen", "Abbrechen", True, "", True, True)
    return aktionen, bearbeitbar, pruefpunkte


def _auftrag_kontext(request: HttpRequest, auftrag: Auftrag) -> dict:
    aktionen, bearbeitbar, pruefpunkte = _aktionen(auftrag, request.user)
    ergebnis = auftrag.ergebnisse.first()
    rev = auftrag.aktuelle_revision
    pruefbar = (
        ergebnis is not None
        and auftrag.zustand == Auftrag.ZUR_PRUEFUNG
        and ergebnis.benutzer_id != request.user.pk
        and not ergebnis.pruefungen.exists()
    )
    return {
        "auftrag": auftrag,
        "rev": rev,
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
        "offene_blockade": auftrag.meldungen.filter(
            art__in=Meldung.BLOCKIERENDE, historisch=False
        ).first()
        if auftrag.zustand == Auftrag.BLOCKIERT
        else None,
        "ergebnis": ergebnis,
        "ergebnisse": auftrag.ergebnisse.all(),
        "pruefung": ergebnis.pruefungen.select_related("pruefer").first() if ergebnis else None,
        "pruefbar": pruefbar,
        "eigenes_ergebnis": ergebnis is not None and ergebnis.benutzer_id == request.user.pk,
        "kriterien": [{"i": i, "text": k} for i, k in enumerate(rev.kriterien)] if pruefbar else [],
        "bewertungen": Kriteriumsbewertung.BEWERTUNGEN,
        "pruefberichte": auftrag.meldungen.filter(art=Meldung.PRUEFBERICHT)[:5],
        "ereignisse": Ereignis.objects.filter(objekt_typ="Auftrag", objekt_kennung=auftrag.nummer)[
            :50
        ],
        "ist_bearbeiter": auftrag.bearbeiter_benutzer_id == request.user.pk,
        "revisionen": auftrag.revisionen.all(),
        "abgeleitete": auftrag.abgeleitete.all(),
        "zurueck": request.GET.get("zurueck", ""),
    }


@login_required
def auftrag_detail(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    kontext = _auftrag_kontext(request, auftrag)
    kontext["als_panel"] = False
    return render(request, "steuerung/auftrag_detail.html", kontext)


@login_required
def auftrag_panel(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    """Fragment für das Seitenpanel; ohne JavaScript führt der Link auf die volle Seite."""
    auftrag = _auftrag_oder_404(auftrag_id)
    kontext = _auftrag_kontext(request, auftrag)
    kontext["als_panel"] = True
    return render(request, "steuerung/auftrag_inhalt.html", kontext)


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
            messages.success(request, "Blockade aufgehoben.")
        elif aktion == "selbst":
            dienst.auftrag_uebernehmen(akteur, auftrag, auftrag.revision_nr)
            messages.success(
                request,
                f"Du bearbeitest {auftrag.nummer} jetzt selbst. Dein Ergebnis prüft ein anderer Mensch.",
            )
        elif aktion == "vorgaenger":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_eintragen(akteur, auftrag, vorgaenger, grund)
            messages.success(request, f"{vorgaenger.nummer} ist als Vorgänger eingetragen.")
        elif aktion == "vorgaenger_entfernen":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_entfernen(akteur, auftrag, vorgaenger)
            messages.success(request, "Vorgänger entfernt.")
        elif aktion == "pruefen":
            _pruefen(request, auftrag, akteur)
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, f"/auftraege/{auftrag.pk}/")


def _pruefen(request: HttpRequest, auftrag: Auftrag, akteur: dienst.Akteur) -> None:
    ergebnis = auftrag.ergebnisse.first()
    if ergebnis is None:
        raise dienst.DienstFehler("Kein Ergebnis zum Prüfen.")
    kriterien = auftrag.revisionen.get(nummer=ergebnis.revision_nr).kriterien
    bewertungen = [
        (request.POST.get(f"k{i}", ""), request.POST.get(f"a{i}", ""))
        for i in range(len(kriterien))
    ]
    dienst.ergebnis_pruefen(
        akteur,
        ergebnis,
        bewertungen=bewertungen,
        entscheidung=request.POST.get("entscheidung", ""),
        begruendung=request.POST.get("begruendung", ""),
        integration_offen=request.POST.get("integration_offen") == "1",
    )
    auftrag.refresh_from_db()
    messages.success(request, f"{auftrag.nummer}: {auftrag.get_zustand_display()}.")


@login_required
@require_POST
def antwort(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    try:
        dienst.rueckfrage_beantworten(_akteur(request), auftrag, request.POST.get("text", ""))
        messages.success(
            request,
            f"Antwort zu {auftrag.nummer} gespeichert; der Bearbeiter sieht sie beim nächsten Lesen.",
        )
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, f"/auftraege/{auftrag.pk}/")


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
                erzwingen=d["erzwingen"],
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
    return _zurueck(request, f"/auftraege/{auftrag_id}/")


@login_required
def pruefung_detail(request: HttpRequest, ergebnis_id: int) -> HttpResponse:
    """Prüfung eines bestimmten Ergebnisses; die Seite ist der Auftrag mit geöffnetem Prüfteil."""
    ergebnis = get_object_or_404(Ergebnis, pk=ergebnis_id)
    auftrag = _auftrag_oder_404(ergebnis.auftrag_id)
    if request.method == "POST":
        try:
            _pruefen(request, auftrag, _akteur(request))
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
            kontext = _auftrag_kontext(request, auftrag)
            kontext["als_panel"] = False
            return render(request, "steuerung/auftrag_detail.html", kontext)
        return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    return redirect("auftrag_detail", auftrag_id=auftrag.pk)


# --- Entscheidungen ----------------------------------------------------------------------


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
    projekte_liste = [(p, bestaetigungen.get(p.pk)) for p in e.betroffene_projekte.all()]
    return render(
        request,
        "steuerung/entscheidung_detail.html",
        {
            "e": e,
            "projekte": projekte_liste,
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
    return _zurueck(request, f"/entscheidungen/{e.pk}/")


# --- Verbindungen --------------------------------------------------------------------------


# Je Programm: Name in der Zugangsliste, Befehl zum Eintragen (mit {link}), Befehl zum Entfernen
# eines früheren Eintrags und was nach dem Einfügen zu tun ist.
KI_PROGRAMME = {
    "claude": {
        "titel": "Claude",
        "hinweis": "Claude-Desktop-App und Claude Code",
        "name": "Claude Desktop",
        "eintragen": "claude mcp add --transport http --scope user kipm {link}",
        "entfernen": "claude mcp remove kipm --scope user",
        "neustart": "Die Claude-App ganz schließen und wieder öffnen.",
        "frage": "Neuen Chat öffnen und schreiben: „Zeig mir alle Projekte in Server-KI-PM.“",
    },
    "codex": {
        "titel": "ChatGPT",
        "hinweis": "Codex in der ChatGPT-Desktop-App",
        "name": "ChatGPT Desktop (Codex)",
        "eintragen": "codex mcp add kipm --url {link}",
        "entfernen": "codex mcp remove kipm",
        "neustart": "Die ChatGPT-App ganz schließen und wieder öffnen.",
        "frage": "Codex öffnen und schreiben: „Zeig mir alle Projekte in Server-KI-PM.“",
    },
    "anderes": {
        "titel": "Anderes Programm",
        "hinweis": "jedes Programm mit MCP-Unterstützung",
        "name": "Anderes KI-Programm",
        "eintragen": "",
        "entfernen": "",
        "neustart": "",
        "frage": "Im Programm fragen: „Zeig mir alle Projekte in Server-KI-PM.“",
    },
}


def _link_basis(request: HttpRequest) -> str:
    """Adresse, unter der KI-Programme den Server erreichen (öffentlicher Name, sobald es ihn gibt)."""
    basis = settings.KIPM_OEFFENTLICHE_URL or f"{request.scheme}://{request.get_host()}/"
    return basis.rstrip("/") + "/"


def _einrichtungszeile(request: HttpRequest, programm: dict, link: str) -> str:
    """Eine Zeile für das Terminal: Zertifikat vertrauen (solange der Server ein eigenes hat),
    einen früheren Eintrag entfernen und den Link eintragen. Läuft in cmd und PowerShell."""
    teile = []
    if not settings.KIPM_OEFFENTLICHE_URL:
        crt = f"{request.scheme}://{request.get_host()}/static/kipm-root.crt"
        teile.append(
            rf"curl.exe -fsSk -o %TEMP%\kipm-root.crt {crt} && "
            rf"certutil -user -addstore Root %TEMP%\kipm-root.crt >nul & "
        )
    eintragen = programm["eintragen"].format(link=link)
    teile.append(f"({programm['entfernen']} >nul 2>&1 & {eintragen})")
    return f'cmd /c "{"".join(teile)}"'


@login_required
def verbindungen(request: HttpRequest) -> HttpResponse:
    neuer = request.session.pop("neuer_link", None)
    if neuer:
        neuer["programm"] = KI_PROGRAMME.get(neuer["art"], KI_PROGRAMME["anderes"])
    return render(
        request,
        "steuerung/verbindungen.html",
        {
            "programme": KI_PROGRAMME,
            "neuer": neuer,
            "eigenes_zertifikat": not settings.KIPM_OEFFENTLICHE_URL,
            **_zugaenge(ChatBindung.objects.all()),
        },
    )


@login_required
@require_POST
def zugang_neu(request: HttpRequest) -> HttpResponse:
    """Link für das gewählte KI-Programm erzeugen; Zeile und Link werden genau einmal angezeigt."""
    art = request.POST.get("programm", "")
    if art not in KI_PROGRAMME:
        messages.error(request, "Bitte ein Programm wählen.")
        return redirect("verbindungen")
    programm = KI_PROGRAMME[art]
    try:
        bindung, schluessel = dienst.bindung_anlegen(
            _akteur(request),
            None,
            name=programm["name"],
            gueltig_tage=365,
            notiz="geheimer Link im KI-Programm",
        )
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
        return redirect("verbindungen")
    link = f"{_link_basis(request)}mcp/{schluessel}/"
    request.session["neuer_link"] = {
        "art": art,
        "name": bindung.name,
        "link": link,
        "zeile": _einrichtungszeile(request, programm, link) if programm["eintragen"] else "",
    }
    return redirect("verbindungen")


@login_required
@require_POST
def bindung_widerrufen(request: HttpRequest, bindung_id: int) -> HttpResponse:
    bindung = get_object_or_404(ChatBindung, pk=bindung_id)
    try:
        dienst.bindung_widerrufen(_akteur(request), bindung, request.POST.get("grund", ""))
        messages.success(request, f"Zugang „{bindung.name}“ ist widerrufen.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, "/verbindungen/")


def _zugaenge(qs) -> dict:
    """Zugangsliste: gültige oben, widerrufene und abgelaufene im Archiv; Löschbarkeit je Zeile."""
    aktive, archiv = [], []
    for b in qs.select_related("projekt", "letztes_projekt").order_by("-angelegt_am"):
        b.loeschbar = not dienst.bindung_benutzt(b)
        (aktive if b.aktiv else archiv).append(b)
    return {"bindungen": aktive, "archiv": archiv, "jetzt": timezone.now()}


@login_required
@require_POST
def bindung_loeschen(request: HttpRequest, bindung_id: int) -> HttpResponse:
    bindung = get_object_or_404(ChatBindung, pk=bindung_id)
    name = bindung.name
    try:
        dienst.bindung_loeschen(_akteur(request), bindung)
        messages.success(request, f"Zugang „{name}“ ist gelöscht.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, "/verbindungen/")


@login_required
@require_POST
def ordner_zuordnen(request: HttpRequest, anfrage_id: int) -> HttpResponse:
    anfrage = get_object_or_404(OrdnerAnfrage.objects.select_related("bindung"), pk=anfrage_id)
    projekt = Projekt.objects.filter(pk=request.POST.get("projekt") or 0, aktiv=True).first()
    if projekt is None:
        messages.error(request, "Bitte ein Projekt wählen.")
        return redirect("posteingang")
    try:
        ort = dienst.ordner_zuordnen(_akteur(request), anfrage, projekt)
        messages.success(request, f"Ordner „{ort.pfad}“ gehört jetzt zu {projekt.name}.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("posteingang")


@login_required
@require_POST
def ordner_verwerfen(request: HttpRequest, anfrage_id: int) -> HttpResponse:
    anfrage = get_object_or_404(OrdnerAnfrage, pk=anfrage_id)
    try:
        dienst.ordner_verwerfen(_akteur(request), anfrage)
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("posteingang")


@login_required
def zugang_pruefen(request: HttpRequest, bindung_id: int) -> HttpResponse:
    """Verbindung prüfen: letzte Anfrage dieses Zugangs mit Zeit, Ordner und Projekt."""
    bindung = get_object_or_404(
        ChatBindung.objects.select_related("projekt", "letztes_projekt"), pk=bindung_id
    )
    ereignisse = Ereignis.objects.filter(akteur_name=bindung.name)[:10]
    return render(
        request,
        "steuerung/zugang_pruefen.html",
        {"b": bindung, "ereignisse": ereignisse, "jetzt": timezone.now()},
    )

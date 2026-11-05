"""Oberfläche. Views rufen nur den Dienst und rendern; keine Geschäftsregeln hier.

Aufbau: Projektauswahl als Einstieg, je Projekt vier Bereiche (Akte in ``views_akte``),
Verbindungen als technische Verwaltung. Aufträge erscheinen als ganze Seite.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from . import abgleich, akte, dienst, sicherung
from .forms import (
    ArbeitsortForm,
    ArbeitspaketForm,
    EigenesErgebnisForm,
    GitHubSchluesselForm,
    ModulForm,
    ProjektForm,
    WunschForm,
    listen_zusammenfuehren,
)
from .kontext import modulwahl
from .models import (
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
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
    """Nach einer Aktion dorthin zurück, wo der Nutzer war (Überblick oder Auftrag)."""
    ziel = request.POST.get("zurueck") or request.GET.get("zurueck") or ""
    if ziel.startswith("/") and not ziel.startswith("//"):
        return redirect(ziel)
    return redirect(standard)


# --- Projekte -------------------------------------------------------------------------------


@login_required
def projekte(request: HttpRequest) -> HttpResponse:
    """Einstieg: Projekt wählen. Kein Dringlichkeitsboard über alle Projekte."""
    projekte_qs = Projekt.objects.filter(aktiv=True).prefetch_related("module")
    suche = request.GET.get("q", "").strip()
    if suche:
        projekte_qs = projekte_qs.filter(
            Q(name__icontains=suche) | Q(beschreibung__icontains=suche) | Q(zweck__icontains=suche)
        )
    projekte_liste = list(projekte_qs)
    abgleich.versionen_auffrischen(projekte_liste)
    for p in projekte_liste:
        p.offen = p.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE).count()
        p.wartet = len(akte.wartet_liste(p))
        p.version = abgleich.version_anzeige(p)["zustand"]
    return render(
        request,
        "steuerung/projekte.html",
        {
            "projekte": projekte_liste,
            "suche": suche,
            "ordneranfragen": OrdnerAnfrage.objects.count(),
            "kopie_warnung": sicherung.kopie_warnung(),
            "archiviert": Projekt.objects.filter(aktiv=False).count(),
        },
    )


@login_required
def projekt_archiv(request: HttpRequest) -> HttpResponse:
    """Archivierte Projekte; nichts daran ist gelöscht, Zurückholen ist ein Klick."""
    return render(
        request,
        "steuerung/projekt_archiv.html",
        {
            "projekte": Projekt.objects.filter(aktiv=False).order_by("name"),
            "darf_zurueckholen": _akteur(request).ist_owner,
        },
    )


@login_required
@require_POST
def projekt_archivieren(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    aktiv = request.POST.get("aktiv") == "1"
    try:
        dienst.projekt_archivieren(_akteur(request), projekt, aktiv=aktiv)
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
        return redirect(
            reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#archivieren"
        )
    if aktiv:
        messages.success(request, f"„{projekt.name}“ ist wieder aktiv.")
        return redirect("projekt_detail", kennung=kennung)
    messages.success(request, f"„{projekt.name}“ ist archiviert; gelöscht wurde nichts.")
    return redirect("projekte")


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
    ("arbeit", "In Arbeit", Auftrag.IN_ARBEIT, (Auftrag.IN_ARBEIT,)),
    ("blockiert", "Blockiert", Auftrag.BLOCKIERT, (Auftrag.BLOCKIERT,)),
    ("erledigt", "Erledigt", Auftrag.ERLEDIGT, (Auftrag.ERLEDIGT,)),
]
PRIO_RANG = dienst.PRIO_RANG


def _spalten(auftraege) -> list[dict]:
    """Aufträge auf die Board-Spalten verteilen; Erledigte nur die letzten zehn."""
    spalten = []
    for klasse, titel, haupt, zustaende in SPALTEN:
        karten = [a for a in auftraege if a.zustand in zustaende]
        karten.sort(key=lambda a: (PRIO_RANG[a.prioritaet], -a.pk))
        if haupt == Auftrag.ERLEDIGT:
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


def hauptaktion(auftrag: Auftrag, pruefpunkte=None) -> dict:
    """Der eine Knopf, der den Auftrag jetzt weiterbringt; gleich in „Wartet auf dich“, auf der
    Board-Karte und im Auftragskopf. Ohne möglichen Knopf ein Satz: warum und was zu tun ist
    (``kurz`` für die Karte)."""
    z = auftrag.zustand
    if z == Auftrag.BLOCKIERT:
        frage = auftrag.meldungen.filter(art__in=Meldung.BLOCKIERENDE, historisch=False).first()
        return {"art": "antworten", "text": "Antworten", "frage": frage}
    if z == Auftrag.ENTWURF:
        if auftrag.aktuelle_revision.ist_wunsch:
            satz = (
                "Noch nicht geplant: Sag einem Chat „Plane "
                f"{auftrag.nummer}“, dann kannst du ihn hier freigeben."
            )
            return {"art": "hinweis", "satz": satz, "kurz": "noch nicht geplant"}
        if pruefpunkte is None:
            pruefpunkte = dienst.freigabepruefung(auftrag)
        offen = [p for p in pruefpunkte if not p.erfuellt]
        if not offen:
            return {"art": "freigeben", "text": "Freigeben"}
        if all(p.arbeitsort_wahl for p in offen):
            satz = "Freigeben geht erst mit Arbeitsort: Arbeitsort unten wählen, dann freigeben."
            return {"art": "hinweis", "satz": satz, "kurz": "Arbeitsort fehlt", "ort": True}
        gruende = ", ".join(p.text for p in offen)
        satz = f"Freigeben noch nicht möglich ({gruende}). Was fehlt, steht unten unter „Freigabe“."
        return {"art": "hinweis", "satz": satz, "kurz": "Freigabe noch nicht möglich"}
    termin = "Der Termin ist da. Verschiebe oder entferne ihn, sonst wartet er weiter auf dich."
    bei = auftrag.bearbeiter_name or "einem Chat"
    still = dienst.verstummt(auftrag)
    if still:
        zeit = timezone.localtime(still["seit"]).strftime("%d.%m. %H:%M")
        vorn = (
            "Der Chat ist beendet, ein Ergebnis fehlt."
            if still["beendet"]
            else f"Seit {zeit} keine Meldung; ob der Chat noch läuft, weiß KI-PM nicht."
        )
        satz = (
            f"{vorn} Soll es weitergehen, sag einem neuen Chat in {bei}: "
            f"„Mach bei {auftrag.nummer} weiter.“"
        )
        if dienst.ist_faellig(auftrag):
            satz = f"{satz} {termin}"
        kurz = "Chat beendet" if still["beendet"] else "keine Meldung"
        return {"art": "hinweis", "satz": satz, "kurz": kurz}
    if dienst.ist_faellig(auftrag):
        # Vor „nichts zu tun“: Ein fälliger Eintrag bleibt in „Wartet auf dich“.
        return {"art": "hinweis", "satz": termin, "kurz": ""}
    if z == Auftrag.FREIGEGEBEN:
        satz = "Freigegeben: Ein Chat übernimmt ihn mit „weiter“. Für dich ist nichts zu tun."
    elif z == Auftrag.IN_ARBEIT:
        satz = f"In Arbeit bei {bei}. Für dich ist nichts zu tun."
    elif z == Auftrag.ERLEDIGT:
        satz = "Erledigt. Passt etwas nicht, lege über „+ Neu“ ein Issue an."
    else:
        satz = "Abgebrochen. Für dich ist nichts zu tun."
    return {"art": "hinweis", "satz": satz, "kurz": ""}


def wartet_eintraege(projekt: Projekt) -> list[dict]:
    """Einträge für „Wartet auf dich“; Aufträge mit ihrem Hauptknopf."""
    return [
        {"e": x}
        if isinstance(x, Entscheidung)
        else {
            "a": x,
            "h": hauptaktion(x),
            "faellig": dienst.ist_faellig(x),
            "still": dienst.verstummt(x),
        }
        for x in akte.wartet_liste(projekt)
    ]


def _auftragsliste(request: HttpRequest, liste, vorlage: str, kontext: dict) -> HttpResponse:
    """Board oder Liste mit Filtern."""
    ansicht = "liste" if request.GET.get("ansicht") == "liste" else "board"
    kontext["ansicht"] = ansicht
    art = request.GET.get("art", "")
    if art in dict(Auftrag.TYPEN):
        liste = liste.filter(typ=art)
    kontext["art"] = art
    filter_, auswahl = modulwahl(kontext["projekt"], request.GET.get("modul", ""))
    if filter_:
        liste = liste.filter(**filter_)
    kontext.update(auswahl)
    if ansicht == "board":
        alle = list(liste)
        for a in alle:
            a.haupt = hauptaktion(a)
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
def projekt_auftraege(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    liste = projekt.auftraege.select_related("projekt", "modul").prefetch_related("revisionen")
    kontext = _projekt_kontext(projekt, "auftraege")
    return _auftragsliste(request, liste, "steuerung/projekt_auftraege.html", kontext)


def _verlauf(request: HttpRequest, ereignisse, vorlage: str, kontext: dict) -> HttpResponse:
    """Neuestes oben (Modellreihenfolge), wann · wer · was, ein Filter nach Art."""
    arten = sorted(set(ereignisse.values_list("objekt_typ", flat=True)))
    art = request.GET.get("art", "")
    if art in arten:
        ereignisse = ereignisse.filter(objekt_typ=art)
    else:
        art = ""
    kontext.update({"ereignisse": ereignisse[:300], "art": art, "arten": arten})
    return render(request, vorlage, kontext)


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
@require_POST
def modul_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = ModulForm(request.POST, prefix="modul")
    if form.is_valid():
        try:
            name = form.cleaned_data["name"]
            eltern = None
            if request.POST.get("eltern"):
                eltern = _modul(kennung, request.POST["eltern"])
            dienst.modul_anlegen(
                _akteur(request),
                projekt,
                kennung=dienst.freie_kennung(Modul, projekt, name, "modul"),
                name=name,
                eltern=eltern,
            )
            wort = f"Untermodul von „{eltern.name}“" if eltern else "Modul"
            messages.success(request, f"{wort} „{name}“ angelegt.")
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
    else:
        messages.error(request, "Modul: Der Name ist erforderlich.")
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#module")


def _modul(kennung: str, modul: str) -> Modul:
    return get_object_or_404(Modul, projekt__kennung=kennung, kennung=modul)


@login_required
@require_POST
def modul_aendern(request: HttpRequest, kennung: str, modul: str) -> HttpResponse:
    m = _modul(kennung, modul)
    try:
        felder = {"name": request.POST.get("name", "")}
        if "eltern" in request.POST:
            wert = request.POST["eltern"]
            felder["eltern"] = _modul(kennung, wert) if wert else None
        m = dienst.modul_aendern(
            _akteur(request), m, erwartete_revision=request.POST.get("revision") or None, **felder
        )
        messages.success(request, f"Modul „{m.name}“ gespeichert.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#module")


@login_required
@require_POST
def modul_archivieren(request: HttpRequest, kennung: str, modul: str) -> HttpResponse:
    m = _modul(kennung, modul)
    aktiv = request.POST.get("aktiv") == "1"
    try:
        dienst.modul_archivieren(
            _akteur(request),
            m,
            aktiv=aktiv,
            erwartete_revision=request.POST.get("revision") or None,
        )
        messages.success(
            request, f"Modul „{m.name}“ ist {'wieder aktiv' if aktiv else 'archiviert'}."
        )
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#module")


@login_required
@require_POST
def arbeitsort_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = ArbeitsortForm(request.POST, prefix="ort", projekt=projekt)
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


@login_required
@require_POST
def arbeitsort_pflegen(request: HttpRequest, kennung: str, pk: int, wahl: str) -> HttpResponse:
    """Arbeitsort in den Projekteinstellungen: Vorschlag übernehmen, ändern, archivieren."""
    ort = get_object_or_404(Arbeitsort, projekt__kennung=kennung, pk=pk)
    akteur = _akteur(request)
    try:
        if wahl == "bestaetigen":
            dienst.arbeitsort_bestaetigen(
                akteur, ort, erwartete_revision=request.POST.get("revision") or None
            )
            messages.success(request, f"Arbeitsort „{ort.name}“ übernommen.")
        elif wahl == "aendern":
            form = ArbeitsortForm(request.POST, prefix=f"ort{ort.pk}", projekt=ort.projekt)
            if not form.is_valid():
                raise dienst.DienstFehler(
                    "Arbeitsort: Der Name ist erforderlich, das Repository muss eine Adresse sein."
                )
            dienst.arbeitsort_aendern(
                akteur,
                ort,
                erwartete_revision=request.POST.get("revision") or None,
                **form.cleaned_data,
            )
            messages.success(request, "Arbeitsort gespeichert.")
        elif wahl in ("archivieren", "zurueckholen"):
            dienst.arbeitsort_aendern(
                akteur, ort, erwartete_revision=None, aktiv=wahl == "zurueckholen"
            )
            messages.success(
                request,
                f"Arbeitsort „{ort.name}“ ist {'wieder aktiv' if wahl == 'zurueckholen' else 'archiviert'}.",
            )
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#arbeitsorte")


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
                faellig=d["faellig"],
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            wort = "Issue" if auftrag.typ == Auftrag.TYP_ISSUE else "Auftrag"
            messages.success(
                request,
                f"{wort} {auftrag.nummer} ist angelegt. Als Nächstes planen: Ziel, Umfang, Kriterien.",
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    titel = "Issue anlegen" if art == Auftrag.TYP_ISSUE else "Auftrag anlegen"
    return render(
        request, "steuerung/wunsch_neu.html", {"form": form, "projekt": projekt, "titel": titel}
    )


def _auftrag_oder_404(auftrag_id: int) -> Auftrag:
    auftrag = (
        Auftrag.objects.select_related(
            "projekt",
            "modul",
            "meilenstein",
            "bearbeiter_bindung",
        )
        .filter(pk=auftrag_id)
        .first()
    )
    if auftrag is None:
        raise Http404
    return auftrag


def _aktionen(auftrag: Auftrag) -> tuple[list[dict], bool, list]:
    """Welche Aktionen gerade erlaubt sind, und warum nicht. Reihenfolge ist die Anzeige."""
    z = auftrag.zustand
    pruefpunkte = dienst.freigabepruefung(auftrag) if z == Auftrag.ENTWURF else []
    offen = [p.text for p in pruefpunkte if not p.erfuellt]
    aktionen: list[dict] = []

    def a(name, text, erlaubt=True, grund="", gefahr=False, begruendung=False):
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
    if z == Auftrag.ENTWURF:
        a("freigeben", "Freigeben", not offen, "; ".join(offen))
    if z == Auftrag.FREIGEGEBEN:
        a("selbst", "Ich bearbeite das selbst")
        a("zuruecknehmen", "Freigabe zurücknehmen")
    if z in Auftrag.BEARBEITER_ZUSTAENDE:
        a("unklar", "Bearbeitung ist unklar", gefahr=True, begruendung=True)
    if not auftrag.ist_endzustand:
        a("abbrechen", "Abbrechen", gefahr=True, begruendung=True)
    return aktionen, bearbeitbar, pruefpunkte


def _auftrag_kontext(request: HttpRequest, auftrag: Auftrag) -> dict:
    """Auftrag als ganze Seite: Kopf mit dem einen Hauptknopf, dann Auftrag, Ergebnis, Verlauf;
    Bearbeiten und Abbrechen unter „Weitere Aktionen“."""
    aktionen, bearbeitbar, pruefpunkte = _aktionen(auftrag)
    ergebnis = auftrag.ergebnisse.first()
    return {
        "auftrag": auftrag,
        "rev": auftrag.aktuelle_revision,
        "haupt": hauptaktion(auftrag, pruefpunkte),
        "pruefpunkte": pruefpunkte,
        "pruef_offen": [p for p in pruefpunkte if not p.erfuellt],
        "bearbeitbar": bearbeitbar,
        "weitere_aktionen": [a for a in aktionen if a["name"] != "freigeben"],
        "vorgaenger": auftrag.abhaengigkeiten.select_related("vorgaenger"),
        "nachfolger": auftrag.nachfolger.select_related("auftrag"),
        "meldungen": auftrag.meldungen.all()[:30],
        "ergebnis": ergebnis,
        "ergebnisse": auftrag.ergebnisse.all(),
        "ereignisse": Ereignis.objects.filter(
            projekt=auftrag.projekt, objekt_typ="Auftrag", objekt_kennung=auftrag.nummer
        )[:50],
        "ist_bearbeiter": auftrag.bearbeiter_benutzer_id == request.user.pk,
        "revisionen": auftrag.revisionen.all(),
        "zurueck": request.GET.get("zurueck", ""),
        "arbeitsorte": auftrag.projekt.arbeitsorte.filter(aktiv=True, bestaetigt=True),
        "arbeitsort_form": ArbeitsortForm(prefix="ort"),
    }


@login_required
def auftrag_detail(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    return render(request, "steuerung/auftrag_detail.html", _auftrag_kontext(request, auftrag))


@login_required
def auftrag_bearbeiten(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    rev = auftrag.aktuelle_revision
    anfang = {
        "titel": auftrag.titel,
        "prioritaet": auftrag.prioritaet,
        "modul": auftrag.modul,
        "meilenstein": auftrag.meilenstein,
        "beschreibung": rev.beschreibung,
        "ziel": rev.ziel,
        "nicht_ziele": rev.nicht_ziele,
        "arbeitsort": rev.arbeitsort,
        "schreibumfang": rev.schreibumfang,
        "quellen": rev.quellen,
        "abnahmekriterien": rev.abnahmekriterien,
    }
    form = ArbeitspaketForm(
        listen_zusammenfuehren(request.POST, "schreibumfang", "abnahmekriterien")
        if request.method == "POST"
        else None,
        initial=anfang,
        auftrag=auftrag,
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
            if "meilenstein" in d:
                dienst.meilenstein_zuordnen(akteur, auftrag, d["meilenstein"])
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
            f"Die Übernahme von {auftrag.bearbeiter_name} erlischt; der Auftrag geht zurück in "
            "den Entwurf und muss neu freigegeben und erneut übernommen werden."
        )
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
        elif aktion == "abbrechen":
            dienst.auftrag_abbrechen(akteur, auftrag, grund)
            messages.success(request, "Abgebrochen.")
        elif aktion == "unklar":
            dienst.bearbeitung_unklar(akteur, auftrag, grund)
            messages.success(request, "Die Übernahme ist beendet; der Auftrag ist wieder Entwurf.")
        elif aktion == "selbst":
            dienst.auftrag_uebernehmen(akteur, auftrag, auftrag.revision_nr)
            messages.success(
                request,
                f"Du bearbeitest {auftrag.nummer} jetzt selbst. Mit deinem Ergebnis ist er erledigt.",
            )
        elif aktion == "vorgaenger":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_eintragen(akteur, auftrag, vorgaenger, grund)
            messages.success(request, f"{vorgaenger.nummer} ist als Vorgänger eingetragen.")
        elif aktion == "vorgaenger_entfernen":
            vorgaenger = get_object_or_404(Auftrag, pk=request.POST.get("vorgaenger"))
            dienst.vorgaenger_entfernen(akteur, auftrag, vorgaenger)
            messages.success(request, "Vorgänger entfernt.")
        elif aktion == "faellig":
            entfernen = request.POST.get("wahl") == "entfernen"
            wert = "" if entfernen else request.POST.get("faellig", "")
            dienst.faellig_setzen(akteur, auftrag, wert)
            messages.success(
                request, f"Termin von {auftrag.nummer} {'gespeichert' if wert else 'entfernt'}."
            )
        else:
            raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, f"/auftraege/{auftrag.pk}/")


@login_required
@require_POST
def auftrag_arbeitsort(request: HttpRequest, auftrag_id: int, wahl: str) -> HttpResponse:
    """Arbeitsort direkt aus der Freigabeprüfung: vorhandenen setzen, neuen anlegen und setzen,
    den Vorschlag einer KI übernehmen, ändern oder verwerfen."""
    auftrag = _auftrag_oder_404(auftrag_id)
    akteur = _akteur(request)
    orte = auftrag.projekt.arbeitsorte
    try:
        if wahl == "setzen":
            ort = get_object_or_404(
                orte, pk=request.POST.get("arbeitsort") or 0, aktiv=True, bestaetigt=True
            )
            dienst.arbeitsort_setzen(akteur, auftrag, ort)
            messages.success(request, f"Arbeitsort „{ort.name}“ gesetzt.")
        elif wahl == "neu":
            form = ArbeitsortForm(request.POST, prefix="ort")
            if not form.is_valid():
                raise dienst.DienstFehler(
                    "Arbeitsort: Der Name ist erforderlich, das Repository muss eine Adresse sein."
                )
            d = form.cleaned_data
            kennung = dienst.freie_kennung(Arbeitsort, auftrag.projekt, d["name"], "ort")
            ort = dienst.arbeitsort_anlegen(akteur, auftrag.projekt, kennung=kennung, **d)
            dienst.arbeitsort_setzen(akteur, auftrag, ort)
            messages.success(request, f"Arbeitsort „{ort.name}“ angelegt und gesetzt.")
        else:
            ort = get_object_or_404(orte, pk=request.POST.get("arbeitsort") or 0)
            if wahl == "bestaetigen":
                dienst.arbeitsort_bestaetigen(
                    akteur, ort, erwartete_revision=request.POST.get("revision") or None
                )
                messages.success(request, f"Vorschlag „{ort.name}“ übernommen.")
            elif wahl == "aendern":
                form = ArbeitsortForm(request.POST, prefix="ort")
                if not form.is_valid():
                    raise dienst.DienstFehler(
                        "Arbeitsort: Der Name ist erforderlich, das Repository muss eine Adresse sein."
                    )
                dienst.arbeitsort_aendern(
                    akteur,
                    ort,
                    erwartete_revision=request.POST.get("revision") or None,
                    bestaetigen=True,
                    **form.cleaned_data,
                )
                messages.success(
                    request, f"Arbeitsort „{form.cleaned_data['name']}“ gespeichert und übernommen."
                )
            elif wahl == "verwerfen":
                dienst.arbeitsort_aendern(
                    akteur, ort, erwartete_revision=None, aktiv=False, grund="Vorschlag verworfen"
                )
                messages.success(request, f"Vorschlag „{ort.name}“ verworfen.")
            else:
                raise Http404
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return _zurueck(request, f"/auftraege/{auftrag.pk}/")


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
def eigenes_ergebnis(request: HttpRequest, auftrag_id: int) -> HttpResponse:
    auftrag = _auftrag_oder_404(auftrag_id)
    form = EigenesErgebnisForm(
        listen_zusammenfuehren(request.POST, "dateien", "pruefungen", "fehlgeschlagen")
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
            auftrag.refresh_from_db()
            messages.success(
                request,
                f"Ergebnis zu {auftrag.nummer} ist eingereicht; Auftrag "
                f"{auftrag.get_zustand_display()}.",
            )
            return redirect("auftrag_detail", auftrag_id=auftrag.pk)
    return render(request, "steuerung/eigenes_ergebnis.html", {"auftrag": auftrag, "form": form})


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
            "ordneranfragen": OrdnerAnfrage.objects.select_related("bindung"),
            "projekte": Projekt.objects.filter(aktiv=True).order_by("name"),
            "github": abgleich.zugang_anzeige(_akteur(request)),
            "github_form": GitHubSchluesselForm(prefix="g"),
            **_zugaenge(ChatBindung.objects.all()),
        },
    )


def _github_ergebnis(request: HttpRequest, fn, *args) -> None:
    """Prüfergebnis als eine Meldung; der Stand bleibt zusätzlich in „GitHub verbinden“ sichtbar."""
    try:
        zugang = fn(_akteur(request), *args)
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
        return
    if zugang.fehler:
        messages.error(request, zugang.fehler)
    else:
        anzahl = len(zugang.repositorys)
        wort = "Repository" if anzahl == 1 else "Repositorys"
        messages.success(request, f"Verbunden als {zugang.konto}, {anzahl} {wort} lesbar.")


@sensitive_post_parameters("g-schluessel")
@login_required
@require_POST
def github_verbinden(request: HttpRequest) -> HttpResponse:
    form = GitHubSchluesselForm(request.POST, prefix="g")
    if form.is_valid():
        _github_ergebnis(request, abgleich.github_verbinden, form.cleaned_data["schluessel"])
    else:
        messages.error(request, "Bitte den Lese-Schlüssel einfügen.")
    return redirect(reverse("verbindungen") + "#github")


@login_required
@require_POST
def github_pruefen(request: HttpRequest) -> HttpResponse:
    _github_ergebnis(request, abgleich.github_pruefen)
    return redirect(reverse("verbindungen") + "#github")


@login_required
@require_POST
def github_trennen(request: HttpRequest) -> HttpResponse:
    try:
        abgleich.github_trennen(_akteur(request))
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    else:
        messages.success(request, "GitHub ist getrennt; der Schlüssel ist gelöscht.")
    return redirect(reverse("verbindungen") + "#github")


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
        return redirect("verbindungen")
    try:
        ort = dienst.ordner_zuordnen(_akteur(request), anfrage, projekt)
        messages.success(request, f"Ordner „{ort.pfad}“ gehört jetzt zu {projekt.name}.")
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("verbindungen")


@login_required
@require_POST
def ordner_verwerfen(request: HttpRequest, anfrage_id: int) -> HttpResponse:
    anfrage = get_object_or_404(OrdnerAnfrage, pk=anfrage_id)
    try:
        dienst.ordner_verwerfen(_akteur(request), anfrage)
    except dienst.DienstFehler as fehler:
        _fehler_anzeigen(request, fehler)
    return redirect("verbindungen")


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

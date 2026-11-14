"""Oberfläche. Views rufen nur den Dienst und rendern; keine Geschäftsregeln hier.

Aufbau: Projektauswahl als Einstieg, je Projekt vier Bereiche (Akte in ``views_akte``),
Verbindungen als technische Verwaltung. Aufträge erscheinen als ganze Seite.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from . import abgleich, akte, dienst, lieferungen, sicherung, views_werkbank
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
    ziel = _pfad_oder_leer(request.POST.get("zurueck") or request.GET.get("zurueck") or "")
    return redirect(ziel or standard)


# --- Projekte -------------------------------------------------------------------------------


@login_required
def projekte(request: HttpRequest) -> HttpResponse:
    """Einstieg: Projekt wählen. Alle Aufträge zusammen zeigt ``alle_auftraege``."""
    projekte_qs = Projekt.objects.filter(aktiv=True).prefetch_related("module")
    suche = request.GET.get("q", "").strip()
    if suche:
        projekte_qs = projekte_qs.filter(
            Q(name__icontains=suche) | Q(beschreibung__icontains=suche) | Q(zweck__icontains=suche)
        )
    projekte_liste = list(projekte_qs)
    abgleich.versionen_auffrischen(projekte_liste)
    wartet = []
    for p in projekte_liste:
        p.offen = p.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE).count()
        eintraege = wartet_eintraege(p)
        p.wartet = len(eintraege)
        p.version = abgleich.version_anzeige(p)["zustand"]
        if not suche:
            wartet.extend(eintraege)
    # Über alle Projekte: Fälliges zuerst, sonst die Reihenfolge je Projekt (stabil sortiert).
    wartet.sort(key=lambda w: 0 if w.get("faellig") else 1)
    laeuft = dienst.laeuft_gerade() if not suche else []
    return render(
        request,
        "steuerung/projekte.html",
        {
            "wartet": wartet,
            "laeuft": laeuft,
            "gruss": _gruss(request),
            "offen_gesamt": sum(p.offen for p in projekte_liste),
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
                kuerzel=daten["kuerzel"],
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
    ("issues", "Issues", None, ()),
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
        # Offene Issues haben ihre eigene Spalte; erledigte stehen bei den Erledigten.
        offen_issue = [a for a in auftraege if a.typ == Auftrag.TYP_ISSUE and not a.ist_endzustand]
        if klasse == "issues":
            karten = [a for a in offen_issue if a.zustand != Auftrag.ABGEBROCHEN]
        else:
            karten = [a for a in auftraege if a.zustand in zustaende and a not in offen_issue]
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


def _werkbank_hauptaktion(auftrag: Auftrag) -> dict:
    """Für Werkbank-Aufträge gibt es keinen Chat-Knopf: Der Satz sagt, was wartet und wo."""
    ausf = auftrag.ausfuehrungen.order_by("-id").first()
    ziel = "#werkbank"
    if auftrag.zustand == Auftrag.FREIGEGEBEN:
        satz = "Freigegeben: Die zugewiesene Werkbank übernimmt ihn. Für dich ist nichts zu tun."
        return {"art": "hinweis", "satz": satz, "kurz": "wartet auf die Werkbank", "ziel": ziel}
    bedarf = [b for b in (ausf.bedarf if ausf else []) if b.get("offen")]
    if bedarf:
        arten = {"frage": "Rückfrage", "freigabe": "Freigabe", "fehler": "Fehler"}
        was = ", ".join(sorted({arten.get(b["typ"], "Klärung") for b in bedarf}))
        satz = f"Die Werkbank wartet auf dich ({was}). Antworten und Freigaben stehen unten bei „Ausführung“."
        return {"art": "hinweis", "satz": satz, "kurz": "Werkbank wartet auf dich", "ziel": ziel}
    satz = "In Arbeit auf einer KI-Werkbank. Für dich ist nichts zu tun."
    return {"art": "hinweis", "satz": satz, "kurz": "", "ziel": ziel}


def hauptaktion(auftrag: Auftrag, pruefpunkte=None) -> dict:
    """Der eine Knopf, der den Auftrag jetzt weiterbringt; gleich in „Wartet auf dich“, auf der
    Board-Karte und im Auftragskopf. Ohne möglichen Knopf ein Satz: warum und was zu tun ist
    (``kurz`` für die Karte)."""
    z = auftrag.zustand
    if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK and z in (
        Auftrag.IN_ARBEIT,
        Auftrag.BLOCKIERT,
        Auftrag.FREIGEGEBEN,
    ):
        return _werkbank_hauptaktion(auftrag)
    if z == Auftrag.BLOCKIERT:
        frage = auftrag.meldungen.filter(art__in=Meldung.BLOCKIERENDE, historisch=False).first()
        bedarf = lieferungen.offener_bedarf(auftrag)
        hinweis = ""
        if bedarf:
            liste = ", ".join(f"{b['auftrag']}/{b['lieferkennung']}" for b in bedarf)
            hinweis = (
                f"Damit entfällt der technische Lieferbedarf auf {liste}; eine spätere Lieferung "
                "weckt den Auftrag nicht mehr."
            )
        return {
            "art": "antworten",
            "text": "Antworten",
            "frage": frage,
            "lieferbedarf": bedarf,
            "verwaist": [
                b
                | {
                    "satz": f"{b['auftrag']} hat inzwischen Revision "
                    f"{b['aktuelle_lieferrevision']}, die Lieferung „{b['lieferkennung']}“ "
                    f"hängt noch an Revision {b['revision']}. Eine Lieferung weckt "
                    f"{auftrag.nummer} erst wieder, wenn ein Chat den Bedarf neu bindet. Sag "
                    f"ihm dazu: „Binde den Lieferbedarf von {auftrag.nummer} neu.“"
                }
                for b in bedarf
                if b["verwaist"]
            ],
            "lieferhinweis": hinweis,
        }
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


def _gruss(request: HttpRequest) -> str:
    """Begrüßung auf der Startseite nach Ortszeit, mit dem Vornamen oder Anmeldenamen."""
    stunde = timezone.localtime().hour
    tageszeit = "Guten Morgen" if stunde < 11 else "Guten Tag" if stunde < 18 else "Guten Abend"
    name = request.user.first_name or request.user.get_username().capitalize()
    return f"{tageszeit}, {name}."


def wartet_eintraege(projekt: Projekt) -> list[dict]:
    """Einträge für „Wartet auf dich“; Aufträge mit ihrem Hauptknopf, jeder mit seinem Projekt."""
    return [
        {"e": x, "p": projekt}
        if isinstance(x, Entscheidung)
        else {
            "p": projekt,
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
    kontext["darf_verschieben"] = _akteur(request).ist_owner
    art = request.GET.get("art", "")
    if art in dict(Auftrag.TYPEN):
        liste = liste.filter(typ=art)
    kontext["art"] = art
    if kontext.get("projekt") is not None:
        filter_, auswahl = modulwahl(kontext["projekt"], request.GET.get("modul", ""))
        if filter_:
            liste = liste.filter(**filter_)
        kontext.update(auswahl)
    else:
        # Projektübergreifende Ansicht: Module gehören zu einem Projekt, hier kein Modulfilter.
        kontext["modul"] = ""
    if ansicht == "board":
        alle = list(liste)
        for a in alle:
            a.haupt = hauptaktion(a)
        kontext["spalten"] = _spalten(alle)
        kontext["zaehlleiste"] = [
            (s["titel"], len(s["karten"]), s["klasse"])
            for s in kontext["spalten"]
            if s["klasse"] != "erledigt" and s["karten"]
        ]
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


PROJEKTWAHL_SITZUNG = "alle_auftraege_p"


def _projektwahl(request: HttpRequest, kennungen: list[str]) -> list[str]:
    """Welche Projekte „Alle Aufträge“ zeigt: ``?p=`` (mehrfach; ``alle`` oder ``-`` für keines),
    sonst die letzte Auswahl dieser Sitzung, sonst alle. Unbekannte Kennungen fallen weg."""
    roh = request.GET.getlist("p")
    if roh:
        if "alle" in roh:
            wahl = list(kennungen)
            request.session[PROJEKTWAHL_SITZUNG] = "alle"
        else:
            wahl = [k for k in kennungen if k in roh]
            request.session[PROJEKTWAHL_SITZUNG] = wahl if wahl != kennungen else "alle"
        return wahl
    gemerkt = request.session.get(PROJEKTWAHL_SITZUNG, "alle")
    if gemerkt == "alle" or not isinstance(gemerkt, list):
        return list(kennungen)
    return [k for k in kennungen if k in gemerkt] or list(kennungen)


def _chip_link(request: HttpRequest, kennungen: list[str], wahl: list[str]) -> str:
    """Adresse mit dieser Auswahl; andere Parameter (Ansicht, Art, Filter) bleiben erhalten."""
    params = request.GET.copy()
    if wahl == kennungen:
        params.setlist("p", ["alle"])
    elif not wahl:
        params.setlist("p", ["-"])
    else:
        params.setlist("p", wahl)
    return "?" + params.urlencode()


@login_required
def alle_auftraege(request: HttpRequest) -> HttpResponse:
    """Board oder Liste über alle aktiven Projekte; Projekte per Schaltfläche ein- und ausschalten."""
    projekte_ = list(Projekt.objects.filter(aktiv=True).order_by("name"))
    kennungen = [p.kennung for p in projekte_]
    wahl = _projektwahl(request, kennungen)
    offen = dict(
        Auftrag.objects.filter(projekt__aktiv=True)
        .exclude(zustand__in=Auftrag.ENDZUSTAENDE)
        .values_list("projekt__kennung")
        .annotate(n=Count("pk"))
    )
    chips = []
    for p in projekte_:
        neu = [k for k in kennungen if (k in wahl) != (k == p.kennung)]
        chips.append(
            {
                "name": p.name,
                "an": p.kennung in wahl,
                "zahl": offen.get(p.kennung, 0),
                "href": _chip_link(request, kennungen, neu),
            }
        )
    liste = (
        Auftrag.objects.filter(projekt__aktiv=True, projekt__kennung__in=wahl)
        .select_related("projekt", "modul")
        .prefetch_related("revisionen")
    )
    kontext = {
        "alle": True,
        "chips": chips,
        "alle_link": _chip_link(request, kennungen, list(kennungen)),
        "keine_link": _chip_link(request, kennungen, []),
        "keine_gewaehlt": not wahl,
    }
    return _auftragsliste(request, liste, "steuerung/alle_auftraege.html", kontext)


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
    if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
        aktiv = dienst._werkbank_aktiv(auftrag)
        for x in aktionen:
            if x["name"] == "selbst":
                x.update(erlaubt=False, grund="Der Auftrag ist einer Werkbank zugewiesen.")
            elif aktiv and x["name"] in ("unklar", "abbrechen"):
                x.update(erlaubt=False, grund="Läuft auf einer Werkbank: unten bei „Ausführung“.")
        if aktiv:
            bearbeitbar = False
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
        "ereignisse": dienst.auftrag_ereignisse(auftrag)[:50],
        "fruehere_nummern": auftrag.fruehere_nummern.select_related("projekt"),
        "darf_verschieben": _akteur(request).ist_owner,
        "verschieben_gesperrt": dienst.verschieben_gesperrt(auftrag),
        "ist_bearbeiter": auftrag.bearbeiter_benutzer_id == request.user.pk,
        "revisionen": auftrag.revisionen.all(),
        "zurueck": request.GET.get("zurueck", ""),
        "arbeitsorte": auftrag.projekt.arbeitsorte.filter(aktiv=True, bestaetigt=True),
        "arbeitsort_form": ArbeitsortForm(prefix="ort"),
        "werkbank": views_werkbank.auftrag_daten(auftrag) if _akteur(request).ist_owner else None,
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
    ketten = lieferungen.betroffene_ketten(auftrag)
    form = ArbeitspaketForm(
        listen_zusammenfuehren(request.POST, "schreibumfang", "abnahmekriterien")
        if request.method == "POST"
        else None,
        initial=anfang,
        auftrag=auftrag,
        lieferketten=ketten,
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
                lieferketten_bestaetigt=bool(d.get("lieferketten_bestaetigt")),
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
        {
            "auftrag": auftrag,
            "form": form,
            "rev": rev,
            "folge": _folge_der_bearbeitung(auftrag),
            "lieferketten": ketten,
        },
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


BEHALTEN = "__behalten"


def _pfad_oder_leer(wert: str) -> str:
    """Nur Pfade dieser Anwendung. Browser lesen einen Rückstrich wie „/“ und entfernen Tab und
    Zeilenumbruch: „/<Tab>/host“ führte sonst nach außen."""
    if not wert.startswith("/") or wert.startswith("//"):
        return ""
    return "" if "\\" in wert or any(ord(z) < 32 or ord(z) == 127 for z in wert) else wert


def _zielgruppen(ziele: list[dict]) -> list[dict]:
    """Zielauswahl ohne JavaScript: je Projekt eine Gruppe, Wert ``kennung|modulkennung``."""
    return [
        {
            "titel": z["name"] + (" (archiviert)" if z["archiviert"] else ""),
            "optionen": [
                {"wert": f"{z['kennung']}|", "text": "ohne Modul / direkt im Projekt"},
                *(
                    {"wert": f"{z['kennung']}|{m['kennung']}", "text": m["pfad"]}
                    for m in z["module"]
                ),
            ],
        }
        for z in ziele
    ]


def _verschiebe_zeile(post, auftrag: Auftrag) -> dict:
    """Ein Eintrag für den Dienst aus den Formularfeldern: eigenes Ziel vor gemeinsamem Ziel."""
    pk = auftrag.pk
    ziel = post.get(f"ziel_{pk}") or post.get("ziel") or ""
    kennung, _, modul = ziel.partition("|")
    zeile = {
        "auftrag": auftrag.nummer,
        "revision": post.get(f"revision_{pk}", auftrag.revision_nr),
        "projekt": kennung,
        "modul": modul,
    }
    for feld in ("arbeitsort", "meilenstein"):
        wert = post.get(f"{feld}_{pk}")
        if wert is not None and wert != BEHALTEN:
            zeile[feld] = wert
    return zeile


def _vorschau_zeilen(post, auftraege, ziele, vorschau) -> list[dict]:
    """Was die Seite je Eintrag zeigt; die Auswahl im Ziel erscheint, sobald das Ziel feststeht."""
    nach_kennung = {z["kennung"]: z for z in ziele}
    plan = {e["id"]: e for e in (vorschau or {}).get("eintraege", []) if "id" in e}
    zeilen = []
    for a in auftraege:
        e = plan.get(a.pk)
        zeile = {
            "a": a,
            "e": e,
            "revision": post.get(f"revision_{a.pk}", a.revision_nr),
            "ziel": post.get(f"ziel_{a.pk}", ""),
        }
        nach = e and e.get("nach")
        if nach:
            z = nach_kennung[nach["projekt"]]
            bleibt = nach["projekt"] == a.projekt.kennung
            rev = a.aktuelle_revision
            standard = BEHALTEN if bleibt else ""
            orte = [
                {
                    "wert": o["kennung"],
                    "text": o["name"] + ("" if o["bestaetigt"] else " (Vorschlag)"),
                }
                for o in z["arbeitsorte"]
            ]
            marken = [
                {"wert": m["nummer"], "text": f"{m['nummer']} {m['name']}"}
                for m in z["meilensteine"]
            ]
            bisher_ort = rev.arbeitsort.name if rev.arbeitsort else "keiner"
            bisher_ms = a.meilenstein.name if a.meilenstein else "keiner"
            zeile.update(
                orte=[
                    *([{"wert": BEHALTEN, "text": f"bisher: {bisher_ort}"}] if bleibt else []),
                    {"wert": "", "text": "kein Arbeitsort (später wählen)"},
                    *orte,
                ],
                ort_wahl=post.get(f"arbeitsort_{a.pk}", standard),
                ort_waehlbar=not a.ist_endzustand,
                marken=[
                    *([{"wert": BEHALTEN, "text": f"bisher: {bisher_ms}"}] if bleibt else []),
                    {"wert": "", "text": "kein Meilenstein"},
                    *marken,
                ],
                marke_wahl=post.get(f"meilenstein_{a.pk}", standard),
            )
        zeilen.append(zeile)
    return zeilen


@login_required
def verschieben(request: HttpRequest) -> HttpResponse:
    """Auswahl → Ziel → Vorschau → „Jetzt verschieben“. Regeln und Prüfung stehen im Dienst; ohne
    JavaScript bedienbar, Abbrechen ist ein Link und ändert nichts."""
    akteur = _akteur(request)
    daten = request.POST if request.method == "POST" else request.GET
    zurueck = _pfad_oder_leer(daten.get("zurueck", ""))
    weg = zurueck or reverse("projekte")
    try:
        ziele = dienst.verschiebeziele(akteur)
    except dienst.KeinZugriff as fehler:
        _fehler_anzeigen(request, fehler)
        return redirect(weg)
    ids = [int(x) for x in daten.getlist("auftrag") if x.isdecimal()]
    auftraege = list(
        Auftrag.objects.filter(pk__in=ids)
        .select_related("projekt", "modul", "meilenstein")
        .order_by("id")
    )
    if not auftraege:
        messages.error(request, "Nichts ausgewählt. Einträge auf dem Board ankreuzen.")
        return redirect(weg)
    vorschau = None
    if request.method == "POST":
        zeilen = [_verschiebe_zeile(request.POST, a) for a in auftraege]
        archiv = {
            k.removeprefix("archiv_"): v for k, v in request.POST.items() if k.startswith("archiv_")
        }
        try:
            if request.POST.get("aktion") == "ausfuehren":
                ergebnis = dienst.verschiebung_ausfuehren(
                    akteur, zeilen, archiv=archiv, pruefcode=request.POST.get("pruefcode", "")
                )
                request.session["verschiebe_ergebnis"] = {**ergebnis, "zurueck": zurueck}
                return redirect("verschieben_ergebnis")
            vorschau = dienst.verschiebung_pruefen(akteur, zeilen, archiv=archiv)
        except dienst.VerschiebungAbgelehnt as fehler:
            vorschau = fehler.vorschau
            messages.error(request, str(fehler))
        except dienst.DienstFehler as fehler:
            _fehler_anzeigen(request, fehler)
            return redirect(weg)
    return render(
        request,
        "steuerung/verschieben.html",
        {
            "zeilen": _vorschau_zeilen(request.POST, auftraege, ziele, vorschau),
            "gruppen": _zielgruppen(ziele),
            "ziel": request.POST.get("ziel", ""),
            "vorschau": vorschau,
            "zurueck": zurueck,
            "abbrechen": weg,
        },
    )


@login_required
def verschieben_ergebnis(request: HttpRequest) -> HttpResponse:
    ergebnis = request.session.get("verschiebe_ergebnis")
    if not ergebnis:
        return redirect("projekte")
    return render(request, "steuerung/verschieben_ergebnis.html", {"ergebnis": ergebnis})


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
        dienst.rueckfrage_beantworten(
            _akteur(request),
            auftrag,
            request.POST.get("text", ""),
            lieferketten_bestaetigt=request.POST.get("lieferketten_bestaetigt") == "ja",
        )
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
            **views_werkbank.verwaltung(request),
            **_zugaenge(ChatBindung.objects.exclude(art=ChatBindung.ART_WERKBANK)),
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

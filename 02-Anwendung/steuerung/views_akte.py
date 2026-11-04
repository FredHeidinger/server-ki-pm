"""Oberfläche der Projektakte: Überblick, Wissen, Projekteinstellungen. Nur Dienstaufrufe und
Darstellung.

Jede Ansicht lädt Objekte ausschließlich über das Projekt in der Adresse; eine fremde Nummer
ergibt „nicht gefunden“, nie ein Objekt eines anderen Projekts.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import abgleich, ablage, akte, dienst
from .forms import (
    ArbeitsortForm,
    EntscheidungEintragForm,
    MeilensteinForm,
    ModulForm,
    ProfilForm,
    UnterlageLinkForm,
    UnterlageTextForm,
)
from .models import (
    Auftrag,
    ChatBindung,
    Entscheidung,
    Meilenstein,
    Projekt,
    Unterlage,
)
from .views import wartet_eintraege


def _akteur(request: HttpRequest) -> dienst.Akteur:
    return dienst.Akteur.aus_benutzer(request.user)


def _projekt(kennung: str) -> Projekt:
    return get_object_or_404(Projekt, kennung=kennung)


def _kontext(projekt: Projekt, reiter: str, **weiteres) -> dict:
    return {"projekt": projekt, "reiter": reiter, **weiteres}


def _gefunden(fn, *args):
    try:
        return fn(*args)
    except dienst.KeinZugriff as fehler:
        raise Http404 from fehler


def _ausfuehren(request: HttpRequest, erfolg: str, fn, *args, **kwargs):
    try:
        ergebnis = fn(*args, **kwargs)
    except dienst.DienstFehler as fehler:
        messages.error(request, str(fehler))
        return None
    if erfolg:
        messages.success(request, erfolg)
    return ergebnis if ergebnis is not None else True


# --- Überblick -------------------------------------------------------------------------------


@login_required
def ueberblick(request: HttpRequest, kennung: str) -> HttpResponse:
    """Von oben: Wartet auf dich · Als Nächstes · Versionszeile · Meilensteine · zuletzt erledigt ·
    Beschreibung."""
    projekt = _projekt(kennung)
    abgleich.versionen_auffrischen([projekt])
    return render(
        request,
        "steuerung/akte_ueberblick.html",
        _kontext(
            projekt,
            "ueberblick",
            wartet=wartet_eintraege(projekt),
            als_naechstes=dienst.weiter_reihenfolge(projekt)[:5],
            version=abgleich.version_anzeige(projekt),
            meilensteine=_meilensteine_mit_form(projekt),
            meilenstein_form=MeilensteinForm(prefix="m"),
            erledigt=projekt.auftraege.filter(zustand=Auftrag.ERLEDIGT)
            .select_related("modul")
            .order_by("-aktualisiert_am", "-id")[:5],
            offen=projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE).count(),
        ),
    )


@login_required
def profil(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    anfang = {f: getattr(projekt, f) for f in ("beschreibung", *akte.PROFILFELDER)} | {
        "revision": projekt.profil_revision
    }
    form = ProfilForm(request.POST or None, initial=anfang)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            akte.profil_bearbeiten(
                _akteur(request), projekt, erwartete_revision=d.pop("revision"), **d
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, "Projektprofil gespeichert.")
            return redirect("projekt_detail", kennung=kennung)
    return render(request, "steuerung/akte_profil.html", _kontext(projekt, "ueberblick", form=form))


# --- Meilensteine ----------------------------------------------------------------------------


def _meilensteine_mit_form(projekt: Projekt) -> list:
    """Gezählte Meilensteine mit vorbelegtem Bearbeitungsformular (ohne JavaScript bedienbar)."""
    liste = akte.meilensteine_des_projekts(projekt)
    for m in liste:
        m.form = MeilensteinForm(
            prefix=f"m{m.pk}",
            initial={"revision": m.revision, "name": m.name, "datum": m.termin},
        )
    return liste


@login_required
@require_POST
def meilenstein_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = MeilensteinForm(request.POST, prefix="m")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"Meilenstein „{d['name']}“ angelegt.",
            akte.meilenstein_anlegen,
            _akteur(request),
            projekt,
            name=d["name"],
            datum=d["datum"],
        )
    else:
        messages.error(request, "Meilenstein: Name ist erforderlich, das Datum muss gültig sein.")
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#meilensteine")


# --- Wissen ----------------------------------------------------------------------------------


@login_required
def wissen(request: HttpRequest, kennung: str) -> HttpResponse:
    """Entscheidungen, Texte und Links; alles lesen die Chats des Projekts ohne Freigabe."""
    projekt = _projekt(kennung)
    zeigen_archiv = request.GET.get("archiv") == "1"
    gruppen: dict[str, list] = {"text": [], "link": [], "datei": []}
    for u in projekt.unterlagen.filter(archiviert=zeigen_archiv).prefetch_related("versionen"):
        fassungen = sorted(u.versionen.all(), key=lambda v: -v.nummer)
        zeile = {"u": u, "v": fassungen[0] if fassungen else None, "fassungen": len(fassungen)}
        gruppen[u.art if u.art in (Unterlage.TEXT, Unterlage.DATEI) else "link"].append(zeile)
    return render(
        request,
        "steuerung/akte_wissen.html",
        _kontext(
            projekt,
            "wissen",
            entscheidungen=[] if zeigen_archiv else akte.entscheidungen_des_projekts(projekt),
            texte=gruppen["text"],
            links=gruppen["link"],
            dateien=gruppen["datei"],
            zeigen_archiv=zeigen_archiv,
        ),
    )


@login_required
@require_POST
def entscheidung_antworten(request: HttpRequest, kennung: str, nummer: int) -> HttpResponse:
    """Antwort aus „Wartet auf dich“; danach gilt sie als Entscheidung."""
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    e = _gefunden(akte.entscheidung_holen, akteur, projekt, nummer)
    stand = request.POST.get("stand", "")
    _ausfuehren(
        request,
        f"{e.nummer} gilt jetzt als Entscheidung.",
        akte.entscheidung_beantworten,
        akteur,
        projekt,
        e,
        erwarteter_stand=int(stand) if stand.isdecimal() else -1,
        antwort=request.POST.get("antwort", ""),
    )
    return redirect("projekt_detail", kennung=kennung)


@login_required
def entscheidung_bearbeiten(request: HttpRequest, kennung: str, nummer: int = 0) -> HttpResponse:
    """Entscheidung von Hand eintragen, ändern oder zurückziehen; meist trägt sie der Chat ein."""
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    e = _gefunden(akte.entscheidung_holen, akteur, projekt, nummer) if nummer else None
    if e is not None and e.zustand == Entscheidung.ZURUECKGEZOGEN:
        raise Http404
    anfang = {"entscheidung": e.text, "warum": e.warum, "stand": e.stand} if e else {}
    form = EntscheidungEintragForm(request.POST or None, initial=anfang)
    if request.method == "POST" and request.POST.get("zurueckziehen") == "1" and e is not None:
        _ausfuehren(
            request,
            f"{e.nummer} ist zurückgezogen.",
            akte.entscheidung_aendern,
            akteur,
            projekt,
            e,
            erwarteter_stand=int(request.POST.get("stand") or 0),
            zurueckziehen=True,
        )
        return redirect("akte_wissen", kennung=kennung)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            if e is None:
                e = akte.entscheidung_eintragen(
                    akteur, projekt, entscheidung=d["entscheidung"], warum=d["warum"]
                )
            else:
                e = akte.entscheidung_aendern(
                    akteur,
                    projekt,
                    e,
                    erwarteter_stand=d["stand"],
                    entscheidung=d["entscheidung"],
                    warum=d["warum"],
                )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"{e.nummer} ist gespeichert und steht im Verlauf.")
            return redirect("akte_wissen", kennung=kennung)
    return render(
        request,
        "steuerung/akte_entscheidung.html",
        _kontext(projekt, "wissen", form=form, e=e),
    )


@login_required
def unterlage_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    art = request.GET.get("art") or request.POST.get("art") or "text"
    if art not in ("text", "link"):
        art = "text"
    form = (UnterlageTextForm if art == "text" else UnterlageLinkForm)(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        akteur = _akteur(request)
        try:
            if art == "text":
                u = akte.text_anlegen(
                    akteur,
                    projekt,
                    titel=d["titel"],
                    text=d["text"],
                    beschreibung=d["beschreibung"],
                )
            else:
                u = akte.link_anlegen(
                    akteur,
                    projekt,
                    titel=d["titel"],
                    uri=d["uri"],
                    quellversion=d["quellversion"],
                    beschreibung=d["beschreibung"],
                )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(
                request, f"{u.kennung} „{u.titel}“ ist gespeichert und für die Chats lesbar."
            )
            return redirect("unterlage_detail", kennung=kennung, nummer=u.nummer)
    return render(
        request,
        "steuerung/akte_unterlage_neu.html",
        _kontext(projekt, "wissen", form=form, art=art),
    )


@login_required
def unterlage_detail(request: HttpRequest, kennung: str, nummer: int) -> HttpResponse:
    projekt = _projekt(kennung)
    u = _gefunden(akte.unterlage_holen, _akteur(request), projekt, nummer)
    aktuell = akte.aktuelle_fassung(u)
    gezeigt = aktuell
    wahl = request.GET.get("fassung", "")
    if wahl.isdigit() and aktuell is not None and int(wahl) != aktuell.nummer:
        gezeigt = _gefunden(akte.version_holen, _akteur(request), projekt, nummer, int(wahl))
    return render(
        request,
        "steuerung/akte_unterlage.html",
        _kontext(
            projekt,
            "wissen",
            u=u,
            aktuell=aktuell,
            gezeigt=gezeigt,
            versionen=u.versionen.order_by("-nummer"),
        ),
    )


@login_required
@require_POST
def unterlage_aktion(request: HttpRequest, kennung: str, nummer: int, aktion: str) -> HttpResponse:
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    u = _gefunden(akte.unterlage_holen, akteur, projekt, nummer)
    p = request.POST
    if aktion == "text":
        _ausfuehren(
            request,
            "Neue Fassung gespeichert; ältere Fassungen bleiben aufrufbar.",
            akte.text_neue_version,
            akteur,
            u,
            text=p.get("text", ""),
            erwartete_nr=int(p.get("erwartete_nr") or 0),
        )
    elif aktion == "link":
        _ausfuehren(
            request,
            "Neue Linkversion gespeichert.",
            akte.link_neue_version,
            akteur,
            u,
            uri=p.get("uri", ""),
            quellversion=p.get("quellversion", ""),
            erwartete_nr=int(p.get("erwartete_nr") or 0),
        )
    elif aktion == "archiv":
        _ausfuehren(
            request, "Gespeichert.", akte.unterlage_archivieren, akteur, u, p.get("archiv") == "1"
        )
    else:
        raise Http404
    return redirect("unterlage_detail", kennung=kennung, nummer=nummer)


@login_required
def unterlage_datei(request: HttpRequest, kennung: str, nummer: int, vnr: int) -> HttpResponse:
    """Früher hochgeladene Dateien bleiben lesbar: nach erneuter Berechtigungsprüfung, nur geprüfte
    Dateien, immer als Anhang. Neue Dateien gibt es nicht."""
    projekt = _projekt(kennung)
    v = _gefunden(akte.version_holen, _akteur(request), projekt, nummer, vnr)
    if not v.blob:
        raise Http404
    if not v.herunterladbar:
        return HttpResponse(
            f"Diese Datei ist nicht freigegeben ({v.get_pruefung_display()}).",
            status=409,
            content_type="text/plain; charset=utf-8",
        )
    pfad = ablage.blob_pfad(v.blob)
    if not pfad.exists():
        raise Http404
    antwort = FileResponse(
        open(pfad, "rb"),
        as_attachment=True,
        filename=v.originalname or f"{v.unterlage.kennung}-v{v.nummer}",
        content_type=v.inhaltstyp or "application/octet-stream",
    )
    antwort["X-Content-Type-Options"] = "nosniff"
    antwort["Content-Security-Policy"] = "default-src 'none'; sandbox"
    antwort["Cache-Control"] = "private, no-store"
    return antwort


# --- Projekteinstellungen: Module, Arbeitsorte, GitHub ---------------------------------------


@login_required
def einstellungen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    module = list(projekt.module.all())
    baum = dienst.modulbaum(projekt)
    for k in baum["flach"]:
        # Umhängen nur unter Module außerhalb des eigenen Teilbaums.
        eigene = set(dienst.modul_teilbaum(k.modul))
        k.ziele = [z.modul for z in baum["flach"] if z.modul.pk not in eigene]
        k.aktive_kinder = bool(k.kinder)
    zugang = abgleich.zugang_anzeige(_akteur(request))
    version = abgleich.version_anzeige(projekt)
    # Das gewählte Repository bleibt wählbar, auch wenn der Schlüssel es gerade nicht listet.
    repo_namen = sorted(
        {r["name"] for r in zugang["repositorys"]} | ({version["repository"]} - {""}),
        key=str.lower,
    )
    return render(
        request,
        "steuerung/projekt_einstellungen.html",
        _kontext(
            projekt,
            "einstellungen",
            arbeitsorte_aktiv=projekt.arbeitsorte.filter(aktiv=True).select_related("modul"),
            arbeitsorte_archiv=projekt.arbeitsorte.filter(aktiv=False),
            laufend=dienst.laufende_eintraege(projekt),
            bindungen=projekt.bindungen.all(),
            ki=ChatBindung.objects.filter(letztes_projekt=projekt).order_by("-zuletzt_aktiv")[:5],
            arbeitsort_form=ArbeitsortForm(prefix="ort", projekt=projekt),
            modul_form=ModulForm(prefix="modul"),
            modulbaum=baum,
            modularchiv=[m for m in module if not m.aktiv],
            github=zugang,
            version=version,
            repo_namen=repo_namen,
        ),
    )


@login_required
@require_POST
def repo_waehlen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    name = request.POST.get("repository", "")
    _ausfuehren(
        request,
        f"Repository {name} gewählt." if name else "Kein Repository mehr gewählt.",
        abgleich.repository_waehlen,
        _akteur(request),
        projekt,
        name,
    )
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#github")


@login_required
@require_POST
def version_pruefen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    _ausfuehren(request, "", abgleich.version_abrufen, _akteur(request), projekt)
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#version")


@login_required
@require_POST
def meilenstein_bearbeiten(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    projekt = _projekt(kennung)
    m = get_object_or_404(Meilenstein, pk=pk, projekt=projekt)
    form = MeilensteinForm(request.POST, prefix=f"m{m.pk}")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"Meilenstein „{d['name']}“ gespeichert.",
            akte.meilenstein_bearbeiten,
            _akteur(request),
            m,
            erwartete_revision=d["revision"],
            name=d["name"],
            datum=d["datum"],
        )
    else:
        messages.error(request, "Meilenstein: Name ist erforderlich, das Datum muss gültig sein.")
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#meilensteine")

"""Oberfläche der Projektakte: Überblick, Funktionen & Planung, Versionen & Betrieb, Wissen &
Entscheidungen, Verlauf, Suche, KI-Kontext und GitHub. Nur Dienstaufrufe und Darstellung.

Jede Ansicht lädt Objekte ausschließlich über das Projekt in der Adresse; eine fremde Nummer
ergibt „nicht gefunden“, nie ein Objekt eines anderen Projekts.
"""

import json
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_POST

from . import abgleich, ablage, akte, dienst, github
from .forms import (
    AbnahmeForm,
    ArbeitsortForm,
    FunktionForm,
    GitHubAktionForm,
    InstallationForm,
    MeilensteinForm,
    ModulForm,
    NachweisForm,
    ProfilForm,
    ReleaseForm,
    RepoForm,
    UmgebungForm,
    UnterlageDateiForm,
    UnterlageLinkForm,
    UnterlageTextForm,
    listen_zusammenfuehren,
)
from .kontext import modulwahl
from .models import (
    Abrufstand,
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
    Ergebnis,
    ExterneAktion,
    ExternesObjekt,
    Funktion,
    Installation,
    Meilenstein,
    Nachweis,
    Objektversion,
    Projekt,
    Release,
    Umgebung,
    Unterlage,
)


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
    projekt = _projekt(kennung)
    staende = [
        (f, akte.funktionsstand(f))
        for f in projekt.funktionen.exclude(planung=Funktion.VERWORFEN)
        .select_related("modul")
        .prefetch_related("auftraege")
    ]
    grenze = akte.obere_grenze(projekt)
    offen = projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE)
    return render(
        request,
        "steuerung/akte_ueberblick.html",
        _kontext(
            projekt,
            "ueberblick",
            nutzbar=[(f, s) for f, s in staende if s["nutzbar"]],
            entwicklung=[(f, s) for f, s in staende if s["umsetzung"] == "in Arbeit"],
            widersprueche=[(f, s) for f, s in staende if s["widerspruch"]],
            funktionen_anzahl=len(staende),
            installationen=akte.installationen_aktuell(projekt),
            meilensteine=projekt.meilensteine.filter(
                zustand__in=(Meilenstein.GEPLANT, Meilenstein.IN_ARBEIT)
            ).prefetch_related("funktionen"),
            aenderungen=akte.aenderungen_seit_besuch(request.user, projekt, grenze),
            grenze=grenze,
            pruefbeduerftig=akte.pruefbeduerftige_auftraege(projekt),
            modulstaende=(staende := akte.modulstaende(projekt)),
            modulstand_zeigen=len(staende) > 1 or any(z["gemeldet"] for z in staende),
            kennzahlen={
                "auftraege": offen.filter(typ=Auftrag.TYP_AUFTRAG).count(),
                "issues": offen.filter(typ=Auftrag.TYP_ISSUE).count(),
                "blockiert": offen.filter(zustand=Auftrag.BLOCKIERT).count(),
            },
            profil_alt=Ereignis.objects.filter(
                projekt=projekt, objekt_typ="Projektprofil", ereignis__startswith="Profil Revision"
            ).first(),
            entwurf_hash=akte.entwurf_hash(projekt) if projekt.profil_entwurf else "",
            profilentwurf=[
                {
                    "feld": Projekt._meta.get_field(feld).verbose_name,
                    "alt": getattr(projekt, feld),
                    "neu": wert,
                }
                for feld, wert in (projekt.profil_entwurf or {}).items()
            ],
        ),
    )


@login_required
@require_POST
def gesehen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    try:
        grenze = int(request.POST.get("grenze", "0"))
    except ValueError:
        grenze = 0
    akte.als_gesehen_markieren(request.user, projekt, grenze)
    messages.success(request, "Änderungen bis zur angezeigten Stelle als gesehen markiert.")
    return redirect("projekt_detail", kennung=kennung)


@login_required
def profil(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    anfang = {f: getattr(projekt, f) for f in akte.PROFILFELDER} | {
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


# --- Funktionen & Planung --------------------------------------------------------------------


@login_required
def funktionen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    liste = projekt.funktionen.select_related("modul").prefetch_related("auftraege")
    filter_, auswahl = modulwahl(projekt, request.GET.get("modul", ""))
    if filter_:
        liste = liste.filter(**filter_)
    zeigen_verworfen = request.GET.get("verworfen") == "1"
    if not zeigen_verworfen:
        liste = liste.exclude(planung=Funktion.VERWORFEN)
    return render(
        request,
        "steuerung/akte_funktionen.html",
        _kontext(
            projekt,
            "funktionen",
            zeilen=[(f, akte.funktionsstand(f)) for f in liste],
            zeigen_verworfen=zeigen_verworfen,
            meilensteine=_meilensteine_mit_form(projekt),
            meilenstein_form=MeilensteinForm(projekt=projekt, prefix="m"),
            zustaende=Meilenstein.ZUSTAENDE,
            **auswahl,
        ),
    )


def _meilensteine_mit_form(projekt: Projekt) -> list:
    """Meilensteine mit vorbelegtem Bearbeitungsformular (ohne JavaScript bedienbar)."""
    liste = list(projekt.meilensteine.prefetch_related("funktionen"))
    for m in liste:
        m.form = MeilensteinForm(
            projekt=projekt,
            prefix=f"m{m.pk}",
            initial={
                "name": m.name,
                "ziel": m.ziel,
                "zielversion": m.zielversion,
                "termin": m.termin,
                "funktionen": list(m.funktionen.all()),
            },
        )
    return liste


@login_required
def funktion_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = FunktionForm(
        listen_zusammenfuehren(request.POST, "kriterien") if request.method == "POST" else None,
        projekt=projekt,
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            f = akte.funktion_anlegen(
                _akteur(request),
                projekt,
                titel=d["titel"],
                nutzen=d["nutzen"],
                kriterien=d["kriterien"],
                planung=d["planung"],
                modul=d.get("modul"),
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, f"{f.kennung} „{f.titel}“ ist erfasst.")
            return redirect("funktion_detail", kennung=kennung, nummer=f.nummer)
    return render(
        request,
        "steuerung/akte_formular.html",
        _kontext(
            projekt,
            "funktionen",
            form=form,
            titel="Funktion erfassen",
            erklaerung="Eine Funktion beschreibt eine Fähigkeit des Produkts. Aufträge arbeiten "
            "daran; ob sie nutzbar ist, zeigen erst Nachweise.",
        ),
    )


@login_required
def funktion_detail(request: HttpRequest, kennung: str, nummer: int) -> HttpResponse:
    projekt = _projekt(kennung)
    f = _gefunden(akte.funktion_holen, _akteur(request), projekt, nummer)
    stand = akte.funktionsstand(f)
    bearbeiten = FunktionForm(
        initial={
            "revision": f.revision,
            "titel": f.titel,
            "nutzen": f.nutzen,
            "kriterien": f.kriterien,
            "planung": f.planung,
            "modul": f.modul,
        },
        projekt=projekt,
        prefix="f",
    )
    verknuepfte = list(f.auftraege.all())
    return render(
        request,
        "steuerung/akte_funktion.html",
        _kontext(
            projekt,
            "funktionen",
            f=f,
            stand=stand,
            bearbeiten=bearbeiten,
            auftraege=verknuepfte,
            waehlbar=projekt.auftraege.exclude(pk__in=[a.pk for a in verknuepfte]).exclude(
                zustand=Auftrag.ABGEBROCHEN
            ),
            ergebnisse=Ergebnis.objects.filter(auftrag__in=verknuepfte)
            .exclude(nachweise__funktion=f)
            .select_related("auftrag"),
            nachweise=f.nachweise.select_related("umgebung").all(),
            nachweis_form=NachweisForm(
                projekt=projekt, prefix="n", initial={"funktion": f, "version": stand["version"]}
            ),
            abnahme_form=AbnahmeForm(prefix="ab", initial={"version": stand["version"]}),
            unterlagen=f.unterlagen.select_related("version__unterlage"),
            versionen=Objektversion.objects.filter(objekt_typ="Funktion", objekt_id=f.pk)[:20],
        ),
    )


@login_required
@require_POST
def funktion_aktion(request: HttpRequest, kennung: str, nummer: int, aktion: str) -> HttpResponse:
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    f = _gefunden(akte.funktion_holen, akteur, projekt, nummer)
    ziel = reverse("funktion_detail", kwargs={"kennung": kennung, "nummer": nummer})
    if aktion == "bearbeiten":
        form = FunktionForm(
            listen_zusammenfuehren(request.POST, "f-kriterien"), projekt=projekt, prefix="f"
        )
        if form.is_valid():
            d = form.cleaned_data
            felder = {k: d[k] for k in ("titel", "nutzen", "kriterien", "planung")}
            if "modul" in d:
                felder["modul"] = d["modul"]
            _ausfuehren(
                request,
                "Neue Revision gespeichert.",
                akte.funktion_bearbeiten,
                akteur,
                f,
                erwartete_revision=d["revision"],
                **felder,
            )
        else:
            messages.error(request, "Bitte Titel und Planungsstand angeben.")
    elif aktion in ("verknuepfen", "loesen"):
        auftrag = Auftrag.objects.filter(
            pk=request.POST.get("auftrag") or 0, projekt=projekt
        ).first()
        if auftrag is None:
            messages.error(request, "Bitte einen Auftrag dieses Projekts wählen.")
        else:
            _ausfuehren(
                request,
                f"{auftrag.nummer} {'verknüpft' if aktion == 'verknuepfen' else 'gelöst'}.",
                akte.funktion_auftrag,
                akteur,
                f,
                auftrag,
                verbinden=aktion == "verknuepfen",
            )
    elif aktion == "ergebnis":
        ergebnis = Ergebnis.objects.filter(
            pk=request.POST.get("ergebnis") or 0, auftrag__projekt=projekt
        ).first()
        if ergebnis is None:
            raise Http404
        _ausfuehren(
            request,
            "Ergebnis als Umsetzungsnachweis verbucht.",
            akte.ergebnis_als_nachweis,
            akteur,
            ergebnis,
            f,
        )
    elif aktion == "abnahme":
        form = AbnahmeForm(request.POST, prefix="ab")
        if form.is_valid():
            d = form.cleaned_data
            _ausfuehren(
                request,
                "Abnahmeentscheidung gespeichert.",
                akte.funktion_abnehmen,
                akteur,
                f,
                version=d["version"],
                angenommen=d["entscheidung"] == "angenommen",
                kriterien=d["kriterien"],
                begruendung=d["begruendung"],
            )
        else:
            messages.error(
                request, "Abnahme: Version, Entscheidung und geprüfte Kriterien angeben."
            )
    else:
        raise Http404
    return redirect(ziel)


@login_required
@require_POST
def meilenstein_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = MeilensteinForm(request.POST, projekt=projekt, prefix="m")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"Meilenstein „{d['name']}“ angelegt.",
            akte.meilenstein_anlegen,
            _akteur(request),
            projekt,
            name=d["name"],
            ziel=d["ziel"],
            zielversion=d["zielversion"],
            funktionen=list(d["funktionen"]),
            termin=d["termin"],
        )
    else:
        messages.error(request, "Meilenstein: Name ist erforderlich.")
    return redirect(reverse("akte_funktionen", kwargs={"kennung": kennung}) + "#meilensteine")


@login_required
@require_POST
def meilenstein_zustand(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    projekt = _projekt(kennung)
    m = get_object_or_404(Meilenstein, pk=pk, projekt=projekt)
    _ausfuehren(
        request,
        "Meilenstein aktualisiert.",
        akte.meilenstein_zustand,
        _akteur(request),
        m,
        request.POST.get("zustand", ""),
        erwartete_revision=request.POST.get("revision"),
    )
    return redirect(reverse("akte_funktionen", kwargs={"kennung": kennung}) + "#meilensteine")


# --- Versionen & Betrieb ---------------------------------------------------------------------


@login_required
def betrieb(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    repos = list(projekt.repos.all())
    return render(
        request,
        "steuerung/akte_betrieb.html",
        _kontext(
            projekt,
            "betrieb",
            umgebungen=projekt.umgebungen.filter(aktiv=True),
            installationen=akte.installationen_aktuell(projekt),
            verlauf_installationen=Installation.objects.filter(projekt=projekt).select_related(
                "umgebung", "release"
            )[:30],
            releases=projekt.releases.filter(archiviert=False).prefetch_related("komponenten"),
            releases_archiv=projekt.releases.filter(archiviert=True),
            auftraege_offen=projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE),
            nachweise=Nachweis.objects.filter(
                projekt=projekt, art__in=(Nachweis.TEST, Nachweis.BETRIEB)
            ).select_related("umgebung", "funktion")[:40],
            einschraenkungen=Nachweis.objects.filter(
                projekt=projekt, ergebnis__in=("fehlgeschlagen", "teilweise")
            ).select_related("umgebung", "funktion")[:10],
            installation_form=InstallationForm(projekt=projekt, prefix="i"),
            nachweis_form=NachweisForm(projekt=projekt, prefix="n"),
            repos=[
                {
                    "v": v,
                    "abrufe": Abrufstand.objects.filter(verbindung=v),
                    "objekte": ExternesObjekt.objects.filter(verbindung=v).select_related(
                        "auftrag"
                    )[:40],
                    "aktionen": v.aktionen_log.all()[:20],
                }
                for v in repos
            ],
            github_eingerichtet=github.adapter() is not None,
        ),
    )


@login_required
def release_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = ReleaseForm(
        listen_zusammenfuehren(request.POST, "komponenten") if request.method == "POST" else None
    )
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        try:
            r = akte.release_anlegen(
                _akteur(request),
                projekt,
                kennung=d["kennung"],
                titel=d["titel"],
                beschreibung=d["beschreibung"],
                komponenten=form.komponentenliste(),
            )
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(
                request,
                f"Release {r.kennung} angelegt (Entwurf). Installiert ist damit noch nichts.",
            )
            return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#releases")
    return render(
        request,
        "steuerung/akte_formular.html",
        _kontext(
            projekt,
            "betrieb",
            form=form,
            titel="Release anlegen",
            erklaerung="Ein Release fasst Komponenten mit Commit oder Artefakthash zusammen. "
            "Installationen werden je Umgebung getrennt gemeldet.",
        ),
    )


@login_required
@require_POST
def release_zustand(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    projekt = _projekt(kennung)
    r = get_object_or_404(Release, pk=pk, projekt=projekt)
    _ausfuehren(
        request,
        "Release aktualisiert.",
        akte.release_zustand,
        _akteur(request),
        r,
        request.POST.get("zustand", ""),
        erwartete_revision=request.POST.get("revision"),
    )
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#releases")


@login_required
@require_POST
def installation_melden(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = InstallationForm(request.POST, projekt=projekt, prefix="i")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            "Installation gemeldet; sie bleibt „gemeldet“, bis du sie bestätigst.",
            akte.installation_melden,
            _akteur(request),
            projekt,
            umgebung=d["umgebung"],
            komponente=d["komponente"],
            version=d["version"],
            release=d["release"],
            installiert_am=d["installiert_am"],
            herkunft=d["herkunft"],
        )
    else:
        messages.error(request, "Installation: Umgebung, Komponente, Version und Herkunft angeben.")
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#installiert")


@login_required
@require_POST
def installation_bewerten(request: HttpRequest, kennung: str, pk: int, wahl: str) -> HttpResponse:
    projekt = _projekt(kennung)
    i = get_object_or_404(Installation, pk=pk, projekt=projekt)
    _ausfuehren(
        request,
        "Installation bewertet.",
        akte.installation_bewerten,
        _akteur(request),
        i,
        bestaetigt=wahl == "bestaetigen",
        grund=request.POST.get("grund", ""),
    )
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#installiert")


@login_required
@require_POST
def nachweis_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = NachweisForm(request.POST, projekt=projekt, prefix="n")
    zurueck = request.POST.get("zurueck", "")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            "Nachweis gespeichert.",
            akte.nachweis_melden,
            _akteur(request),
            projekt,
            art=d["art"],
            ergebnis=d["ergebnis"],
            version=d["version"],
            quelle=d["quelle"],
            umfang=d["umfang"],
            beobachtet_am=d["beobachtet_am"],
            umgebung=d["umgebung"],
            funktion=d["funktion"],
        )
    else:
        messages.error(request, "Nachweis: Art, Ergebnis, Version und Quelle angeben.")
    if zurueck.startswith("/projekt/") and "//" not in zurueck:
        return redirect(zurueck)
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#pruefungen")


# --- Wissen & Entscheidungen -----------------------------------------------------------------


@login_required
def wissen(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    zeigen_archiv = request.GET.get("archiv") == "1"
    unterlagen = projekt.unterlagen.filter(archiviert=zeigen_archiv).prefetch_related("versionen")
    zeilen = []
    for u in unterlagen:
        aktuell = u.versionen.first()
        zeilen.append({"u": u, "v": aktuell, "grundlage": akte.geltende_grundlage(u)})
    entscheidungen = []
    for e in Entscheidung.objects.filter(betroffene_projekte=projekt).distinct():
        entscheidungen.append(
            {
                "e": e,
                "uebernommen": e.bestaetigungen.filter(projekt=projekt, stand=e.stand).exists(),
                "weitere": e.betroffene_projekte.exclude(pk=projekt.pk).count(),
            }
        )
    return render(
        request,
        "steuerung/akte_wissen.html",
        _kontext(
            projekt,
            "wissen",
            zeilen=zeilen,
            entscheidungen=entscheidungen,
            zeigen_archiv=zeigen_archiv,
            pruefbeduerftig=akte.pruefbeduerftige_auftraege(projekt),
        ),
    )


@login_required
def unterlage_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    art = request.GET.get("art") or request.POST.get("art") or "text"
    if art not in ("text", "datei", "link"):
        art = "text"
    if art == "datei":
        form = UnterlageDateiForm(initial={"upload_schluessel": uuid.uuid4().hex})
        return _upload_formular(request, projekt, None, form)
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
                request, f"{u.kennung} „{u.titel}“ ist gespeichert (Version 1, Entwurf)."
            )
            return redirect("unterlage_detail", kennung=kennung, nummer=u.nummer)
    return render(
        request,
        "steuerung/akte_unterlage_neu.html",
        _kontext(projekt, "wissen", form=form, art=art),
    )


def _upload_formular(request, projekt, unterlage, form, status=200, fehler=""):
    return render(
        request,
        "steuerung/akte_unterlage_neu.html",
        _kontext(
            projekt,
            "wissen",
            form=form,
            art="datei",
            unterlage=unterlage,
            grenzen=_grenzen(),
            uploads_aktiv=_uploads_aktiv(),
            fehler=fehler,
        ),
        status=status,
    )


def _grenzen() -> dict:
    from django.conf import settings

    return {
        "datei_mib": settings.KIPM_UPLOAD_MAX // ablage.MIB,
        "dateien": settings.KIPM_UPLOAD_DATEIEN,
        "projekt_mib": settings.KIPM_PROJEKT_QUOTE // ablage.MIB,
    }


def _uploads_aktiv() -> bool:
    from django.conf import settings

    return settings.KIPM_UPLOADS_AKTIV


@csrf_exempt
@login_required
def hochladen(request: HttpRequest, kennung: str, nummer: int | None = None) -> HttpResponse:
    """Neue Datei-Unterlage oder ausdrücklich neue Version. Der Größenwächter muss vor dem Lesen
    des Formulars stehen; deshalb prüft erst die innere Funktion das CSRF-Token."""
    request.upload_handlers.insert(0, ablage.Grenzwaechter(request))
    projekt = _projekt(kennung)
    unterlage = None
    if nummer is not None:
        unterlage = _gefunden(akte.unterlage_holen, _akteur(request), projekt, nummer)
    return _hochladen(request, projekt, unterlage)


@csrf_protect
def _hochladen(request: HttpRequest, projekt: Projekt, unterlage: Unterlage | None) -> HttpResponse:
    """Neue Unterlagen: bis zu ``KIPM_UPLOAD_DATEIEN`` Dateien je Vorgang, je Datei eine eigene
    Unterlage. Neue Version einer Unterlage: genau eine Datei. Erst werden alle Dateien geprüft;
    ist eine abzuweisen, wird keine gespeichert."""
    from django.conf import settings

    mehrere = unterlage is None
    if request.method != "POST":
        form = UnterlageDateiForm(initial={"upload_schluessel": uuid.uuid4().hex}, mehrere=mehrere)
        return _upload_formular(request, projekt, unterlage, form)
    form = UnterlageDateiForm(request.POST, request.FILES, mehrere=mehrere)
    if getattr(request, "upload_zu_gross", False):
        fehler = (
            f"Eine Datei ist größer als {_grenzen()['datei_mib']} MiB; nichts wurde gespeichert."
        )
        neu = UnterlageDateiForm(initial={"upload_schluessel": uuid.uuid4().hex}, mehrere=mehrere)
        return _upload_formular(request, projekt, unterlage, neu, 413, fehler=fehler)
    if not form.is_valid():
        return _upload_formular(request, projekt, unterlage, form, 400)
    dateien = form.cleaned_data["datei"]
    grenze = 1 if unterlage is not None else settings.KIPM_UPLOAD_DATEIEN
    # Gezählt wird, was gesendet wurde; das Einzelfeld einer Version sähe nur die erste Datei.
    if len(request.FILES.getlist("datei")) > grenze:
        form.add_error(
            None,
            "Eine neue Version besteht aus genau einer Datei."
            if unterlage is not None
            else f"Höchstens {grenze} Dateien je Vorgang; nichts wurde gespeichert.",
        )
        return _upload_formular(request, projekt, unterlage, form, 400)
    d = form.cleaned_data
    eingaenge = []
    try:
        for datei in dateien:
            try:
                eingaenge.append(ablage.annehmen(datei))
            except ablage.Abgewiesen as fehler:
                raise dienst.DienstFehler(f"{datei.name}: {fehler}") from fehler
    except dienst.DienstFehler as fehler:
        for e in eingaenge:
            e.verwerfen()
        form.add_error(None, f"{fehler} Nichts wurde gespeichert.")
        return _upload_formular(request, projekt, unterlage, form, 400)
    gespeichert, fehlgeschlagen = [], []
    for nr, eingang in enumerate(eingaenge):
        try:
            v = akte.datei_aufnehmen(
                _akteur(request),
                projekt,
                eingang,
                # Je Datei ein eigener, wiederholbarer Schlüssel: ein erneutes Absenden legt
                # nichts doppelt an.
                upload_schluessel=d["upload_schluessel"]
                if nr == 0
                else f"{d['upload_schluessel']}-{nr}",
                unterlage=unterlage,
                erwartete_nr=int(request.POST["erwartete_nr"])
                if request.POST.get("erwartete_nr")
                else None,
                titel=d["titel"] if len(eingaenge) == 1 else "",
                beschreibung=d["beschreibung"],
            )
        except dienst.DienstFehler as fehler:
            fehlgeschlagen.append(f"{eingang.originalname}: {fehler}")
            continue
        gespeichert.append(v)
    if fehlgeschlagen and not gespeichert:
        form.add_error(None, " ".join(fehlgeschlagen))
        return _upload_formular(request, projekt, unterlage, form, 400)
    for v in gespeichert:
        messages.success(
            request,
            f"{v.unterlage.kennung} Version {v.nummer} gespeichert · Prüfung: "
            f"{v.get_pruefung_display()}" + (f" ({v.pruefhinweis})" if v.pruefhinweis else ""),
        )
    for text in fehlgeschlagen:
        messages.error(request, f"Nicht gespeichert: {text}")
    if len(gespeichert) == 1:
        return redirect(
            "unterlage_detail", kennung=projekt.kennung, nummer=gespeichert[0].unterlage.nummer
        )
    return redirect(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))


@login_required
def unterlage_detail(request: HttpRequest, kennung: str, nummer: int) -> HttpResponse:
    projekt = _projekt(kennung)
    u = _gefunden(akte.unterlage_holen, _akteur(request), projekt, nummer)
    aktuell = u.versionen.first()
    return render(
        request,
        "steuerung/akte_unterlage.html",
        _kontext(
            projekt,
            "wissen",
            u=u,
            aktuell=aktuell,
            versionen=u.versionen.all(),
            grundlage=akte.geltende_grundlage(u),
            zuordnungen=[
                z
                for v in u.versionen.all()
                for z in v.zuordnungen.select_related(
                    "funktion", "auftrag", "entscheidung", "release"
                )
            ],
            funktionen=projekt.funktionen.exclude(planung=Funktion.VERWORFEN),
            auftraege=projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE),
            entscheidungen=Entscheidung.objects.filter(betroffene_projekte=projekt)
            .exclude(zustand=Entscheidung.ZURUECKGEZOGEN)
            .distinct(),
            releases=projekt.releases.filter(archiviert=False),
            text_form=UnterlageTextForm(
                initial={"titel": u.titel, "text": aktuell.text if aktuell else ""}
            )
            if u.art == Unterlage.TEXT
            else None,
            upload_schluessel=uuid.uuid4().hex,
            grenzen=_grenzen(),
            uploads_aktiv=_uploads_aktiv(),
        ),
    )


@login_required
@require_POST
def unterlage_aktion(request: HttpRequest, kennung: str, nummer: int, aktion: str) -> HttpResponse:
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    u = _gefunden(akte.unterlage_holen, akteur, projekt, nummer)
    p = request.POST

    def version():
        return _gefunden(akte.version_holen, akteur, projekt, nummer, int(p.get("version") or 0))

    if aktion == "text":
        _ausfuehren(
            request,
            "Neue Textversion gespeichert (Entwurf).",
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
    elif aktion == "rolle":
        _ausfuehren(
            request, "Rolle gesetzt.", akte.rolle_setzen, akteur, version(), p.get("rolle", "")
        )
    elif aktion == "ki":
        _ausfuehren(
            request, "KI-Freigabe geändert.", akte.ki_freigabe, akteur, u, p.get("frei") == "1"
        )
    elif aktion == "archiv":
        _ausfuehren(
            request, "Gespeichert.", akte.unterlage_archivieren, akteur, u, p.get("archiv") == "1"
        )
    elif aktion == "pruefen":
        _ausfuehren(
            request, "Prüfung erneut ausgeführt.", akte.pruefung_wiederholen, akteur, version()
        )
    elif aktion == "zuordnen":
        ziel = p.get("ziel", "")
        art, _, pk = ziel.partition(":")
        felder = {}
        if art == "f":
            felder["funktion"] = get_object_or_404(Funktion, pk=pk or 0, projekt=projekt)
        elif art == "a":
            felder["auftrag"] = get_object_or_404(Auftrag, pk=pk or 0, projekt=projekt)
        elif art == "e":
            felder["entscheidung"] = get_object_or_404(
                Entscheidung, pk=pk or 0, betroffene_projekte=projekt
            )
        elif art == "r":
            felder["release"] = get_object_or_404(Release, pk=pk or 0, projekt=projekt)
        else:
            messages.error(request, "Bitte ein Ziel wählen.")
            return redirect("unterlage_detail", kennung=kennung, nummer=nummer)
        _ausfuehren(
            request,
            "Zugeordnet.",
            akte.zuordnen,
            akteur,
            version(),
            zweck=p.get("zweck", ""),
            **felder,
        )
    else:
        raise Http404
    return redirect("unterlage_detail", kennung=kennung, nummer=nummer)


@login_required
def unterlage_datei(request: HttpRequest, kennung: str, nummer: int, vnr: int) -> HttpResponse:
    """Download nur nach erneuter Berechtigungsprüfung, nur geprüfte Dateien, immer als Anhang."""
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


# --- Verlauf, Suche, Kontext -----------------------------------------------------------------


@login_required
def verlauf_stand(request: HttpRequest, kennung: str, ereignis_id: int) -> HttpResponse:
    """Damaliger Stand eines Objekts aus der zum Ereignis gespeicherten Version."""
    projekt = _projekt(kennung)
    e = get_object_or_404(
        Ereignis.objects.select_related("version"), pk=ereignis_id, projekt=projekt
    )
    vorher = None
    if e.version is not None:
        vorher = Objektversion.objects.filter(
            objekt_typ=e.version.objekt_typ,
            objekt_id=e.version.objekt_id,
            nummer=e.version.nummer - 1,
        ).first()
    return render(
        request,
        "steuerung/akte_stand.html",
        _kontext(
            projekt,
            "verlauf",
            e=e,
            vorher=vorher,
            felder=sorted(
                set((e.version.daten if e.version else {}) | (vorher.daten if vorher else {}))
            ),
        ),
    )


@login_required
def projekt_suche(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    return render(
        request,
        "steuerung/akte_suche.html",
        _kontext(projekt, "suche", treffer=akte.suche(projekt, request.GET.get("q", ""))),
    )


@login_required
def ki_kontext(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    auftrag = None
    nr = request.GET.get("auftrag", "").upper().removeprefix("A-")
    if nr.isdigit():
        auftrag = Auftrag.objects.filter(pk=int(nr), projekt=projekt).first()
        if auftrag is None:
            raise Http404
    k = akte.kontext_erzeugen(_akteur(request), projekt, auftrag=auftrag)
    if request.GET.get("format") == "json":
        return HttpResponse(
            json.dumps(
                {"manifest": k.manifest, "hash": k.hash, "teile": k.teile},
                ensure_ascii=False,
                indent=2,
            ),
            content_type="application/json; charset=utf-8",
        )
    return render(
        request,
        "steuerung/akte_kontext.html",
        _kontext(
            projekt,
            "ueberblick",
            k=k,
            auftrag=auftrag,
            auftraege=projekt.auftraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE),
        ),
    )


# --- Projekteinstellungen: Umgebungen, Module, GitHub ----------------------------------------


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
    return render(
        request,
        "steuerung/projekt_einstellungen.html",
        _kontext(
            projekt,
            "einstellungen",
            arbeitsorte_aktiv=projekt.arbeitsorte.filter(aktiv=True),
            arbeitsorte_archiv=projekt.arbeitsorte.filter(aktiv=False),
            bindungen=projekt.bindungen.all(),
            ki=ChatBindung.objects.filter(letztes_projekt=projekt).order_by("-zuletzt_aktiv")[:5],
            arbeitsort_form=ArbeitsortForm(prefix="ort"),
            modul_form=ModulForm(prefix="modul"),
            modulbaum=baum,
            modularchiv=[m for m in module if not m.aktiv],
            umgebungen_aktiv=projekt.umgebungen.filter(aktiv=True),
            umgebungen_archiv=projekt.umgebungen.filter(aktiv=False),
            umgebungsarten=Umgebung.ARTEN,
            umgebung_form=UmgebungForm(prefix="u"),
            repos=projekt.repos.all(),
            repo_form=RepoForm(prefix="r"),
            github_eingerichtet=github.adapter() is not None,
        ),
    )


@login_required
@require_POST
def umgebung_neu(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = UmgebungForm(request.POST, prefix="u")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"Umgebung „{d['name']}“ angelegt.",
            akte.umgebung_anlegen,
            _akteur(request),
            projekt,
            name=d["name"],
            art=d["art"],
            beschreibung=d["beschreibung"],
            url=d["url"],
        )
    else:
        messages.error(
            request, "Umgebung: Name und Art sind erforderlich, die Adresse muss gültig sein."
        )
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#umgebungen")


@login_required
@require_POST
def repo_verbinden(request: HttpRequest, kennung: str) -> HttpResponse:
    projekt = _projekt(kennung)
    form = RepoForm(request.POST, prefix="r")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"{d['name']} ist verbunden.",
            abgleich.verbindung_anlegen,
            _akteur(request),
            projekt,
            name=d["name"],
            schreibend=d["schreibend"],
            aktionen="\n".join(d["aktionen"]),
            pfade=d["pfade"],
        )
    else:
        messages.error(request, "Repository: Name besitzer/repository angeben.")
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#github")


@login_required
@require_POST
def repo_aktion(request: HttpRequest, kennung: str, pk: int, aktion: str) -> HttpResponse:
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    v = _gefunden(abgleich.verbindung_holen, akteur, projekt, pk)
    if aktion == "aktualisieren":
        bericht = _ausfuehren(request, "", abgleich.aktualisieren, akteur, v)
        if isinstance(bericht, dict):
            fehler = [f"{b}: {w['fehler']}" for b, w in bericht.items() if "fehler" in w]
            if fehler:
                messages.error(
                    request,
                    "GitHub teilweise nicht lesbar; ältere Daten bleiben sichtbar. "
                    + " · ".join(fehler),
                )
            else:
                messages.success(
                    request,
                    "GitHub aktualisiert: "
                    + ", ".join(
                        f"{b} {'unverändert' if w['status'] == 304 else str(w['geaendert']) + ' neu/geändert'}"
                        for b, w in bericht.items()
                    ),
                )
        return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#github")
    if aktion == "widerrufen":
        _ausfuehren(
            request,
            f"Verbindung zu {v.name} widerrufen.",
            abgleich.verbindung_widerrufen,
            akteur,
            v,
        )
        return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#github")
    raise Http404


@login_required
def github_aktion_neu(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    projekt = _projekt(kennung)
    akteur = _akteur(request)
    v = _gefunden(abgleich.verbindung_holen, akteur, projekt, pk)
    form = GitHubAktionForm(
        request.POST or None, initial={"art": request.GET.get("art", "issue_anlegen")}
    )
    if request.method == "POST" and form.is_valid():
        try:
            a = abgleich.aktion_vorbereiten(akteur, v, form.cleaned_data["art"], form.nutzlast())
        except dienst.DienstFehler as fehler:
            form.add_error(None, str(fehler))
        else:
            messages.success(request, "Vorschau erstellt. Bei GitHub ist noch nichts geändert.")
            return redirect("github_aktion", kennung=kennung, aktions_id=a.aktions_id)
    return render(
        request,
        "steuerung/akte_formular.html",
        _kontext(
            projekt,
            "betrieb",
            form=form,
            titel=f"GitHub-Änderung vorbereiten: {v.name}",
            erklaerung="Du siehst zuerst eine genaue Vorschau. Erst deine Bestätigung "
            "und danach „Ausführen“ ändern etwas bei GitHub. Keine Merges, Releases oder Deployments.",
        ),
    )


def _aktion(projekt: Projekt, aktions_id: str) -> ExterneAktion:
    return get_object_or_404(
        ExterneAktion.objects.select_related("verbindung"), aktions_id=aktions_id, projekt=projekt
    )


@login_required
def github_aktion(request: HttpRequest, kennung: str, aktions_id: str) -> HttpResponse:
    projekt = _projekt(kennung)
    a = _aktion(projekt, aktions_id)
    import difflib

    diff = ""
    if a.art == "dokument_pr":
        alt = (a.basis.get("alter_inhalt") or "").splitlines()
        diff = "\n".join(
            difflib.unified_diff(
                alt, a.nutzlast.get("inhalt", "").splitlines(), "vorher", "nachher", lineterm=""
            )
        )
    elif a.art == "issue_aendern":
        teile = []
        for feld in ("titel", "text", "zustand"):
            if feld in a.nutzlast:
                teile.extend(
                    difflib.unified_diff(
                        str(a.basis.get(feld, "")).splitlines(),
                        str(a.nutzlast[feld]).splitlines(),
                        f"{feld} vorher",
                        f"{feld} nachher",
                        lineterm="",
                    )
                )
        diff = "\n".join(teile)
    from django.utils import timezone

    return render(
        request,
        "steuerung/akte_github_aktion.html",
        _kontext(
            projekt,
            "betrieb",
            a=a,
            diff=diff,
            bestaetigung_offen=a.zustand == ExterneAktion.BESTAETIGT
            and a.bestaetigt_am
            and a.bestaetigt_am >= timezone.now() - abgleich.BESTAETIGUNG_GUELTIG,
        ),
    )


@login_required
@require_POST
def github_aktion_schritt(
    request: HttpRequest, kennung: str, aktions_id: str, schritt: str
) -> HttpResponse:
    projekt = _projekt(kennung)
    a = _aktion(projekt, aktions_id)
    akteur = _akteur(request)
    p = request.POST
    if schritt == "bestaetigen":
        _ausfuehren(
            request,
            "Bestätigt. Ausführen ist 15 Minuten lang möglich.",
            abgleich.aktion_bestaetigen,
            akteur,
            a,
            revision=int(p.get("revision") or 0),
            nutzlast_hash=p.get("hash", ""),
        )
    elif schritt in ("ausfuehren", "fortsetzen"):
        ergebnis = _ausfuehren(
            request, "", abgleich.aktion_ausfuehren, akteur, a, fortsetzen=schritt == "fortsetzen"
        )
        if ergebnis:
            a.refresh_from_db()
            (messages.success if a.zustand == ExterneAktion.ERFOLGREICH else messages.error)(
                request,
                f"GitHub-Aktion: {a.get_zustand_display()}"
                + (f" – {a.fehler}" if a.fehler else ""),
            )
    elif schritt == "abgleichen":
        _ausfuehren(request, "Abgeglichen.", abgleich.aktion_abgleichen, akteur, a)
    elif schritt == "nicht_ausgefuehrt":
        _ausfuehren(
            request,
            "Geklärt; neuer Versuch braucht eine neue Bestätigung.",
            abgleich.aktion_nicht_ausgefuehrt,
            akteur,
            a,
            p.get("grund", ""),
        )
    elif schritt == "verwerfen":
        _ausfuehren(request, "Verworfen.", abgleich.aktion_verwerfen, akteur, a)
    else:
        raise Http404
    return redirect("github_aktion", kennung=kennung, aktions_id=aktions_id)


# --- Pflege: Umgebungen, Releases, Meilensteine, GitHub-Verknüpfung, Profilentwurf ----------


@login_required
@require_POST
def umgebung_pflegen(request: HttpRequest, kennung: str, pk: int, wahl: str) -> HttpResponse:
    """Umgebung übernehmen (Entwurf einer KI), ändern, archivieren oder zurückholen."""
    projekt = _projekt(kennung)
    u = get_object_or_404(Umgebung, pk=pk, projekt=projekt)
    akteur = _akteur(request)
    if wahl == "bestaetigen":
        _ausfuehren(
            request,
            f"Umgebung „{u.name}“ übernommen.",
            akte.umgebung_bestaetigen,
            akteur,
            u,
            erwartete_revision=request.POST.get("revision") or None,
        )
    elif wahl == "aendern":
        form = UmgebungForm(request.POST, prefix=f"u{u.pk}")
        if form.is_valid():
            _ausfuehren(
                request,
                "Umgebung gespeichert.",
                akte.umgebung_aendern,
                akteur,
                u,
                erwartete_revision=request.POST.get("revision") or None,
                **form.cleaned_data,
            )
        else:
            messages.error(
                request, "Umgebung: Name und Art sind erforderlich, die Adresse muss gültig sein."
            )
    elif wahl in ("archivieren", "zurueckholen"):
        _ausfuehren(
            request,
            "Umgebung archiviert." if wahl == "archivieren" else "Umgebung wieder aktiv.",
            akte.umgebung_aendern,
            akteur,
            u,
            erwartete_revision=request.POST.get("revision") or None,
            aktiv=wahl == "zurueckholen",
        )
    else:
        raise Http404
    return redirect(reverse("projekt_einstellungen", kwargs={"kennung": kennung}) + "#umgebungen")


@login_required
@require_POST
def release_pflegen(request: HttpRequest, kennung: str, pk: int, wahl: str) -> HttpResponse:
    """Release bearbeiten (Komponenten nur im Entwurf), archivieren oder zurückholen."""
    projekt = _projekt(kennung)
    r = get_object_or_404(Release, pk=pk, projekt=projekt)
    revision = request.POST.get("revision") or None
    if wahl == "aendern":
        felder = {
            "titel": request.POST.get("titel", ""),
            "beschreibung": request.POST.get("beschreibung", ""),
        }
        if r.zustand == Release.ENTWURF and "komponenten" in request.POST:
            form = ReleaseForm(
                {"kennung": r.kennung, "komponenten": request.POST.get("komponenten", "")}
            )
            form.is_valid()
            felder["komponenten"] = (
                form.komponentenliste() if "komponenten" in form.cleaned_data else []
            )
        _ausfuehren(
            request,
            f"Release {r.kennung} gespeichert.",
            akte.release_aendern,
            _akteur(request),
            r,
            erwartete_revision=revision,
            **felder,
        )
    elif wahl in ("archivieren", "zurueckholen"):
        _ausfuehren(
            request,
            f"Release {r.kennung} archiviert."
            if wahl == "archivieren"
            else "Release wieder aktiv.",
            akte.release_aendern,
            _akteur(request),
            r,
            erwartete_revision=revision,
            archiviert=wahl == "archivieren",
        )
    else:
        raise Http404
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#releases")


@login_required
@require_POST
def meilenstein_bearbeiten(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    projekt = _projekt(kennung)
    m = get_object_or_404(Meilenstein, pk=pk, projekt=projekt)
    form = MeilensteinForm(request.POST, projekt=projekt, prefix=f"m{m.pk}")
    if form.is_valid():
        d = form.cleaned_data
        _ausfuehren(
            request,
            f"Meilenstein „{d['name']}“ gespeichert.",
            akte.meilenstein_bearbeiten,
            _akteur(request),
            m,
            erwartete_revision=request.POST.get("revision") or None,
            name=d["name"],
            ziel=d["ziel"],
            zielversion=d["zielversion"],
            termin=d["termin"],
            funktionen=list(d["funktionen"]),
        )
    else:
        messages.error(request, "Meilenstein: Name ist erforderlich, der Termin ein Datum.")
    return redirect(reverse("akte_funktionen", kwargs={"kennung": kennung}) + "#meilensteine")


@login_required
@require_POST
def github_objekt_verknuepfen(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    """GitHub-Issue oder Pull Request mit einem Auftrag dieses Projekts verknüpfen oder lösen."""
    projekt = _projekt(kennung)
    o = get_object_or_404(ExternesObjekt, pk=pk, verbindung__projekt=projekt)
    auftrag = None
    if request.POST.get("auftrag"):
        auftrag = get_object_or_404(Auftrag, pk=request.POST["auftrag"], projekt=projekt)
    _ausfuehren(
        request,
        f"Mit {auftrag.nummer} verknüpft." if auftrag else "Verknüpfung gelöst.",
        abgleich.objekt_verknuepfen,
        _akteur(request),
        o,
        auftrag,
    )
    return redirect(reverse("akte_betrieb", kwargs={"kennung": kennung}) + "#github")


@login_required
@require_POST
def profil_entwurf(request: HttpRequest, kennung: str, wahl: str) -> HttpResponse:
    projekt = _projekt(kennung)
    if wahl not in ("uebernehmen", "verwerfen"):
        raise Http404
    _ausfuehren(
        request,
        "Profilentwurf übernommen." if wahl == "uebernehmen" else "Profilentwurf verworfen.",
        akte.profil_entwurf_uebernehmen,
        _akteur(request),
        projekt,
        uebernehmen=wahl == "uebernehmen",
        erwarteter_entwurf=request.POST.get("entwurf", ""),
    )
    return redirect("projekt_detail", kennung=kennung)


@login_required
@require_POST
def nachweis_bestaetigen(request: HttpRequest, kennung: str, pk: int) -> HttpResponse:
    """Gemeldete Umsetzung oder Prüfung als Mensch bestätigen; nie die eigene Meldung."""
    projekt = _projekt(kennung)
    n = get_object_or_404(Nachweis, pk=pk, projekt=projekt)
    _ausfuehren(request, "Meldung bestätigt.", akte.nachweis_bestaetigen, _akteur(request), n)
    return redirect(reverse("projekt_detail", kwargs={"kennung": kennung}) + "#modulstand")

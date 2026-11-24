from django import forms
from django.db import models

from .models import (
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Meilenstein,
    Modul,
    Projekt,
)

# Stufen für die Anzeige an jedem Feld: Pflicht, für Freigabe nötig, optional.
PFLICHT = "pflicht"
FREIGABE = "freigabe"
OPTIONAL = "optional"


def _stufen(form: forms.Form, **stufen: str) -> None:
    for name, feld in form.fields.items():
        feld.stufe = stufen.get(name, PFLICHT if feld.required else OPTIONAL)


def _liste(feld: forms.Field, platzhalter: str) -> forms.Field:
    """Feld, das in der Oberfläche als Liste mit Plus-Knopf erscheint (ein Eintrag je Zeile)."""
    feld.liste = True
    feld.platzhalter = platzhalter
    return feld


class NamensAuswahl(forms.ModelChoiceField):
    """Zeigt den verständlichen Namen statt der technischen Kennung."""

    def label_from_instance(self, obj):
        return getattr(obj, "name", None) or getattr(obj, "get_username", lambda: str(obj))()


class ProjektForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="z. B. Nexus")
    beschreibung = forms.CharField(
        label="Beschreibung",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Ein Satz, worum es geht.",
    )
    module = _liste(
        forms.CharField(label="Module", required=False, widget=forms.HiddenInput),
        "Modulname, z. B. Core",
    )
    kuerzel = forms.CharField(
        label="Kürzel für die Nummern",
        max_length=8,
        required=False,
        help_text="Steht vorn in jeder Nummer, z. B. NEX in NEX-12. Später nicht mehr änderbar; "
        "leer lassen, dann wird es aus dem Namen gebildet.",
    )
    kennung = forms.SlugField(
        label="Kennung für die Adresse",
        max_length=40,
        required=False,
        help_text="Kurz, ohne Leerzeichen; leer lassen, dann wird sie aus dem Namen gebildet.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class ModulForm(forms.Form):
    name = forms.CharField(label="Name des Moduls", max_length=120)


class ArbeitsortForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="z. B. Nexus auf meinem PC")
    pfad = forms.CharField(
        label="Ordner auf dem PC",
        max_length=300,
        required=False,
        help_text=(
            "z. B. W:\\Projekte\\Nexus. Arbeitet eine KI in diesem Ordner, weiß sie das Projekt."
        ),
    )
    rechner = forms.CharField(
        label="PC", max_length=120, required=False, help_text="z. B. Arbeitsplatz"
    )
    repository = forms.URLField(
        label="Repository",
        max_length=300,
        required=False,
        assume_scheme="https",
        help_text=(
            "z. B. https://github.com/FredHeidinger/nexus; macht Dateien in Ergebnissen anklickbar."
        ),
    )
    modul = NamensAuswahl(
        label="Modul",
        queryset=Modul.objects.none(),
        required=False,
        empty_label="Gesamtes Projekt",
        help_text="Eine KI-Sitzung in diesem Ordner sieht beim Start nur dieses Modul.",
    )

    def __init__(self, *args, projekt: Projekt | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        module = projekt.module.filter(aktiv=True) if projekt is not None else Modul.objects.none()
        if module.exists():
            self.fields["modul"].queryset = module
        else:
            del self.fields["modul"]
        _stufen(self)


class WunschForm(forms.Form):
    """Auftrag oder Issue anlegen; geplant wird danach."""

    typ = forms.ChoiceField(
        label="Art",
        choices=Auftrag.TYPEN,
        initial=Auftrag.TYP_AUFTRAG,
        widget=forms.RadioSelect,
        help_text=(
            "Issue: ein Fehler oder Problem. Auftrag: etwas soll gebaut oder geändert werden."
        ),
    )
    projekt = NamensAuswahl(label="Projekt", queryset=Projekt.objects.filter(aktiv=True))
    modul = NamensAuswahl(label="Modul", queryset=Modul.objects.none(), required=False)
    titel = forms.CharField(label="Titel", max_length=200)
    beschreibung = forms.CharField(
        label="Beschreibung",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="In deinen Worten. Ziel, Umfang und Kriterien folgen beim Planen.",
    )
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO, initial="mittel")
    # Ein Datumsfeld im Browser versteht nur das ISO-Format; lokalisiert bliebe es leer.
    faellig = forms.DateField(
        label="Fällig am",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Ab diesem Tag steht der Eintrag oben in „Wartet auf dich“.",
    )
    erzwingen = forms.BooleanField(
        label="Trotz gleichem Titel anlegen (kein Doppel)", required=False
    )

    def __init__(self, *args, projekt: Projekt | None = None, typ: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        if typ in dict(Auftrag.TYPEN):
            self.fields["typ"].initial = typ
        aktive = Projekt.objects.filter(aktiv=True)
        if projekt is None and aktive.count() == 1:
            projekt = aktive.first()
        if projekt is not None:
            self.fields["projekt"].initial = projekt
        projekt_id = self.data.get("projekt") or (projekt.pk if projekt else None)
        if projekt_id:
            self.fields["modul"].queryset = Modul.objects.filter(projekt_id=projekt_id, aktiv=True)
        else:
            self.fields["modul"].help_text = (
                "Zuerst das Projekt wählen und speichern; das Modul lässt sich beim "
                "Planen nachtragen."
            )
        _stufen(self)


class ArbeitspaketForm(forms.Form):
    """Konkretisieren: erzeugt eine neue Revision."""

    titel = forms.CharField(label="Titel", max_length=200)
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO)
    modul = NamensAuswahl(label="Modul", queryset=Modul.objects.none(), required=False)
    meilenstein = NamensAuswahl(
        label="Meilenstein",
        queryset=Meilenstein.objects.none(),
        required=False,
        empty_label="kein Meilenstein",
    )
    beschreibung = forms.CharField(
        label="Wunsch in Alltagssprache", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    ziel = forms.CharField(
        label="Ziel",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Ein beobachtbares Ergebnis in ein bis drei Sätzen.",
    )
    nicht_ziele = forms.CharField(
        label="Nicht-Ziele",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Was ausdrücklich nicht gemacht wird; sonst „keine“.",
    )
    arbeitsort = NamensAuswahl(
        label="Arbeitsort", queryset=Arbeitsort.objects.none(), required=False
    )
    schreibumfang = _liste(
        forms.CharField(
            label="Schreibumfang",
            required=False,
            widget=forms.HiddenInput,
            help_text="Welche Dateien oder Ordner der Bearbeiter ändern darf.",
        ),
        "Pfad oder Muster, z. B. src/auth/** oder docs/README.md",
    )
    quellen = forms.CharField(
        label="Quellen und Verträge",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Verweise mit Revisionsstand.",
    )
    abnahmekriterien = _liste(
        forms.CharField(
            label="Abnahmekriterien",
            required=False,
            widget=forms.HiddenInput,
            help_text="Jedes Kriterium ist eine prüfbare Aussage.",
        ),
        "z. B. Falsches Passwort zeigt einen verständlichen Hinweis",
    )
    anlass = forms.CharField(label="Anlass der Änderung", max_length=200, required=False)

    def __init__(self, *args, auftrag: Auftrag, lieferketten=(), **kwargs):
        super().__init__(*args, **kwargs)
        if lieferketten:
            # Nur wenn die neue Revision Lieferketten trennt (lieferungen.betroffene_ketten).
            self.fields["lieferketten_bestaetigt"] = forms.BooleanField(
                label="Ja, Lieferketten trennen: Die oben genannten Lieferbedarfe verwaisen.",
                help_text="Wartende Aufträge bleiben blockiert, bis ihr Bedarf neu gebunden ist.",
            )
        self.fields["modul"].queryset = Modul.objects.filter(projekt=auftrag.projekt, aktiv=True)
        meilensteine = Meilenstein.objects.filter(projekt=auftrag.projekt)
        self.fields["meilenstein"].queryset = meilensteine
        if not meilensteine.exists():
            del self.fields["meilenstein"]
        self.fields["arbeitsort"].queryset = Arbeitsort.objects.filter(
            projekt=auftrag.projekt, aktiv=True
        )
        _stufen(
            self,
            ziel=FREIGABE,
            nicht_ziele=FREIGABE,
            arbeitsort=FREIGABE,
            schreibumfang=FREIGABE,
            abnahmekriterien=FREIGABE,
        )


class EigenesErgebnisForm(forms.Form):
    zusammenfassung = forms.CharField(
        label="Zusammenfassung", widget=forms.Textarea(attrs={"rows": 3})
    )
    dateien = _liste(
        forms.CharField(label="Geänderte Dateien mit Revision", widget=forms.HiddenInput),
        "z. B. src/auth/login.py@9c2e7b1",
    )
    pruefungen = _liste(
        forms.CharField(
            label="Bestandene Prüfungen",
            required=False,
            widget=forms.HiddenInput,
            help_text="Leer lassen, wenn es keine automatischen Tests gibt; der Auftrag wird dann "
            "„ohne automatische Tests“ erledigt.",
        ),
        "z. B. pytest tests/auth: 12 bestanden",
    )
    fehlgeschlagen = _liste(
        forms.CharField(
            label="Fehlgeschlagene Prüfungen",
            required=False,
            widget=forms.HiddenInput,
            help_text="Eine fehlgeschlagene Prüfung blockiert den Auftrag mit diesem Grund.",
        ),
        "z. B. pytest tests/auth: 2 fehlgeschlagen",
    )
    luecken = forms.CharField(
        label="Bekannte Lücken", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


# --- Projektakte -------------------------------------------------------------------------------


def _text(label, zeilen=3, hilfe="", pflicht=False):
    return forms.CharField(
        label=label,
        required=pflicht,
        help_text=hilfe,
        widget=forms.Textarea(attrs={"rows": zeilen}),
    )


class EntscheidungEintragForm(forms.Form):
    stand = forms.IntegerField(widget=forms.HiddenInput, required=False)
    entscheidung = forms.CharField(
        label="Entscheidung",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Was gilt, in einem Satz, z. B. „Es gibt keine Nutzerabnahme.“",
    )
    warum = _text("Warum", 3, "Begründung, damit spätere Chats sie verstehen.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class ProfilForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
    beschreibung = _text("Beschreibung", 5, "Worum es im Projekt geht; erscheint im Überblick.")
    zweck = _text("Zweck", 3, "Was soll das Projekt leisten? Zwei bis vier Sätze.")
    zielgruppe = _text("Zielgruppe", 2, "Für wen ist es gedacht?")
    nicht_ziele = _text("Nicht-Ziele", 2, "Was das Projekt ausdrücklich nicht tun soll.")
    phase = forms.CharField(
        label="Phase", max_length=60, required=False, help_text="z. B. Aufbau, Erprobung, Betrieb"
    )
    entwicklungsstufe = _text(
        "Nächste Entwicklungsstufe",
        3,
        "Von dir festgelegtes nächstes Ziel; keine KI-Priorisierung.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class MeilensteinForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    name = forms.CharField(
        label="Name", max_length=160, help_text="z. B. Erste Version für den Alltag"
    )
    # Ein Datumsfeld im Browser versteht nur das ISO-Format; lokalisiert bliebe es leer.
    datum = forms.DateField(
        label="Datum",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class UnterlageTextForm(forms.Form):
    titel = forms.CharField(label="Titel", max_length=200)
    beschreibung = forms.CharField(label="Kurzer Zweck", max_length=300, required=False)
    text = forms.CharField(label="Text", widget=forms.Textarea(attrs={"rows": 14}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class UnterlageLinkForm(forms.Form):
    titel = forms.CharField(label="Titel", max_length=200)
    beschreibung = forms.CharField(label="Kurzer Zweck", max_length=300, required=False)
    uri = forms.URLField(label="Adresse", max_length=500, assume_scheme="https")
    quellversion = forms.CharField(
        label="Fixierter Stand",
        max_length=80,
        required=False,
        help_text="z. B. Commit oder Datum; leer = veränderliche Quelle",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class GitHubSchluesselForm(forms.Form):
    schluessel = forms.CharField(
        label="Lese-Schlüssel",
        max_length=1000,
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


def listen_zusammenfuehren(post, *felder: str):
    """Einträge der Plus-Listen (``<feld>_eintrag``) zu Zeilen des Hauptfelds zusammenführen."""
    daten = post.copy()
    for feld in felder:
        eintraege = [z.strip() for z in post.getlist(f"{feld}_eintrag") if z.strip()]
        if eintraege or feld not in post:
            daten[feld] = "\n".join(eintraege)
    return daten


class StufenregelForm(forms.Form):
    """Stufenregel für die Planung (KIPM-31); nur Fred speichert sie."""

    zugang = NamensAuswahl(
        label="Welcher Zugang plant",
        queryset=ChatBindung.objects.none(),
        help_text="PC-Zugang der Planung, z. B. ChatGPT Desktop (Codex).",
    )
    stufe_a = forms.BooleanField(
        label="Stufe A: fertig geplante Aufträge freigeben und Lieferkanten nachtragen",
        required=False,
    )
    stufe_b = forms.BooleanField(
        label="Stufe B: technische Entscheidungen mit geprüfter Fundstelle buchen",
        required=False,
        help_text="Ziel, Kosten, Rechte, Cloud/Microsoft, Sicherheit, Daten, Neuschnitt und "
        "Löschung bleiben immer bei dir.",
    )
    gueltig_bis = forms.DateField(
        label="Gültig bis",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Höchstens 90 Tage; der Tag zählt bis 23:59.",
    )
    masterspec_bezeichnung = forms.CharField(
        label="Gebundene MasterSpec",
        max_length=200,
        required=False,
        help_text="z. B. SOURCE-MASTER-SPEC-BASELINE-005; leer = keine",
    )
    masterspec_sha256 = forms.CharField(
        label="SHA-256 der MasterSpec",
        max_length=64,
        required=False,
        help_text="64 Hexzeichen, wie in der Quellenbindung des Projekts",
    )

    def __init__(self, *args, projekt=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["zugang"].queryset = ChatBindung.objects.filter(
            widerrufen_am__isnull=True, art=ChatBindung.ART_PC
        )
        _stufen(self)


class VollmachtForm(forms.Form):
    """Vollmacht für einen technischen Standardfall; nur Fred erteilt sie."""

    aktion = forms.ChoiceField(
        label="Aktion",
        choices=[("wiederaufnahme", "Wiederaufnahme beim bisherigen Bearbeiter")],
        help_text="Setzt einen blockierten Auftrag beim selben Bearbeiter fort.",
    )
    ausfuehrende_bindung = NamensAuswahl(
        label="Wer darf ausführen",
        queryset=ChatBindung.objects.none(),
        help_text="PC-Zugang der Automation, z. B. Claude Desktop.",
    )
    bearbeiter_bindung = NamensAuswahl(
        label="Bei welchem Bearbeiter",
        queryset=ChatBindung.objects.none(),
        help_text="Nur Aufträge, die dieser Zugang gerade bearbeitet.",
    )
    meldungsarten = forms.MultipleChoiceField(
        label="Bei welchen Blockaden",
        choices=[("FEHLER", "Fehler"), ("HINDERNIS", "Hindernis"), ("RUECKFRAGE", "Rückfrage")],
        initial=["FEHLER"],
        widget=forms.CheckboxSelectMultiple,
        help_text="Reine Lieferblockaden laufen immer über die Lieferautomatik.",
    )
    gueltig_bis = forms.DateField(
        label="Gültig bis",
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        help_text="Höchstens 90 Tage; der Tag zählt bis 23:59.",
    )
    auftrag = forms.CharField(
        label="Nur für Auftrag",
        max_length=40,
        required=False,
        help_text="z. B. NEX-63; leer = alle",
    )
    entscheidung = forms.CharField(
        label="Bezug", max_length=20, required=False, help_text="z. B. E-213"
    )
    lieferung = forms.CharField(
        label="Nur mit Nachweis",
        max_length=20,
        required=False,
        help_text="Liefernummer der belegten Korrektur, z. B. L-1; leer = jede passende",
    )

    def __init__(self, *args, projekt=None, **kwargs):
        super().__init__(*args, **kwargs)
        zugaenge = ChatBindung.objects.filter(widerrufen_am__isnull=True).exclude(
            art=ChatBindung.ART_WERKBANK
        )
        if projekt is not None:
            zugaenge = zugaenge.filter(models.Q(art=ChatBindung.ART_PC) | models.Q(projekt=projekt))
        self.fields["ausfuehrende_bindung"].queryset = zugaenge.filter(art=ChatBindung.ART_PC)
        self.fields["bearbeiter_bindung"].queryset = zugaenge
        _stufen(self)

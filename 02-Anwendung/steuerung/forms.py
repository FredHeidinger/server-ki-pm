from django import forms

from .models import Arbeitsort, Auftrag, Modul, Projekt


class NamensAuswahl(forms.ModelChoiceField):
    """Zeigt den verständlichen Namen statt der technischen Kennung."""

    def label_from_instance(self, obj):
        return getattr(obj, "name", None) or getattr(obj, "get_username", lambda: str(obj))()


class ProjektForm(forms.Form):
    kennung = forms.SlugField(
        label="Kennung",
        max_length=40,
        help_text="Kurz und dauerhaft, nur Buchstaben, Ziffern und Bindestriche. Beispiel: synth-a",
    )
    name = forms.CharField(label="Name", max_length=120)
    beschreibung = forms.CharField(
        label="Beschreibung", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )
    module = forms.CharField(
        label="Module (optional)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Ein Modul je Zeile, z. B. „Kern“ oder „kern: Kernfunktionen“.",
    )


class ModulForm(forms.Form):
    kennung = forms.SlugField(label="Kennung", max_length=40)
    name = forms.CharField(label="Name", max_length=120)


class ArbeitsortForm(forms.Form):
    kennung = forms.SlugField(label="Kennung", max_length=40, help_text="z. B. repo-haupt")
    name = forms.CharField(label="Name", max_length=120, help_text="z. B. Repository synth-a")
    rechner = forms.CharField(
        label="Rechner", max_length=120, required=False, help_text="z. B. Freds PC"
    )
    pfad = forms.CharField(
        label="Pfad oder Repository",
        max_length=300,
        required=False,
        help_text="Nur zur Orientierung; der Dienst öffnet keine Pfade.",
    )


class BindungForm(forms.Form):
    name = forms.CharField(
        label="Name der Bindung", max_length=120, help_text="z. B. Chat Synthetisches A"
    )
    gueltig_tage = forms.IntegerField(label="Gültig (Tage)", min_value=1, max_value=365, initial=90)


class WunschForm(forms.Form):
    projekt = NamensAuswahl(label="Projekt", queryset=Projekt.objects.filter(aktiv=True))
    modul = NamensAuswahl(label="Modul (optional)", queryset=Modul.objects.none(), required=False)
    titel = forms.CharField(label="Titel", max_length=200)
    beschreibung = forms.CharField(
        label="Beschreibung in deinen Worten",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Was soll anders sein? Ziel, Umfang und Abnahme folgen beim Konkretisieren.",
    )
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO, initial="mittel")

    def __init__(self, *args, projekt: Projekt | None = None, **kwargs):
        super().__init__(*args, **kwargs)
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
                "Konkretisieren nachtragen."
            )


class ArbeitspaketForm(forms.Form):
    """Konkretisieren: erzeugt eine neue Revision."""

    titel = forms.CharField(label="Titel", max_length=200)
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO)
    modul = NamensAuswahl(label="Modul", queryset=Modul.objects.none(), required=False)
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
        help_text="Mindestens ein Eintrag oder ausdrücklich „keine“.",
    )
    arbeitsort = NamensAuswahl(
        label="Arbeitsort", queryset=Arbeitsort.objects.none(), required=False
    )
    schreibumfang = forms.CharField(
        label="Schreibumfang",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Ein Pfadmuster je Zeile, z. B. src/auth/** oder docs/ZUSTANDSMODELL.md.",
    )
    quellen = forms.CharField(
        label="Quellen und Verträge",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Verweise mit Revisionsstand; darf leer sein.",
    )
    abnahmekriterien = forms.CharField(
        label="Abnahmekriterien",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="Eine prüfbare Aussage je Zeile.",
    )
    pruefer = NamensAuswahl(label="Prüfer", queryset=None, required=False)
    anlass = forms.CharField(label="Anlass der Änderung", max_length=200, required=False)

    def __init__(self, *args, auftrag: Auftrag, menschen, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["modul"].queryset = Modul.objects.filter(projekt=auftrag.projekt, aktiv=True)
        self.fields["arbeitsort"].queryset = Arbeitsort.objects.filter(
            projekt=auftrag.projekt, aktiv=True
        )
        self.fields["pruefer"].queryset = menschen


class BegruendungForm(forms.Form):
    grund = forms.CharField(label="Begründung", widget=forms.Textarea(attrs={"rows": 2}))


class EigenesErgebnisForm(forms.Form):
    zusammenfassung = forms.CharField(
        label="Zusammenfassung", widget=forms.Textarea(attrs={"rows": 3})
    )
    dateien = forms.CharField(
        label="Geänderte Dateien mit Revision",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Eine je Zeile, z. B. src/auth/login.py@9c2e7b1",
    )
    pruefungen = forms.CharField(
        label="Ausgeführte Prüfungen",
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Eine je Zeile, z. B. pytest tests/auth: 12 bestanden",
    )
    luecken = forms.CharField(
        label="Bekannte Lücken", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )


class EntscheidungForm(forms.Form):
    titel = forms.CharField(label="Titel", max_length=200)
    text = forms.CharField(label="Vorschlag", widget=forms.Textarea(attrs={"rows": 4}))
    verweis = forms.CharField(
        label="Verweis auf Dokument mit Revision", max_length=300, required=False
    )
    projekte = forms.ModelMultipleChoiceField(
        label="Betroffene Projekte",
        queryset=Projekt.objects.filter(aktiv=True),
        widget=forms.CheckboxSelectMultiple,
    )
    sofort_entscheiden = forms.BooleanField(
        label="Sofort entscheiden (nicht nur vorschlagen)", required=False
    )


class EntscheidenForm(forms.Form):
    text = forms.CharField(
        label="Entscheidungstext (leer = Vorschlagstext übernehmen)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

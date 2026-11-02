from django import forms

from .models import Arbeitsort, Auftrag, Modul, Projekt

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
    kennung = forms.SlugField(
        label="Kürzel",
        max_length=40,
        required=False,
        help_text="Kurz, ohne Leerzeichen; leer lassen, dann wird es aus dem Namen gebildet.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class ModulForm(forms.Form):
    name = forms.CharField(label="Name des Moduls", max_length=120)


class ArbeitsortForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="z. B. Repository Nexus")
    rechner = forms.CharField(
        label="Rechner", max_length=120, required=False, help_text="z. B. Freds PC"
    )
    pfad = forms.CharField(
        label="Ordner auf dem Rechner",
        max_length=300,
        required=False,
        help_text=(
            "z. B. W:/Projekte/Nexus. Ein PC-Zugang erkennt daran das Projekt; "
            "der Dienst öffnet den Ordner nicht."
        ),
    )
    repository = forms.URLField(
        label="Repository",
        max_length=300,
        required=False,
        assume_scheme="https",
        help_text=(
            "z. B. https://github.com/FredHeidinger/nexus; "
            "macht Datei@Commit in Ergebnissen anklickbar."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class ZugangForm(forms.Form):
    """Zugang anlegen: Name und Gültigkeit; Projekt nur beim Projektschlüssel."""

    name = forms.CharField(
        label="Name", max_length=120, help_text="Wer oder was diesen Zugang nutzt."
    )
    projekt = NamensAuswahl(
        label="Projekt", queryset=Projekt.objects.filter(aktiv=True), required=False
    )
    gueltig_tage = forms.IntegerField(label="Gültig (Tage)", min_value=1, max_value=365, initial=90)

    def __init__(self, *args, projektpflicht: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        if projektpflicht:
            self.fields["projekt"].required = True
        else:
            del self.fields["projekt"]
        _stufen(self)


class WunschForm(forms.Form):
    projekt = NamensAuswahl(label="Projekt", queryset=Projekt.objects.filter(aktiv=True))
    modul = NamensAuswahl(label="Modul", queryset=Modul.objects.none(), required=False)
    titel = forms.CharField(label="Titel", max_length=200)
    beschreibung = forms.CharField(
        label="Beschreibung in deinen Worten",
        widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Was soll anders sein? Ziel, Umfang und Abnahme folgen beim Konkretisieren.",
    )
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO, initial="mittel")
    erzwingen = forms.BooleanField(
        label="Trotz gleichem Titel anlegen (kein Doppel)", required=False
    )

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
        _stufen(self)


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
    pruefer = NamensAuswahl(label="Prüfer", queryset=None, required=False)
    anlass = forms.CharField(label="Anlass der Änderung", max_length=200, required=False)

    def __init__(self, *args, auftrag: Auftrag, menschen, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["modul"].queryset = Modul.objects.filter(projekt=auftrag.projekt, aktiv=True)
        self.fields["arbeitsort"].queryset = Arbeitsort.objects.filter(
            projekt=auftrag.projekt, aktiv=True
        )
        self.fields["pruefer"].queryset = menschen
        _stufen(
            self,
            ziel=FREIGABE,
            nicht_ziele=FREIGABE,
            arbeitsort=FREIGABE,
            schreibumfang=FREIGABE,
            abnahmekriterien=FREIGABE,
            pruefer=FREIGABE,
        )


class BegruendungForm(forms.Form):
    grund = forms.CharField(label="Begründung", widget=forms.Textarea(attrs={"rows": 2}))


class EigenesErgebnisForm(forms.Form):
    zusammenfassung = forms.CharField(
        label="Zusammenfassung", widget=forms.Textarea(attrs={"rows": 3})
    )
    dateien = _liste(
        forms.CharField(label="Geänderte Dateien mit Revision", widget=forms.HiddenInput),
        "z. B. src/auth/login.py@9c2e7b1",
    )
    pruefungen = _liste(
        forms.CharField(label="Ausgeführte Prüfungen", widget=forms.HiddenInput),
        "z. B. pytest tests/auth: 12 bestanden",
    )
    luecken = forms.CharField(
        label="Bekannte Lücken", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class EntscheidenForm(forms.Form):
    text = forms.CharField(
        label="Entscheidungstext (leer = Vorschlagstext übernehmen)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )


def listen_zusammenfuehren(post, *felder: str):
    """Einträge der Plus-Listen (``<feld>_eintrag``) zu Zeilen des Hauptfelds zusammenführen."""
    daten = post.copy()
    for feld in felder:
        eintraege = [z.strip() for z in post.getlist(f"{feld}_eintrag") if z.strip()]
        if eintraege or feld not in post:
            daten[feld] = "\n".join(eintraege)
    return daten

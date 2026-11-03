from django import forms

from .models import (
    Arbeitsort,
    Auftrag,
    Funktion,
    Modul,
    Nachweis,
    Projekt,
    Release,
    RepoVerbindung,
    Umgebung,
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


class ModulNeuForm(forms.Form):
    """Modul über „+ Neu“ anlegen: Projekt wählen, Namen eingeben."""

    projekt = NamensAuswahl(label="Projekt", queryset=Projekt.objects.filter(aktiv=True))
    name = forms.CharField(
        label="Name des Moduls", max_length=120, help_text="z. B. Export oder Oberfläche"
    )

    def __init__(self, *args, projekt: Projekt | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        if projekt is not None:
            self.fields["projekt"].initial = projekt
        _stufen(self)


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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
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
        help_text="In deinen Worten. Ziel, Umfang und Abnahme folgen beim Planen.",
    )
    prioritaet = forms.ChoiceField(label="Priorität", choices=Auftrag.PRIO, initial="mittel")
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


# --- Projektakte -------------------------------------------------------------------------------


def _text(label, zeilen=3, hilfe="", pflicht=False):
    return forms.CharField(
        label=label,
        required=pflicht,
        help_text=hilfe,
        widget=forms.Textarea(attrs={"rows": zeilen}),
    )


class ProfilForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput)
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


class FunktionForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)
    titel = forms.CharField(
        label="Titel",
        max_length=200,
        help_text="Eine Fähigkeit, z. B. „Unterlagen einem Auftrag zuordnen“",
    )
    nutzen = _text("Nutzen", 2, "Wem hilft das und wobei?")
    kriterien = _liste(
        forms.CharField(label="Kriterien", required=False, widget=forms.HiddenInput),
        "z. B. Eine Unterlage lässt sich einem Auftrag zuordnen",
    )
    planung = forms.ChoiceField(label="Planung", choices=Funktion.PLANUNG, initial=Funktion.IDEE)
    modul = NamensAuswahl(
        label="Modul",
        queryset=Modul.objects.none(),
        required=False,
        empty_label="Gesamtes Projekt",
        help_text="„Gesamtes Projekt“ gilt modulübergreifend.",
    )

    def __init__(self, *args, projekt: Projekt, **kwargs):
        super().__init__(*args, **kwargs)
        module = Modul.objects.filter(projekt=projekt, aktiv=True)
        self.fields["modul"].queryset = module
        if not module.exists():
            del self.fields["modul"]
        _stufen(self)


class MeilensteinForm(forms.Form):
    name = forms.CharField(label="Name", max_length=160)
    ziel = _text("Ziel", 2)
    zielversion = forms.CharField(label="Zielversion", max_length=60, required=False)
    termin = forms.DateField(
        label="Termin", required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    funktionen = forms.ModelMultipleChoiceField(
        label="Funktionen",
        queryset=Funktion.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, projekt: Projekt, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["funktionen"].queryset = Funktion.objects.filter(projekt=projekt)
        _stufen(self)


class NachweisForm(forms.Form):
    """Prüfung oder Umsetzung dokumentieren; Abnahme ist eine eigene Aktion."""

    art = forms.ChoiceField(
        label="Art",
        choices=[
            (Nachweis.TEST, "Test"),
            (Nachweis.BETRIEB, "Betrieb"),
            (Nachweis.UMSETZUNG, "Umsetzung"),
        ],
    )
    ergebnis = forms.ChoiceField(
        label="Ergebnis",
        choices=[
            ("bestanden", "bestanden"),
            ("teilweise", "teilweise"),
            ("fehlgeschlagen", "fehlgeschlagen"),
            ("erreichbar", "nur erreichbar (HTTP)"),
            ("gemeldet", "Umsetzung gemeldet"),
        ],
    )
    version = forms.CharField(label="Version / Commit", max_length=80, help_text="Wofür gilt das?")
    umgebung = NamensAuswahl(label="Umgebung", queryset=Umgebung.objects.none(), required=False)
    umfang = _text("Geprüfter Umfang", 2, "z. B. Anmeldung und Dateiabruf; bei Umsetzung optional")
    quelle = forms.CharField(
        label="Quelle", max_length=300, help_text="Befehl mit Ergebnis, Bericht oder Link"
    )
    beobachtet_am = forms.DateTimeField(
        label="Beobachtet am",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="Leer = jetzt. Rückdatieren ist erlaubt; erfasst wird trotzdem heute.",
    )
    funktion = NamensAuswahl(label="Funktion", queryset=Funktion.objects.none(), required=False)

    def __init__(self, *args, projekt: Projekt, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["umgebung"].queryset = Umgebung.objects.filter(projekt=projekt, aktiv=True)
        self.fields["funktion"].queryset = Funktion.objects.filter(projekt=projekt)
        self.fields["funktion"].label_from_instance = lambda f: f"{f.kennung} {f.titel}"
        _stufen(self)


class AbnahmeForm(forms.Form):
    version = forms.CharField(label="Version", max_length=80)
    entscheidung = forms.ChoiceField(
        label="Entscheidung",
        choices=[("angenommen", "Annehmen"), ("zurueckgewiesen", "Zurückweisen")],
        widget=forms.RadioSelect,
    )
    kriterien = _text("Geprüfte Kriterien", 3, "Was hast du an dieser Version geprüft?", True)
    begruendung = _text("Begründung", 2, "Bei Zurückweisung nötig")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class UmgebungForm(forms.Form):
    name = forms.CharField(label="Name", max_length=120, help_text="z. B. Testserver oder Betrieb")
    art = forms.ChoiceField(label="Art", choices=Umgebung.ARTEN)
    beschreibung = forms.CharField(label="Zielsystem", max_length=300, required=False)
    url = forms.URLField(
        label="Dienstadresse",
        max_length=300,
        required=False,
        assume_scheme="https",
        help_text="Ohne Zugangsdaten; wird nicht automatisch abgefragt.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class ReleaseForm(forms.Form):
    kennung = forms.CharField(label="Version", max_length=60, help_text="z. B. 1.2.0")
    titel = forms.CharField(label="Titel", max_length=200, required=False)
    beschreibung = _text("Beschreibung", 3)
    komponenten = _liste(
        forms.CharField(
            label="Komponenten",
            widget=forms.HiddenInput,
            help_text="Je Zeile: Komponente | Repository | Commit oder Artefakthash",
        ),
        "z. B. Anwendung | FredHeidinger/nexus | 9c2e7b1",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)

    def komponentenliste(self) -> list[dict]:
        liste = []
        for zeile in self.cleaned_data["komponenten"].splitlines():
            teile = [t.strip() for t in zeile.split("|")]
            if not teile[0]:
                continue
            teile += [""] * (3 - len(teile))
            wert = teile[2]
            ist_hash = len(wert) == 64 and all(z in "0123456789abcdef" for z in wert.lower())
            liste.append(
                {
                    "komponente": teile[0],
                    "repository": teile[1],
                    "commit": "" if ist_hash else wert,
                    "artefakt_hash": wert if ist_hash else "",
                }
            )
        return liste


class InstallationForm(forms.Form):
    umgebung = NamensAuswahl(label="Umgebung", queryset=Umgebung.objects.none())
    komponente = forms.CharField(label="Komponente", max_length=80, help_text="z. B. Anwendung")
    version = forms.CharField(label="Version / Commit", max_length=80)
    release = forms.ModelChoiceField(
        label="Release", queryset=Release.objects.none(), required=False, empty_label="kein Release"
    )
    installiert_am = forms.DateTimeField(
        label="Installiert am",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="Leer = jetzt",
    )
    herkunft = forms.CharField(
        label="Herkunft der Meldung",
        max_length=200,
        help_text="z. B. Ausgabe der Aktualisierungszeile in der Proxmox-Shell",
    )

    def __init__(self, *args, projekt: Projekt, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["umgebung"].queryset = Umgebung.objects.filter(projekt=projekt, aktiv=True)
        self.fields["release"].queryset = Release.objects.filter(projekt=projekt)
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


class UnterlageDateiForm(forms.Form):
    titel = forms.CharField(
        label="Titel", max_length=200, required=False, help_text="Leer = Dateiname"
    )
    beschreibung = forms.CharField(label="Kurzer Zweck", max_length=300, required=False)
    datei = forms.FileField(label="Datei")
    upload_schluessel = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class RepoForm(forms.Form):
    name = forms.CharField(
        label="Repository",
        max_length=200,
        help_text="besitzer/repository, z. B. FredHeidinger/nexus",
    )
    schreibend = forms.BooleanField(label="Schreibende Aktionen erlauben", required=False)
    aktionen = forms.MultipleChoiceField(
        label="Erlaubte Aktionen",
        choices=RepoVerbindung.AKTIONEN,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    pfade = forms.CharField(
        label="Erlaubte Dokumentpfade",
        initial="docs/**",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Muster je Zeile; nur .md und .txt",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)


class GitHubAktionForm(forms.Form):
    art = forms.ChoiceField(label="Aktion", choices=RepoVerbindung.AKTIONEN)
    nummer = forms.IntegerField(label="Issue-Nummer", required=False, min_value=1)
    titel = forms.CharField(label="Titel", max_length=250, required=False)
    text = forms.CharField(label="Text", required=False, widget=forms.Textarea(attrs={"rows": 5}))
    zustand = forms.ChoiceField(
        label="Issue-Zustand",
        choices=[("", "unverändert"), ("open", "offen"), ("closed", "geschlossen")],
        required=False,
    )
    pfad = forms.CharField(label="Dokumentpfad", max_length=300, required=False)
    inhalt = forms.CharField(
        label="Neuer Dokumentinhalt", required=False, widget=forms.Textarea(attrs={"rows": 10})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _stufen(self)

    def nutzlast(self) -> dict:
        d = self.cleaned_data
        art = d["art"]
        if art == "issue_anlegen":
            return {"titel": d["titel"], "text": d["text"]}
        if art == "kommentar":
            return {"nummer": d["nummer"], "text": d["text"]}
        if art == "issue_aendern":
            werte = {"nummer": d["nummer"]}
            for feld in ("titel", "text", "zustand"):
                if d[feld]:
                    werte[feld] = d[feld]
            return werte
        return {"pfad": d["pfad"], "inhalt": d["inhalt"], "titel": d["titel"], "text": d["text"]}


def listen_zusammenfuehren(post, *felder: str):
    """Einträge der Plus-Listen (``<feld>_eintrag``) zu Zeilen des Hauptfelds zusammenführen."""
    daten = post.copy()
    for feld in felder:
        eintraege = [z.strip() for z in post.getlist(f"{feld}_eintrag") if z.strip()]
        if eintraege or feld not in post:
            daten[feld] = "\n".join(eintraege)
    return daten

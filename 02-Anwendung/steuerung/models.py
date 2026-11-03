"""Datenmodell von Server-KI-PM.

Jede fachliche Änderung läuft über ``steuerung.dienst`` und schreibt dort im selben
Commit ein ``Ereignis``. Die Modelle selbst enthalten keine Geschäftsregeln.
"""

from django.conf import settings
from django.db import models


class Projekt(models.Model):
    kennung = models.SlugField(
        "Kennung",
        max_length=40,
        unique=True,
        help_text="Kurz, eindeutig, unveränderlich, z. B. synth-a",
    )
    name = models.CharField("Name", max_length=120)
    beschreibung = models.TextField("Beschreibung", blank=True)
    aktiv = models.BooleanField("Aktiv", default=True)
    angelegt_am = models.DateTimeField("Angelegt am", auto_now_add=True)
    # Projektprofil (Projektakte); leer heißt „noch nicht erfasst“.
    zweck = models.TextField("Zweck", blank=True)
    zielgruppe = models.TextField("Zielgruppe", blank=True)
    nicht_ziele = models.TextField("Nicht-Ziele", blank=True)
    entwicklungsstufe = models.TextField("Nächste Entwicklungsstufe", blank=True)
    phase = models.CharField("Phase", max_length=60, blank=True)
    profil_revision = models.PositiveIntegerField("Profilrevision", default=0)
    # Ein offener Profilentwurf einer KI; gilt erst, wenn Fred ihn übernimmt.
    profil_entwurf = models.JSONField("Profilentwurf", default=dict, blank=True)
    profil_entwurf_von = models.CharField(max_length=120, blank=True)
    profil_entwurf_am = models.DateTimeField(null=True, blank=True)
    profil_entwurf_basis = models.PositiveIntegerField(default=0)
    # Steigt mit jedem fachlichen Ereignis des Projekts; Lesen und Besuchsmarken zählen nicht.
    inhalt_revision = models.PositiveIntegerField("Inhaltsrevision", default=0)

    class Meta:
        ordering = ["name"]
        verbose_name = "Projekt"
        verbose_name_plural = "Projekte"

    def __str__(self) -> str:
        return f"{self.name} ({self.kennung})"


class Modul(models.Model):
    projekt = models.ForeignKey(Projekt, on_delete=models.CASCADE, related_name="module")
    kennung = models.SlugField("Kennung", max_length=40)
    name = models.CharField("Name", max_length=120)
    aktiv = models.BooleanField("Aktiv", default=True)
    # Untermodule beliebig tief; ohne Elternmodul hängt das Modul direkt am Projekt.
    eltern = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="kinder"
    )
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["projekt", "kennung"], name="modul_kennung_je_projekt"),
        ]
        verbose_name = "Modul"
        verbose_name_plural = "Module"

    def __str__(self) -> str:
        return f"{self.projekt.kennung}/{self.kennung}"


class Arbeitsort(models.Model):
    """Rechner, Repository oder Arbeitskopie eines Projekts. Keine Zugangsdaten."""

    projekt = models.ForeignKey(Projekt, on_delete=models.CASCADE, related_name="arbeitsorte")
    kennung = models.SlugField("Kennung", max_length=40)
    name = models.CharField("Name", max_length=120)
    rechner = models.CharField("Rechner", max_length=120, blank=True)
    pfad = models.CharField("Ordner auf dem Rechner", max_length=300, blank=True)
    repository = models.URLField("Repository (z. B. GitHub)", max_length=300, blank=True)
    aktiv = models.BooleanField("Aktiv", default=True)
    # Ein Vorschlag einer KI gilt erst nach Bestätigung durch einen Menschen: vorher ordnet er
    # keinen Ordner zu und zählt nicht für die Freigabe.
    bestaetigt = models.BooleanField("Bestätigt", default=True)
    vorgeschlagen_von = models.CharField("Vorgeschlagen von", max_length=120, blank=True)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["projekt", "kennung"], name="arbeitsort_kennung_je_projekt"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.projekt.kennung}/{self.kennung}"


class ChatBindung(models.Model):
    """Zugang eines Chats oder eines PCs. Gespeichert wird nur der Hash des Schlüssels.

    Art „projekt“: genau ein Projekt. Art „pc“: alle Projekte; das Projekt ergibt sich aus
    dem Ordner, in dem der Chat arbeitet (Arbeitsort mit passendem Pfad).
    """

    ROLLE_BEARBEITER = "bearbeiter"
    ROLLEN = [(ROLLE_BEARBEITER, "Bearbeiter")]
    ART_PROJEKT = "projekt"
    ART_PC = "pc"
    ARTEN = [
        (ART_PROJEKT, "Chat-Schlüssel für ein Projekt"),
        (ART_PC, "PC-Zugang für alle Projekte"),
    ]

    art = models.CharField("Art", max_length=10, choices=ARTEN, default=ART_PROJEKT)
    projekt = models.ForeignKey(
        Projekt, null=True, blank=True, on_delete=models.CASCADE, related_name="bindungen"
    )
    name = models.CharField("Name", max_length=120)
    rolle = models.CharField("Rolle", max_length=20, choices=ROLLEN, default=ROLLE_BEARBEITER)
    praefix = models.CharField("Schlüsselpräfix", max_length=16, unique=True)
    schluessel_hash = models.CharField("Schlüsselhash", max_length=64)
    notiz = models.CharField("Wo gespeichert", max_length=200, blank=True)
    angelegt_am = models.DateTimeField(auto_now_add=True)
    gueltig_bis = models.DateTimeField("Gültig bis")
    widerrufen_am = models.DateTimeField("Widerrufen am", null=True, blank=True)
    zuletzt_aktiv = models.DateTimeField("Zuletzt aktiv", null=True, blank=True)
    letzter_ordner = models.CharField("Letzter Arbeitsordner", max_length=300, blank=True)
    letztes_projekt = models.ForeignKey(
        Projekt, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    letzter_pfad = models.CharField("Letzte Anfrage", max_length=120, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.projekt.kennung if self.projekt else 'alle Projekte'})"

    @property
    def aktiv(self) -> bool:
        from django.utils import timezone

        return self.widerrufen_am is None and self.gueltig_bis > timezone.now()


class OrdnerAnfrage(models.Model):
    """Ein PC-Zugang hat aus einem Ordner gearbeitet, der keinem Projekt zugeordnet ist.
    Erscheint im Posteingang, bis der Mensch ihn einem Projekt zuordnet oder verwirft."""

    ordner = models.CharField("Ordner", max_length=300)
    normiert = models.CharField("Ordner normiert", max_length=300, unique=True)
    bindung = models.ForeignKey(
        ChatBindung, on_delete=models.CASCADE, related_name="ordneranfragen"
    )
    angelegt_am = models.DateTimeField(auto_now_add=True)
    zuletzt_am = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["angelegt_am"]

    def __str__(self) -> str:
        return self.ordner


class Auftrag(models.Model):
    """Ein Auftrag beginnt als Entwurf, wird freigegeben, bearbeitet und mit dem Ergebnis
    erledigt (bestandene technische Prüfungen) oder blockiert. Nummer ist ``A-<id>``."""

    ENTWURF = "ENTWURF"
    FREIGEGEBEN = "FREIGEGEBEN"
    IN_ARBEIT = "IN_ARBEIT"
    BLOCKIERT = "BLOCKIERT"
    ERLEDIGT = "ERLEDIGT"
    ABGEBROCHEN = "ABGEBROCHEN"
    # Frühere Zustände des Ablaufs mit Nutzerabnahme. Migration 0008 überführt sie; kein Ablauf
    # erreicht sie mehr.
    ZUR_PRUEFUNG = "ZUR_PRUEFUNG"
    NACHARBEIT = "NACHARBEIT"
    ABGENOMMEN = "ABGENOMMEN"
    KLAERUNG_ERFORDERLICH = "KLAERUNG_ERFORDERLICH"
    PAUSIERT = "PAUSIERT"
    ZUSTAENDE = [
        (ENTWURF, "Entwurf"),
        (FREIGEGEBEN, "Freigegeben"),
        (IN_ARBEIT, "In Arbeit"),
        (BLOCKIERT, "Blockiert"),
        (ERLEDIGT, "Erledigt"),
        (ABGEBROCHEN, "Abgebrochen"),
    ]
    # Zustände, in denen ein Schreibumfang als belegt gilt.
    BELEGENDE_ZUSTAENDE = (FREIGEGEBEN, IN_ARBEIT, BLOCKIERT)
    # Zustände, in denen ein Bearbeiter zugeordnet ist.
    BEARBEITER_ZUSTAENDE = (IN_ARBEIT, BLOCKIERT)
    ENDZUSTAENDE = (ERLEDIGT, ABGEBROCHEN)

    PRIO = [("hoch", "hoch"), ("mittel", "mittel"), ("niedrig", "niedrig")]
    TYP_AUFTRAG = "auftrag"
    TYP_ISSUE = "issue"
    TYPEN = [(TYP_AUFTRAG, "Auftrag"), (TYP_ISSUE, "Issue")]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="auftraege")
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="auftraege"
    )
    titel = models.CharField("Titel", max_length=200)
    zustand = models.CharField("Zustand", max_length=24, choices=ZUSTAENDE, default=ENTWURF)
    zustand_vor_pause = models.CharField(max_length=24, blank=True)
    prioritaet = models.CharField("Priorität", max_length=10, choices=PRIO, default="mittel")
    typ = models.CharField(
        "Art",
        max_length=10,
        choices=TYPEN,
        default=TYP_AUFTRAG,
        help_text="Issue: Fehler oder Problem",
    )
    revision_nr = models.PositiveIntegerField("Aktuelle Revision", default=0)
    abgeleitet_von = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="abgeleitete"
    )
    entscheidung = models.ForeignKey(
        "Entscheidung", null=True, blank=True, on_delete=models.SET_NULL, related_name="auftraege"
    )
    entscheidung_stand = models.PositiveIntegerField(default=0)
    pruefer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="zu_pruefende_auftraege",
    )
    bearbeiter_bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="auftraege"
    )
    bearbeiter_benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="bearbeitete_auftraege",
    )
    bearbeiter_name = models.CharField(max_length=120, blank=True)
    uebernommen_am = models.DateTimeField(null=True, blank=True)
    uebernommene_revision = models.PositiveIntegerField(default=0)
    letzter_bearbeiter_name = models.CharField(max_length=120, blank=True)
    letzter_bearbeiter_bindung = models.ForeignKey(
        ChatBindung,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="fruehere_auftraege",
    )
    letzter_bearbeiter_benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="fruehere_auftraege",
    )
    # Revision, mit der der Auftrag erledigt wurde (Name aus dem früheren Ablauf mit Abnahme).
    abgenommene_revision = models.PositiveIntegerField(default=0)
    freigegeben_am = models.DateTimeField(null=True, blank=True)
    zuletzt_gemeldet_am = models.DateTimeField(null=True, blank=True)
    angelegt_am = models.DateTimeField(auto_now_add=True)
    aktualisiert_am = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        verbose_name = "Auftrag"
        verbose_name_plural = "Aufträge"

    def __str__(self) -> str:
        return f"{self.nummer} {self.titel}"

    @property
    def nummer(self) -> str:
        return f"A-{self.pk}"

    @property
    def aktuelle_revision(self) -> "Revision":
        return self.revisionen.get(nummer=self.revision_nr)

    @property
    def hat_bearbeiter(self) -> bool:
        return self.bearbeiter_bindung_id is not None or self.bearbeiter_benutzer_id is not None

    @property
    def ist_endzustand(self) -> bool:
        return self.zustand in self.ENDZUSTAENDE


class Revision(models.Model):
    """Eingefrorener Stand eines Auftrags. Freigabe, Übernahme, Ergebnis, Abnahme binden sie."""

    auftrag = models.ForeignKey(Auftrag, on_delete=models.CASCADE, related_name="revisionen")
    nummer = models.PositiveIntegerField()
    beschreibung = models.TextField("Wunsch in Alltagssprache", blank=True)
    ziel = models.TextField("Ziel", blank=True)
    nicht_ziele = models.TextField("Nicht-Ziele", blank=True)
    arbeitsort = models.ForeignKey(
        Arbeitsort, null=True, blank=True, on_delete=models.PROTECT, related_name="revisionen"
    )
    schreibumfang = models.TextField("Schreibumfang (Pfadmuster je Zeile)", blank=True)
    quellen = models.TextField("Quellen und Verträge", blank=True)
    abnahmekriterien = models.TextField("Abnahmekriterien (eines je Zeile)", blank=True)
    erstellt_am = models.DateTimeField(auto_now_add=True)
    erstellt_von_name = models.CharField(max_length=120)
    anlass = models.CharField("Anlass der Revision", max_length=200, blank=True)

    class Meta:
        ordering = ["-nummer"]
        constraints = [
            models.UniqueConstraint(
                fields=["auftrag", "nummer"], name="revision_nummer_je_auftrag"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.auftrag.nummer} Rev {self.nummer}"

    @property
    def kriterien(self) -> list[str]:
        return [z.strip() for z in self.abnahmekriterien.splitlines() if z.strip()]

    @property
    def muster(self) -> list[str]:
        return [z.strip() for z in self.schreibumfang.splitlines() if z.strip()]

    @property
    def ist_wunsch(self) -> bool:
        return not (self.ziel.strip() and self.kriterien)


class Abhaengigkeit(models.Model):
    auftrag = models.ForeignKey(Auftrag, on_delete=models.CASCADE, related_name="abhaengigkeiten")
    vorgaenger = models.ForeignKey(Auftrag, on_delete=models.CASCADE, related_name="nachfolger")
    grund = models.CharField("Grund", max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["auftrag", "vorgaenger"], name="abhaengigkeit_einmal"),
        ]


class Meldung(models.Model):
    """Fortschritt, Hindernis, Vorschlag oder Prüfbericht eines Bearbeiters oder Chats."""

    FORTSCHRITT = "FORTSCHRITT"
    HINDERNIS = "HINDERNIS"
    RUECKFRAGE = "RUECKFRAGE"
    FEHLER = "FEHLER"
    VORSCHLAG = "VORSCHLAG"
    PRUEFBERICHT = "PRUEFBERICHT"
    ANTWORT = "ANTWORT"
    ARTEN = [
        (FORTSCHRITT, "Fortschritt"),
        (HINDERNIS, "Hindernis"),
        (RUECKFRAGE, "Rückfrage"),
        (FEHLER, "Fehler"),
        (VORSCHLAG, "Vorschlag"),
        (PRUEFBERICHT, "Prüfbericht"),
        (ANTWORT, "Antwort"),
    ]
    BLOCKIERENDE = (HINDERNIS, RUECKFRAGE, FEHLER)
    OFFEN = "OFFEN"
    UEBERNOMMEN = "UEBERNOMMEN"
    ABGELEHNT = "ABGELEHNT"
    VORSCHLAG_STATUS = [(OFFEN, "offen"), (UEBERNOMMEN, "übernommen"), (ABGELEHNT, "abgelehnt")]

    auftrag = models.ForeignKey(Auftrag, on_delete=models.CASCADE, related_name="meldungen")
    revision_nr = models.PositiveIntegerField()
    art = models.CharField(max_length=16, choices=ARTEN)
    text = models.TextField("Text")
    akteur_name = models.CharField(max_length=120)
    bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="meldungen"
    )
    zeitpunkt = models.DateTimeField(auto_now_add=True)
    historisch = models.BooleanField(
        "Bezieht sich auf eine überholte Revision oder erloschene Übernahme", default=False
    )
    vorschlag_status = models.CharField(
        max_length=12, choices=VORSCHLAG_STATUS, default=OFFEN, blank=True
    )

    class Meta:
        ordering = ["-zeitpunkt", "-id"]


class Ergebnis(models.Model):
    auftrag = models.ForeignKey(Auftrag, on_delete=models.CASCADE, related_name="ergebnisse")
    revision_nr = models.PositiveIntegerField()
    zusammenfassung = models.TextField("Zusammenfassung")
    dateien = models.TextField("Geänderte Dateien mit Revision (eine je Zeile)", blank=True)
    pruefschritte = models.TextField("Bestandene Prüfungen (eine je Zeile)", blank=True)
    fehlgeschlagen = models.TextField("Fehlgeschlagene Prüfungen (eine je Zeile)", blank=True)
    luecken = models.TextField("Bekannte Lücken", blank=True)
    eingereicht_von_name = models.CharField(max_length=120)
    bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="ergebnisse"
    )
    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="eingereichte_ergebnisse",
    )
    eingereicht_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-eingereicht_am", "-id"]

    @property
    def dateiliste(self) -> list[str]:
        return [z.strip() for z in self.dateien.splitlines() if z.strip()]

    @property
    def pruefungsliste(self) -> list[str]:
        return [z.strip() for z in self.pruefschritte.splitlines() if z.strip()]

    @property
    def fehlgeschlagenliste(self) -> list[str]:
        return [z.strip() for z in self.fehlgeschlagen.splitlines() if z.strip()]

    @property
    def ohne_tests(self) -> bool:
        """Weder bestandene noch fehlgeschlagene Prüfungen gemeldet."""
        return not (self.pruefungsliste or self.fehlgeschlagenliste)


class Pruefung(models.Model):
    ABGENOMMEN = "ABGENOMMEN"
    NACHARBEIT = "NACHARBEIT"
    ENTSCHEIDUNGEN = [(ABGENOMMEN, "Abgenommen"), (NACHARBEIT, "Nacharbeit")]

    ergebnis = models.ForeignKey(Ergebnis, on_delete=models.CASCADE, related_name="pruefungen")
    pruefer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="pruefungen"
    )
    entscheidung = models.CharField(max_length=12, choices=ENTSCHEIDUNGEN)
    begruendung = models.TextField("Begründung", blank=True)
    integration_offen = models.BooleanField("Integrationsprüfung bleibt offen", default=False)
    zeitpunkt = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-zeitpunkt", "-id"]


class Kriteriumsbewertung(models.Model):
    ERFUELLT = "ERFUELLT"
    NICHT_ERFUELLT = "NICHT_ERFUELLT"
    NICHT_GEPRUEFT = "NICHT_GEPRUEFT"
    BEWERTUNGEN = [
        (ERFUELLT, "erfüllt"),
        (NICHT_ERFUELLT, "nicht erfüllt"),
        (NICHT_GEPRUEFT, "nicht geprüft"),
    ]

    pruefung = models.ForeignKey(Pruefung, on_delete=models.CASCADE, related_name="bewertungen")
    position = models.PositiveIntegerField()
    kriterium = models.TextField()
    bewertung = models.CharField(max_length=16, choices=BEWERTUNGEN)
    anmerkung = models.TextField(blank=True)

    class Meta:
        ordering = ["position"]


class Entscheidung(models.Model):
    VORSCHLAG = "VORSCHLAG"
    ENTSCHIEDEN = "ENTSCHIEDEN"
    ZURUECKGEZOGEN = "ZURUECKGEZOGEN"
    ZUSTAENDE = [
        (VORSCHLAG, "Vorschlag"),
        (ENTSCHIEDEN, "Entschieden"),
        (ZURUECKGEZOGEN, "Zurückgezogen"),
    ]

    titel = models.CharField("Titel", max_length=200)
    text = models.TextField("Vorschlag / Entscheidung")
    verweis = models.CharField("Verweis auf Dokument mit Revision", max_length=300, blank=True)
    zustand = models.CharField(max_length=16, choices=ZUSTAENDE, default=VORSCHLAG)
    stand = models.PositiveIntegerField("Stand", default=0)
    betroffene_projekte = models.ManyToManyField(Projekt, related_name="entscheidungen")
    vorgeschlagen_von_name = models.CharField(max_length=120)
    vorgeschlagen_am = models.DateTimeField(auto_now_add=True)
    entschieden_von = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="entscheidungen",
    )
    entschieden_am = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self) -> str:
        return f"{self.nummer} {self.titel}"

    @property
    def nummer(self) -> str:
        return f"E-{self.pk}"


class Uebernahmebestaetigung(models.Model):
    entscheidung = models.ForeignKey(
        Entscheidung, on_delete=models.CASCADE, related_name="bestaetigungen"
    )
    projekt = models.ForeignKey(Projekt, on_delete=models.CASCADE)
    stand = models.PositiveIntegerField()
    bestaetigt_von_name = models.CharField(max_length=120)
    zeitpunkt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entscheidung", "projekt", "stand"], name="bestaetigung_je_stand"
            ),
        ]


class AnfrageKennung(models.Model):
    """Wiederholungsschutz für verändernde Chat-Anfragen: gleiche Kennung, gleicher Inhalt."""

    bindung = models.ForeignKey(ChatBindung, on_delete=models.CASCADE, related_name="anfragen")
    kennung = models.CharField(max_length=80)
    inhalt_hash = models.CharField(max_length=64)
    antwort = models.JSONField()
    status = models.PositiveIntegerField()
    zeitpunkt = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bindung", "kennung"], name="anfragekennung_einmal"),
        ]


class Ereignis(models.Model):
    """Unveränderbarer Verlaufseintrag. Wird nur über den Dienst angelegt, nie bearbeitet."""

    zeitpunkt = models.DateTimeField("Zeitpunkt", auto_now_add=True, db_index=True)
    akteur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="ereignisse",
    )
    akteur_name = models.CharField("Akteur", max_length=120)
    projekt = models.ForeignKey(
        Projekt, null=True, blank=True, on_delete=models.PROTECT, related_name="ereignisse"
    )
    objekt_typ = models.CharField("Objekt", max_length=40)
    objekt_kennung = models.CharField("Objektkennung", max_length=80)
    ereignis = models.CharField("Ereignis", max_length=120)
    anlass = models.TextField("Anlass / Begründung", blank=True)
    revision = models.CharField("Revision", max_length=40, blank=True)
    # Wann etwas tatsächlich geschah (z. B. Installation gestern), getrennt vom Erfassen.
    beobachtet_am = models.DateTimeField("Beobachtet am", null=True, blank=True)
    version = models.ForeignKey(
        "Objektversion", null=True, blank=True, on_delete=models.PROTECT, related_name="ereignisse"
    )

    class Meta:
        ordering = ["-zeitpunkt", "-id"]
        verbose_name = "Ereignis"
        verbose_name_plural = "Ereignisse"

    def __str__(self) -> str:
        return f"{self.akteur_name}: {self.ereignis} ({self.objekt_kennung})"


# --- Projektakte -------------------------------------------------------------------------------


class Objektversion(models.Model):
    """Eingefrorener Stand eines Akte-Objekts nach einer Änderung; Grundlage der Historie."""

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="objektversionen")
    objekt_typ = models.CharField(max_length=40)
    objekt_id = models.PositiveBigIntegerField()
    nummer = models.PositiveIntegerField()
    daten = models.JSONField()
    zeitpunkt = models.DateTimeField(auto_now_add=True)
    akteur_name = models.CharField(max_length=120)

    class Meta:
        ordering = ["-nummer"]
        constraints = [
            models.UniqueConstraint(
                fields=["objekt_typ", "objekt_id", "nummer"], name="objektversion_einmal"
            ),
        ]


class Meilenstein(models.Model):
    GEPLANT, IN_ARBEIT, ERREICHT, ZURUECKGESTELLT = (
        "geplant",
        "in_arbeit",
        "erreicht",
        "zurueckgestellt",
    )
    ZUSTAENDE = [
        (GEPLANT, "geplant"),
        (IN_ARBEIT, "in Arbeit"),
        (ERREICHT, "erreicht"),
        (ZURUECKGESTELLT, "zurückgestellt"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="meilensteine")
    name = models.CharField("Name", max_length=160)
    ziel = models.TextField("Ziel", blank=True)
    zielversion = models.CharField("Zielversion", max_length=60, blank=True)
    termin = models.DateField("Termin", null=True, blank=True)
    zustand = models.CharField(max_length=20, choices=ZUSTAENDE, default=GEPLANT)
    revision = models.PositiveIntegerField(default=1)

    class Meta:
        ordering = ["id"]


class Funktion(models.Model):
    """Fähigkeit des Produkts. Nummer ``F-<n>`` je Projekt; Zustände ergeben sich aus Nachweisen."""

    IDEE, VORGESEHEN, ZURUECKGESTELLT, VERWORFEN = (
        "idee",
        "vorgesehen",
        "zurueckgestellt",
        "verworfen",
    )
    PLANUNG = [
        (IDEE, "Idee"),
        (VORGESEHEN, "vorgesehen"),
        (ZURUECKGESTELLT, "zurückgestellt"),
        (VERWORFEN, "verworfen"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="funktionen")
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="funktionen"
    )
    nummer = models.PositiveIntegerField()
    titel = models.CharField("Titel", max_length=200)
    nutzen = models.TextField("Nutzen", blank=True)
    kriterien = models.TextField("Kriterien (eines je Zeile)", blank=True)
    planung = models.CharField("Planung", max_length=20, choices=PLANUNG, default=IDEE)
    revision = models.PositiveIntegerField(default=1)
    auftraege = models.ManyToManyField(Auftrag, blank=True, related_name="funktionen")
    meilensteine = models.ManyToManyField(Meilenstein, blank=True, related_name="funktionen")
    angelegt_am = models.DateTimeField(auto_now_add=True)
    aktualisiert_am = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nummer"]
        constraints = [
            models.UniqueConstraint(
                fields=["projekt", "nummer"], name="funktion_nummer_je_projekt"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kennung} {self.titel}"

    @property
    def kennung(self) -> str:
        return f"F-{self.nummer}"

    @property
    def kriterienliste(self) -> list[str]:
        return [z.strip() for z in self.kriterien.splitlines() if z.strip()]


class Umgebung(models.Model):
    LOKAL, TEST, PRODUKTION = "lokal", "test", "produktion"
    ARTEN = [(LOKAL, "lokal"), (TEST, "Test"), (PRODUKTION, "Produktion")]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="umgebungen")
    kennung = models.SlugField(max_length=40)
    name = models.CharField("Name", max_length=120)
    art = models.CharField("Art", max_length=12, choices=ARTEN, default=TEST)
    beschreibung = models.TextField("Zielsystem", blank=True)
    url = models.URLField("Dienstadresse (ohne Geheimnisse)", max_length=300, blank=True)
    aktiv = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    # Von einer KI vorgeschlagen: zählt erst nach Übernahme durch Fred für Meldungen.
    bestaetigt = models.BooleanField("Bestätigt", default=True)
    vorgeschlagen_von = models.CharField("Vorgeschlagen von", max_length=120, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["projekt", "kennung"], name="umgebung_je_projekt"),
        ]

    def __str__(self) -> str:
        return self.name


class Release(models.Model):
    ENTWURF, VEROEFFENTLICHT, ERSETZT = "entwurf", "veroeffentlicht", "ersetzt"
    ZUSTAENDE = [(ENTWURF, "Entwurf"), (VEROEFFENTLICHT, "veröffentlicht"), (ERSETZT, "ersetzt")]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="releases")
    kennung = models.CharField("Version", max_length=60)
    titel = models.CharField("Titel", max_length=200, blank=True)
    beschreibung = models.TextField("Beschreibung", blank=True)
    zustand = models.CharField(max_length=16, choices=ZUSTAENDE, default=ENTWURF)
    externe_id = models.CharField("Externe Release-ID", max_length=80, blank=True)
    revision = models.PositiveIntegerField(default=1)
    archiviert = models.BooleanField(default=False)
    angelegt_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["projekt", "kennung"], name="release_je_projekt"),
        ]

    def __str__(self) -> str:
        return self.kennung


class ReleaseKomponente(models.Model):
    release = models.ForeignKey(Release, on_delete=models.PROTECT, related_name="komponenten")
    komponente = models.CharField("Komponente", max_length=80)
    repository = models.CharField("Repository", max_length=200, blank=True)
    commit = models.CharField("Commit", max_length=64, blank=True)
    artefakt_hash = models.CharField("Artefakthash", max_length=128, blank=True)

    class Meta:
        ordering = ["komponente"]


class Installation(models.Model):
    GEMELDET, BESTAETIGT, FEHLGESCHLAGEN, ERSETZT = (
        "gemeldet",
        "bestaetigt",
        "fehlgeschlagen",
        "ersetzt",
    )
    ZUSTAENDE = [
        (GEMELDET, "gemeldet"),
        (BESTAETIGT, "bestätigt"),
        (FEHLGESCHLAGEN, "fehlgeschlagen"),
        (ERSETZT, "ersetzt"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="installationen")
    umgebung = models.ForeignKey(Umgebung, on_delete=models.PROTECT, related_name="installationen")
    komponente = models.CharField("Komponente", max_length=80)
    release = models.ForeignKey(
        Release, null=True, blank=True, on_delete=models.PROTECT, related_name="installationen"
    )
    version = models.CharField("Version / Commit", max_length=80)
    installiert_am = models.DateTimeField("Installiert am (gemeldet)")
    erfasst_am = models.DateTimeField(auto_now_add=True)
    melder_name = models.CharField(max_length=120)
    herkunft = models.CharField("Herkunft der Meldung", max_length=200, blank=True)
    bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    zustand = models.CharField(max_length=16, choices=ZUSTAENDE, default=GEMELDET)
    bestaetigt_von = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    vorgaenger = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="nachfolger"
    )
    bemerkung = models.TextField(blank=True)
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="installationen"
    )
    meldung = models.CharField("Meldungs-ID", max_length=80, blank=True, db_index=True)
    meldung_hash = models.CharField(max_length=64, blank=True)
    korrigiert_durch = models.CharField("Korrigiert durch Meldung", max_length=80, blank=True)

    class Meta:
        ordering = ["-installiert_am", "-id"]


class Nachweis(models.Model):
    """Beleg zu Umsetzung, Test, Abnahme oder Betrieb; immer mit Version, Quelle und Zeitpunkten."""

    UMSETZUNG, TEST, ABNAHME, BETRIEB = "umsetzung", "test", "abnahme", "betrieb"
    ARTEN = [(UMSETZUNG, "Umsetzung"), (TEST, "Test"), (ABNAHME, "Abnahme"), (BETRIEB, "Betrieb")]
    ERGEBNISSE = [
        ("gemeldet", "gemeldet"),
        ("bestanden", "bestanden"),
        ("teilweise", "teilweise"),
        ("fehlgeschlagen", "fehlgeschlagen"),
        ("erreichbar", "erreichbar"),
        ("angenommen", "angenommen"),
        ("zurueckgewiesen", "zurückgewiesen"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="nachweise")
    art = models.CharField("Art", max_length=12, choices=ARTEN)
    ergebnis = models.CharField("Ergebnis", max_length=16, choices=ERGEBNISSE)
    umfang = models.TextField("Geltungs- / Testumfang", blank=True)
    quelle = models.CharField("Quelle", max_length=300, blank=True)
    version = models.CharField("Version / Commit / Hash", max_length=80, blank=True)
    umgebung = models.ForeignKey(
        Umgebung, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    beobachtet_am = models.DateTimeField("Beobachtet am")
    erfasst_am = models.DateTimeField(auto_now_add=True)
    akteur_name = models.CharField(max_length=120)
    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    gemessen = models.BooleanField("Automatische Messung", default=False)
    funktion = models.ForeignKey(
        Funktion, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    release = models.ForeignKey(
        Release, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    installation = models.ForeignKey(
        Installation, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    auftrag = models.ForeignKey(
        Auftrag, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    auftrag_ergebnis = models.ForeignKey(
        Ergebnis, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    kriterien = models.TextField("Geprüfte Kriterien", blank=True)
    # Meldevertrag (kipm-stand/1): Modul, Auftragsrevision, Herkunft der Meldung, Korrektur und
    # die getrennte menschliche Bestätigung.
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="nachweise"
    )
    komponente = models.CharField("Komponente", max_length=80, blank=True)
    auftrag_revision = models.PositiveIntegerField(default=0)
    meldung = models.CharField("Meldungs-ID", max_length=80, blank=True, db_index=True)
    meldung_hash = models.CharField(max_length=64, blank=True)
    korrigiert_durch = models.CharField("Korrigiert durch Meldung", max_length=80, blank=True)
    einschraenkungen = models.TextField("Einschränkungen", blank=True)
    bestaetigt_von = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bestaetigt_am = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-beobachtet_am", "-id"]


class Unterlage(models.Model):
    TEXT, DATEI, LINK, REPO = "text", "datei", "link", "repo"
    ARTEN = [(TEXT, "Text"), (DATEI, "Datei"), (LINK, "Link"), (REPO, "Repository-Datei")]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="unterlagen")
    nummer = models.PositiveIntegerField()
    titel = models.CharField("Titel", max_length=200)
    art = models.CharField("Art", max_length=8, choices=ARTEN)
    beschreibung = models.TextField("Zweck", blank=True)
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="unterlagen"
    )
    archiviert = models.BooleanField(default=False)
    ki_freigegeben = models.BooleanField("Für KI verfügbar", default=False)
    aktuelle_nr = models.PositiveIntegerField(default=0)
    angelegt_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["nummer"]
        constraints = [
            models.UniqueConstraint(fields=["projekt", "nummer"], name="unterlage_je_projekt"),
        ]

    def __str__(self) -> str:
        return f"{self.kennung} {self.titel}"

    @property
    def kennung(self) -> str:
        return f"U-{self.nummer}"


class Unterlagenversion(models.Model):
    ENTWURF, GRUNDLAGE, HINTERGRUND = "entwurf", "grundlage", "hintergrund"
    ROLLEN = [(ENTWURF, "Entwurf"), (GRUNDLAGE, "Grundlage"), (HINTERGRUND, "Hintergrund")]
    NICHT_NOETIG, AUSSTEHEND, SAUBER, ABGEWIESEN = (
        "nicht_noetig",
        "ausstehend",
        "sauber",
        "abgewiesen",
    )
    PRUEFUNGEN = [
        (NICHT_NOETIG, "keine Dateiprüfung nötig"),
        (AUSSTEHEND, "Prüfung ausstehend"),
        (SAUBER, "geprüft, unauffällig"),
        (ABGEWIESEN, "abgewiesen"),
    ]
    EXTRAKTIONEN = [
        ("keine", "keine"),
        ("ausstehend", "ausstehend"),
        ("vollstaendig", "vollständig"),
        ("teilweise", "teilweise"),
        ("nicht_auswertbar", "nicht auswertbar"),
    ]

    unterlage = models.ForeignKey(Unterlage, on_delete=models.PROTECT, related_name="versionen")
    nummer = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    blob = models.CharField(max_length=64, blank=True)
    uri = models.CharField("Adresse", max_length=500, blank=True)
    quellversion = models.CharField("Fixierter Stand", max_length=80, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    groesse = models.PositiveBigIntegerField(default=0)
    inhaltstyp = models.CharField(max_length=100, blank=True)
    originalname = models.CharField(max_length=255, blank=True)
    herkunft = models.CharField(max_length=200, blank=True)
    autor_name = models.CharField(max_length=120)
    benutzer = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    zeitpunkt = models.DateTimeField(auto_now_add=True)
    rolle = models.CharField(max_length=12, choices=ROLLEN, default=ENTWURF)
    pruefung = models.CharField(max_length=14, choices=PRUEFUNGEN, default=NICHT_NOETIG)
    pruefhinweis = models.CharField(max_length=300, blank=True)
    extraktion = models.CharField(max_length=16, choices=EXTRAKTIONEN, default="keine")
    extrakt = models.TextField(blank=True)
    extrakt_hinweis = models.CharField(max_length=300, blank=True)
    upload_schluessel = models.CharField(max_length=64, null=True, blank=True, unique=True)

    class Meta:
        ordering = ["-nummer"]
        constraints = [
            models.UniqueConstraint(
                fields=["unterlage", "nummer"], name="unterlagenversion_einmal"
            ),
        ]

    @property
    def herunterladbar(self) -> bool:
        return bool(self.blob) and self.pruefung == self.SAUBER


class Unterlagenzuordnung(models.Model):
    """Version einer Unterlage gilt für genau ein Ziel desselben Projekts."""

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="+")
    version = models.ForeignKey(
        Unterlagenversion, on_delete=models.PROTECT, related_name="zuordnungen"
    )
    funktion = models.ForeignKey(
        Funktion, null=True, blank=True, on_delete=models.PROTECT, related_name="unterlagen"
    )
    auftrag = models.ForeignKey(
        Auftrag, null=True, blank=True, on_delete=models.PROTECT, related_name="unterlagen"
    )
    auftrag_revision = models.PositiveIntegerField(default=0)
    entscheidung = models.ForeignKey(
        Entscheidung, null=True, blank=True, on_delete=models.PROTECT, related_name="unterlagen"
    )
    entscheidung_stand = models.PositiveIntegerField(default=0)
    release = models.ForeignKey(
        Release, null=True, blank=True, on_delete=models.PROTECT, related_name="unterlagen"
    )
    zweck = models.CharField("Verwendungszweck", max_length=200, blank=True)
    angelegt_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(funktion__isnull=False, auftrag__isnull=True)
                    & models.Q(entscheidung__isnull=True, release__isnull=True)
                )
                | (
                    models.Q(funktion__isnull=True, auftrag__isnull=False)
                    & models.Q(entscheidung__isnull=True, release__isnull=True)
                )
                | (
                    models.Q(funktion__isnull=True, auftrag__isnull=True)
                    & models.Q(entscheidung__isnull=False, release__isnull=True)
                )
                | (
                    models.Q(funktion__isnull=True, auftrag__isnull=True)
                    & models.Q(entscheidung__isnull=True, release__isnull=False)
                ),
                name="zuordnung_genau_ein_ziel",
            ),
        ]


class RepoVerbindung(models.Model):
    """GitHub-Repository eines Projekts, an der stabilen GitHub-ID erkannt."""

    AKTIONEN = [
        ("issue_anlegen", "Issue anlegen"),
        ("kommentar", "Kommentar ergänzen"),
        ("issue_aendern", "Issue ändern"),
        ("dokument_pr", "Dokument auf Branch mit Pull Request"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="repos")
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    github_id = models.PositiveBigIntegerField("GitHub-Repository-ID")
    host = models.CharField(max_length=100, default="github.com")
    name = models.CharField("Name (Besitzer/Repository)", max_length=200)
    standardbranch = models.CharField(max_length=200, default="main")
    installation_id = models.PositiveBigIntegerField(null=True, blank=True)
    schreibend = models.BooleanField("Schreiben erlaubt", default=False)
    aktionen = models.TextField("Erlaubte Aktionen (eine je Zeile)", blank=True)
    erlaubte_pfade = models.TextField(
        "Erlaubte Dokumentpfade (Muster je Zeile)", blank=True, default="docs/**"
    )
    workflow_stand = models.CharField("Geprüfter Workflow-Stand", max_length=64, blank=True)
    workflow_hinweis = models.TextField("Geprüfte Workflow-Wirkung", blank=True)
    angelegt_am = models.DateTimeField(auto_now_add=True)
    widerrufen_am = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["projekt", "host", "github_id"], name="repo_einmal_je_projekt"
            ),
            models.UniqueConstraint(
                fields=["host", "github_id"],
                condition=models.Q(schreibend=True, widerrufen_am__isnull=True),
                name="repo_ein_schreibendes_projekt",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def aktiv(self) -> bool:
        return self.widerrufen_am is None

    @property
    def aktionsliste(self) -> list[str]:
        return [z.strip() for z in self.aktionen.splitlines() if z.strip()]

    @property
    def pfadmuster(self) -> list[str]:
        return [z.strip() for z in self.erlaubte_pfade.splitlines() if z.strip()]


class Abrufstand(models.Model):
    """Letzter Abruf eines Bereichs (Issues, Pull Requests …) einer Verbindung."""

    verbindung = models.ForeignKey(RepoVerbindung, on_delete=models.CASCADE, related_name="abrufe")
    bereich = models.CharField(max_length=20)
    etag = models.CharField(max_length=200, blank=True)
    seit = models.DateTimeField(null=True, blank=True)
    letzter_erfolg = models.DateTimeField(null=True, blank=True)
    letzter_versuch = models.DateTimeField(null=True, blank=True)
    fehler = models.CharField(max_length=300, blank=True)
    vollstaendig = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["verbindung", "bereich"], name="abruf_je_bereich"),
        ]


class ExternesObjekt(models.Model):
    """Beobachtung eines GitHub-Objekts mit Quellstand; GitHub bleibt führend."""

    verbindung = models.ForeignKey(RepoVerbindung, on_delete=models.PROTECT, related_name="objekte")
    typ = models.CharField(max_length=12)
    externe_id = models.CharField(max_length=80)
    nummer = models.PositiveIntegerField(null=True, blank=True)
    titel = models.CharField(max_length=300, blank=True)
    zustand = models.CharField(max_length=40, blank=True)
    url = models.URLField(max_length=500, blank=True)
    quell_aktualisiert = models.DateTimeField(null=True, blank=True)
    daten = models.JSONField(default=dict)
    zuletzt_gesehen = models.DateTimeField()
    auftrag = models.ForeignKey(
        Auftrag, null=True, blank=True, on_delete=models.PROTECT, related_name="externe_objekte"
    )

    class Meta:
        ordering = ["-quell_aktualisiert", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["verbindung", "typ", "externe_id"], name="externes_objekt_einmal"
            ),
        ]


class ExterneAktion(models.Model):
    """Von Fred bestätigte Schreibaktion bei GitHub; vor dem Aufruf gespeichert."""

    ENTWURF = "entwurf"
    BESTAETIGT = "bestaetigt"
    IN_AUSFUEHRUNG = "in_ausfuehrung"
    ERFOLGREICH = "erfolgreich"
    TEILWEISE = "teilweise"
    FEHLGESCHLAGEN = "fehlgeschlagen"
    UNKLAR = "unklar"
    KONFLIKT = "konflikt"
    VERWORFEN = "verworfen"
    ZUSTAENDE = [
        (ENTWURF, "Entwurf"),
        (BESTAETIGT, "bestätigt"),
        (IN_AUSFUEHRUNG, "in Ausführung"),
        (ERFOLGREICH, "erfolgreich"),
        (TEILWEISE, "Teilergebnis"),
        (FEHLGESCHLAGEN, "fehlgeschlagen"),
        (UNKLAR, "Ausgang unklar"),
        (KONFLIKT, "Konflikt"),
        (VERWORFEN, "verworfen"),
    ]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="externe_aktionen")
    verbindung = models.ForeignKey(
        RepoVerbindung, on_delete=models.PROTECT, related_name="aktionen_log"
    )
    aktions_id = models.CharField(max_length=32, unique=True)
    art = models.CharField(max_length=20, choices=RepoVerbindung.AKTIONEN)
    revision = models.PositiveIntegerField(default=1)
    nutzlast = models.JSONField()
    nutzlast_hash = models.CharField(max_length=64)
    basis = models.JSONField(default=dict)
    zustand = models.CharField(max_length=16, choices=ZUSTAENDE, default=ENTWURF)
    vorgeschlagen_von_name = models.CharField(max_length=120)
    bindung = models.ForeignKey(
        ChatBindung, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bestaetigt_von = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    bestaetigt_am = models.DateTimeField(null=True, blank=True)
    bestaetigter_hash = models.CharField(max_length=64, blank=True)
    bestaetigte_revision = models.PositiveIntegerField(default=0)
    ausfuehrung_am = models.DateTimeField(null=True, blank=True)
    schritte = models.JSONField(default=list)
    ergebnis_url = models.URLField(max_length=500, blank=True)
    fehler = models.TextField(blank=True)
    angelegt_am = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]


class Besuchsmarke(models.Model):
    """Bis zu welchem Ereignis ein Mensch die Änderungen eines Projekts gesehen hat."""

    benutzer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    projekt = models.ForeignKey(Projekt, on_delete=models.CASCADE, related_name="+")
    gesehen_bis = models.PositiveBigIntegerField(default=0)
    aktualisiert_am = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["benutzer", "projekt"], name="besuchsmarke_einmal"),
        ]


class Kontextbezug(models.Model):
    """Ausgegebener KI-Kontext: Manifest und Hash, keine Inhalte und keine Geheimnisse."""

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="+")
    kontext_id = models.CharField(max_length=32, unique=True)
    erstellt_am = models.DateTimeField(auto_now_add=True)
    inhalt_revision = models.PositiveIntegerField()
    auftrag = models.ForeignKey(
        Auftrag, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    auftrag_revision = models.PositiveIntegerField(default=0)
    manifest = models.JSONField()
    hash = models.CharField(max_length=64)
    akteur_name = models.CharField(max_length=120)

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
    """Ein Auftrag beginnt als Wunsch und reift zum Arbeitspaket. Nummer ist ``A-<id>``."""

    ENTWURF = "ENTWURF"
    FREIGEGEBEN = "FREIGEGEBEN"
    IN_ARBEIT = "IN_ARBEIT"
    ZUR_PRUEFUNG = "ZUR_PRUEFUNG"
    NACHARBEIT = "NACHARBEIT"
    ABGENOMMEN = "ABGENOMMEN"
    BLOCKIERT = "BLOCKIERT"
    KLAERUNG_ERFORDERLICH = "KLAERUNG_ERFORDERLICH"
    PAUSIERT = "PAUSIERT"
    ABGEBROCHEN = "ABGEBROCHEN"
    ZUSTAENDE = [
        (ENTWURF, "Entwurf"),
        (FREIGEGEBEN, "Freigegeben"),
        (IN_ARBEIT, "In Arbeit"),
        (ZUR_PRUEFUNG, "Zur Prüfung"),
        (NACHARBEIT, "Nacharbeit"),
        (ABGENOMMEN, "Abgenommen"),
        (BLOCKIERT, "Blockiert"),
        (KLAERUNG_ERFORDERLICH, "Klärung erforderlich"),
        (PAUSIERT, "Pausiert"),
        (ABGEBROCHEN, "Abgebrochen"),
    ]
    # Zustände, in denen ein Schreibumfang als belegt gilt.
    BELEGENDE_ZUSTAENDE = (
        FREIGEGEBEN,
        IN_ARBEIT,
        NACHARBEIT,
        ZUR_PRUEFUNG,
        BLOCKIERT,
        PAUSIERT,  # Pause gibt den Schreibumfang nicht frei
    )
    # Zustände, in denen ein Bearbeiter zugeordnet ist.
    BEARBEITER_ZUSTAENDE = (IN_ARBEIT, NACHARBEIT, BLOCKIERT)
    ENDZUSTAENDE = (ABGENOMMEN, ABGEBROCHEN)

    PRIO = [("hoch", "hoch"), ("mittel", "mittel"), ("niedrig", "niedrig")]

    projekt = models.ForeignKey(Projekt, on_delete=models.PROTECT, related_name="auftraege")
    modul = models.ForeignKey(
        Modul, null=True, blank=True, on_delete=models.PROTECT, related_name="auftraege"
    )
    titel = models.CharField("Titel", max_length=200)
    zustand = models.CharField("Zustand", max_length=24, choices=ZUSTAENDE, default=ENTWURF)
    zustand_vor_pause = models.CharField(max_length=24, blank=True)
    prioritaet = models.CharField("Priorität", max_length=10, choices=PRIO, default="mittel")
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
    pruefschritte = models.TextField("Ausgeführte Prüfungen (eine je Zeile)", blank=True)
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

    class Meta:
        ordering = ["-zeitpunkt", "-id"]
        verbose_name = "Ereignis"
        verbose_name_plural = "Ereignisse"

    def __str__(self) -> str:
        return f"{self.akteur_name}: {self.ereignis} ({self.objekt_kennung})"

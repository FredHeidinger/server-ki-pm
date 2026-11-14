"""Fachlicher Dienst. Oberfläche und HTTP-Schnittstelle rufen nur diese Funktionen.

Regeln:
- Jede Funktion bekommt den handelnden Akteur und prüft dessen Befugnis selbst.
- Jede Änderung läuft in einer Transaktion und schreibt im selben Commit ein Ereignis.
- Fehler sind ``DienstFehler`` mit einem Text, der dem Menschen angezeigt werden kann.
  ``Konflikt`` ist ein DienstFehler für konkurrierende oder veraltete Anfragen.
"""

import functools
import hashlib
import json
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

from django.apps import apps
from django.contrib.auth.models import AbstractBaseUser
from django.db import IntegrityError, transaction
from django.db.models import F, ProtectedError, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    AUSFUEHRUNG_AKTIV,
    Abhaengigkeit,
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Ereignis,
    Ergebnis,
    FruehereNummer,
    Meilenstein,
    Meldung,
    Modul,
    OrdnerAnfrage,
    Projekt,
    Revision,
    WerkbankAusfuehrung,
)

KUERZEL_MUSTER = re.compile(r"[A-Z][A-Z0-9]{1,7}")


class DienstFehler(Exception):
    """Verständlicher, anzeigbarer Fehler aus einer fachlichen Regel."""


class Konflikt(DienstFehler):
    """Konkurrierende, veraltete oder nicht erlaubte Zustandsänderung."""


class KeinZugriff(DienstFehler):
    """Objekt existiert nicht für diesen Akteur (fremdes Projekt) oder Befugnis fehlt."""


@dataclass(frozen=True)
class Akteur:
    """Wer handelt: ein angemeldeter Mensch oder eine Chat-Bindung."""

    benutzer: AbstractBaseUser | None
    bindung: ChatBindung | None
    name: str
    ist_owner: bool
    projekt: Projekt | None = None  # Projekt der Bindung; bei PC-Zugang aus Ordner oder Angabe
    ordner: str = ""
    # PC-Zugang: KI-Assistent des Owners; legt an, plant und gibt auf Freds Anweisung im Chat frei.
    ist_assistent: bool = False
    # Freds Worte aus dem Chat, die eine Änderung tragen; jedes Ereignis der Anfrage nennt sie.
    fred_sagte: str = ""

    @classmethod
    def aus_benutzer(cls, benutzer: AbstractBaseUser) -> "Akteur":
        return cls(
            benutzer=benutzer,
            bindung=None,
            name=benutzer.get_username(),
            ist_owner=bool(benutzer.is_superuser),
        )

    @classmethod
    def aus_bindung(
        cls, bindung: ChatBindung, projekt: Projekt | None = None, ordner: str = ""
    ) -> "Akteur":
        return cls(
            benutzer=None,
            bindung=bindung,
            name=bindung.name,
            ist_owner=False,
            projekt=projekt or bindung.projekt,
            ordner=ordner,
            ist_assistent=bindung.art == ChatBindung.ART_PC,
        )

    @property
    def ist_mensch(self) -> bool:
        return self.benutzer is not None

    def ist_bearbeiter_von(self, auftrag: Auftrag) -> bool:
        if self.bindung is not None:
            return auftrag.bearbeiter_bindung_id == self.bindung.pk
        if self.benutzer is not None:
            return auftrag.bearbeiter_benutzer_id == self.benutzer.pk
        return False


# --- Hilfen -----------------------------------------------------------------------------


def _ereignis(
    akteur: Akteur,
    *,
    projekt: Projekt | None,
    objekt_typ: str,
    objekt_kennung: str,
    ereignis: str,
    anlass: str = "",
    revision: str = "",
    beobachtet_am=None,
    version=None,
) -> Ereignis:
    """Verlaufseintrag im selben Commit; jedes Ereignis eines Projekts erhöht dessen
    Inhaltsrevision (Lesen, Besuchsmarken und unveränderte Abrufe schreiben keines)."""
    if akteur.fred_sagte:
        anlass = (anlass + " · " if anlass else "") + f"Fred sagte: „{akteur.fred_sagte}“"
    if akteur.ordner:
        anlass = (anlass + " · " if anlass else "") + f"Ordner {akteur.ordner}"
    if projekt is not None:
        Projekt.objects.filter(pk=projekt.pk).update(inhalt_revision=F("inhalt_revision") + 1)
    return Ereignis.objects.create(
        beobachtet_am=beobachtet_am,
        version=version,
        akteur=akteur.benutzer if akteur.benutzer is not None and akteur.benutzer.pk else None,
        akteur_name=akteur.name,
        projekt=projekt,
        objekt_typ=objekt_typ,
        objekt_kennung=objekt_kennung,
        ereignis=ereignis,
        anlass=anlass,
        revision=revision,
    )


def _bearbeiter_abloesen(auftrag: Auftrag) -> None:
    """Bearbeiter entfernen, Identität als „letzter Bearbeiter“ behalten."""
    if auftrag.hat_bearbeiter:
        auftrag.letzter_bearbeiter_name = auftrag.bearbeiter_name
        auftrag.letzter_bearbeiter_bindung = auftrag.bearbeiter_bindung
        auftrag.letzter_bearbeiter_benutzer = auftrag.bearbeiter_benutzer
    auftrag.bearbeiter_bindung = None
    auftrag.bearbeiter_benutzer = None
    auftrag.bearbeiter_name = ""


def _werkbank_aktiv(auftrag: Auftrag) -> bool:
    """Läuft der Auftrag gerade in einer aktiven Ausführung einer KI-Werkbank?"""
    return WerkbankAusfuehrung.objects.filter(
        auftrag_id=auftrag.pk, zustand__in=AUSFUEHRUNG_AKTIV
    ).exists()


def _ohne_aktive_werkbank(handlung: str):
    """Aktive Werkbank-Aufträge ändern nur die Werkbank-Aktionen (Antworten, Bearbeitung ändern,
    Abbrechen): Dieser alte Weg darf sie weder neu planen, verschieben, entkoppeln noch
    vorzeitig abschließen (Auftrag 7.2). Termine und Meilensteine bleiben änderbar."""

    def deko(funktion):
        @functools.wraps(funktion)
        def innen(akteur, auftrag, *args, **kwargs):
            # Prüfung und Wirkung teilen eine Transaktion (BEGIN IMMEDIATE): Startet die Werkbank
            # dazwischen, sieht die Prüfung das, statt dass die Wirkung eine laufende Ausführung trifft.
            with transaction.atomic():
                if _werkbank_aktiv(auftrag):
                    raise Konflikt(
                        f"{auftrag.nummer} läuft auf einer KI-Werkbank: {handlung} geht nur über die "
                        "Aktionen der Werkbank-Ausführung (Antworten, Bearbeitung ändern, Abbrechen)."
                    )
                return funktion(akteur, auftrag, *args, **kwargs)

        return innen

    return deko


def _owner_erforderlich(akteur: Akteur, handlung: str) -> None:
    if not akteur.ist_owner:
        raise KeinZugriff(f"{handlung} darf nur der Owner.")


def _planung_erforderlich(akteur: Akteur, handlung: str) -> None:
    """Anlegen, Planen und Freigeben: Owner oder sein KI-Assistent (PC-Zugang); der Assistent
    gibt nur auf Freds Anweisung im Chat frei."""
    if not (akteur.ist_owner or akteur.ist_assistent):
        raise KeinZugriff(f"{handlung} darf nur der Owner oder sein KI-Assistent (PC-Zugang).")
    if akteur.bindung is not None and not akteur.bindung.aktiv:
        raise KeinZugriff("Zugangsschlüssel ungültig, abgelaufen oder widerrufen.")


def _mensch_erforderlich(akteur: Akteur, handlung: str) -> None:
    if not akteur.ist_mensch:
        raise KeinZugriff(f"{handlung} darf nur ein Mensch.")


def _projekt_sichtbar(akteur: Akteur, projekt: Projekt) -> None:
    """Projektschlüssel sehen nur ihr Projekt; Fremdes gilt als nicht vorhanden. Der Assistent
    (PC-Zugang) sieht alle Projekte."""
    if akteur.bindung is None or akteur.ist_assistent:
        return
    if akteur.projekt is None or akteur.projekt.pk != projekt.pk:
        raise KeinZugriff("Nicht gefunden.")


def zeilen(text: str) -> list[str]:
    return [z.strip() for z in (text or "").splitlines() if z.strip()]


def datum_lesen(wert) -> date | None:
    """ISO-Datum oder ``date``; leer heißt kein Datum."""
    if wert in (None, ""):
        return None
    if isinstance(wert, date):
        return wert
    try:
        return date.fromisoformat(str(wert).strip())
    except ValueError as fehler:
        raise DienstFehler(f"Datum „{wert}“ ist ungültig (z. B. 2026-12-01).") from fehler


def module_aus_text(text: str) -> list[tuple[str, str]]:
    """Zeilen wie ``Kern`` oder ``kern: Kernfunktionen`` in (kennung, name) umwandeln."""
    ergebnis: list[tuple[str, str]] = []
    gesehen: set[str] = set()
    for zeile in zeilen(text):
        if ":" in zeile:
            kennung, name = (teil.strip() for teil in zeile.split(":", 1))
        else:
            kennung, name = zeile, zeile
        kennung = slugify(kennung)
        if not kennung or not name:
            raise DienstFehler(f"Modulzeile „{zeile}“ ergibt keine gültige Kennung.")
        if kennung in gesehen:
            raise DienstFehler(f"Modulkennung „{kennung}“ kommt doppelt vor.")
        gesehen.add(kennung)
        ergebnis.append((kennung, name))
    return ergebnis


def _pfadpraefix(muster: str) -> str:
    """Pfadmuster konservativ auf seinen festen Anfang kürzen: ``src/auth/**`` → ``src/auth``."""
    muster = muster.strip().replace("\\", "/").lower()
    while muster.startswith("./"):
        muster = muster[2:]
    muster = muster.strip("/")
    teile = []
    for teil in muster.split("/"):
        if any(z in teil for z in "*?["):
            break
        teile.append(teil)
    return "/".join(teile)


def muster_ueberschneiden(a: str, b: str) -> bool:
    """Zwei Pfadmuster überschneiden sich, wenn ein fester Anfang den anderen umfasst."""
    pa, pb = _pfadpraefix(a), _pfadpraefix(b)
    if pa == "" or pb == "":
        return True
    return pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")


# --- Projekte, Arbeitsorte, Bindungen -------------------------------------------------------


def kuerzel_vorschlag(name: str) -> str:
    """Kürzel für ein neues Projekt: Anfangsbuchstaben der Wörter, sonst die ersten drei Zeichen;
    ist es vergeben, wird es länger, zuletzt mit einer Ziffer."""
    flach = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    woerter = re.findall(r"[A-Z0-9]+", flach)
    zeichen = "".join(woerter) or "P"
    if not zeichen[0].isalpha():
        zeichen = "P" + zeichen
    vorn = "".join(w[0] for w in woerter)[:4] if len(woerter) > 1 else zeichen[:3]
    belegt = set(Projekt.objects.values_list("kuerzel", flat=True))
    for kandidat in (vorn, *(zeichen[:n] for n in range(3, min(len(zeichen), 8) + 1))):
        if KUERZEL_MUSTER.fullmatch(kandidat) and kandidat not in belegt:
            return kandidat
    n = 2
    while f"{zeichen[:5]}{n}" in belegt:
        n += 1
    return f"{zeichen[:5]}{n}"


def _kuerzel_lesen(kuerzel: str) -> str:
    kuerzel = kuerzel.strip().upper()
    if not KUERZEL_MUSTER.fullmatch(kuerzel):
        raise DienstFehler(
            "Das Kürzel braucht 2 bis 8 Zeichen: zuerst ein Buchstabe, dann Buchstaben A–Z oder "
            "Ziffern, z. B. NEX."
        )
    if Projekt.objects.filter(kuerzel=kuerzel).exists():
        raise DienstFehler(f"Das Kürzel „{kuerzel}“ ist bereits vergeben.")
    return kuerzel


def projekt_registrieren(
    akteur: Akteur,
    *,
    kennung: str,
    name: str,
    beschreibung: str = "",
    module: list[tuple[str, str]] | None = None,
    kuerzel: str = "",
) -> Projekt:
    """Projekt mit optionalen Modulen anlegen. Erteilt keinem Chat Rechte."""
    _planung_erforderlich(akteur, "Projekte registrieren")
    kennung = slugify(kennung)
    if not kennung:
        raise DienstFehler("Die Kennung darf nur Buchstaben, Ziffern und Bindestriche enthalten.")
    if not name.strip():
        raise DienstFehler("Der Name darf nicht leer sein.")
    if Projekt.objects.filter(kennung=kennung).exists():
        raise DienstFehler(f"Die Kennung „{kennung}“ ist bereits vergeben.")
    kuerzel = _kuerzel_lesen(kuerzel) if kuerzel.strip() else kuerzel_vorschlag(name)
    try:
        with transaction.atomic():
            projekt = Projekt.objects.create(
                kennung=kennung,
                name=name.strip(),
                beschreibung=beschreibung.strip(),
                kuerzel=kuerzel,
            )
            for modul_kennung, modul_name in module or []:
                Modul.objects.create(projekt=projekt, kennung=modul_kennung, name=modul_name)
            _ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="Projekt",
                objekt_kennung=projekt.kennung,
                ereignis="Projekt registriert",
                anlass=f"{len(module or [])} Modul(e)" if module else "ohne Module",
            )
    except IntegrityError as fehler:  # gleichzeitig angelegt
        raise DienstFehler(
            f"Die Kennung „{kennung}“ oder das Kürzel „{kuerzel}“ ist bereits vergeben."
        ) from fehler
    return projekt


def modul_anlegen(
    akteur: Akteur, projekt: Projekt, *, kennung: str, name: str, eltern: Modul | None = None
) -> Modul:
    """Modul oder, mit ``eltern``, Untermodul anlegen; beliebig tief."""
    _planung_erforderlich(akteur, "Module anlegen")
    _projekt_sichtbar(akteur, projekt)
    kennung = slugify(kennung)
    if not kennung or not name.strip():
        raise DienstFehler("Kennung und Name sind erforderlich.")
    if eltern is not None:
        if eltern.projekt_id != projekt.pk:
            raise KeinZugriff("Nicht gefunden.")
        if not eltern.aktiv:
            raise DienstFehler(f"Modul „{eltern.name}“ ist archiviert.")
    try:
        with transaction.atomic():
            modul = Modul.objects.create(
                projekt=projekt, kennung=kennung, name=name.strip(), eltern=eltern
            )
            _ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="Modul",
                objekt_kennung=str(modul),
                ereignis="Untermodul angelegt" if eltern else "Modul angelegt",
                anlass=f"unter {eltern.name}" if eltern else "",
            )
    except IntegrityError as fehler:
        raise DienstFehler(f"Modulkennung „{kennung}“ gibt es in diesem Projekt schon.") from fehler
    return modul


def _vorfahren(modul: Modul | None) -> list[int]:
    """Das Modul und seine Elternkette bis zur Wurzel (ids); bricht bei einem Kreis ab."""
    kette: list[int] = []
    while modul is not None and modul.pk not in kette:
        kette.append(modul.pk)
        modul = modul.eltern
    return kette


_UNVERAENDERT = object()


def _modul_revision(modul: Modul, erwartete_revision) -> None:
    if erwartete_revision is not None and int(erwartete_revision) != modul.revision:
        raise Konflikt(
            f"Modul „{modul.name}“ wurde inzwischen geändert (jetzt Revision "
            f"{modul.revision}, gelesen {erwartete_revision})."
        )


def modul_aendern(
    akteur: Akteur,
    modul: Modul,
    *,
    name: str | None = None,
    eltern=_UNVERAENDERT,
    erwartete_revision: int | None = None,
    grund: str = "",
) -> Modul:
    """Modul umbenennen oder umhängen (``eltern`` None = direkt unter das Projekt). Die Kennung
    bleibt, damit Verweise und KI-Aufrufe gültig bleiben. Kein Modul unter sich selbst oder
    eines seiner Untermodule."""
    _planung_erforderlich(akteur, "Module ändern")
    _projekt_sichtbar(akteur, modul.projekt)
    if name is not None and not name.strip():
        raise DienstFehler("Der Name darf nicht leer sein.")
    with transaction.atomic():
        modul = Modul.objects.select_related("projekt", "eltern").get(pk=modul.pk)
        _modul_revision(modul, erwartete_revision)
        teile = []
        if name is not None and name.strip() != modul.name:
            teile.append(f"umbenannt {modul.name} → {name.strip()}")
            modul.name = name.strip()
        if eltern is not _UNVERAENDERT and getattr(eltern, "pk", None) != modul.eltern_id:
            if eltern is not None:
                # Unter Sperre frisch lesen: ein veraltetes Elternobjekt ließe einen Kreis zu.
                eltern = Modul.objects.select_related("eltern").get(pk=eltern.pk)
                if eltern.projekt_id != modul.projekt_id:
                    raise KeinZugriff("Nicht gefunden.")
                if modul.pk in _vorfahren(eltern):
                    raise Konflikt(
                        f"„{eltern.name}“ liegt unter „{modul.name}“; das ergäbe einen Kreis."
                    )
                if not eltern.aktiv:
                    raise DienstFehler(f"Modul „{eltern.name}“ ist archiviert.")
            teile.append(f"umgehängt unter {eltern.name if eltern else 'Gesamtes Projekt'}")
            modul.eltern = eltern
        if not teile:
            return modul
        if grund.strip():
            teile.append(grund.strip())
        modul.revision += 1
        modul.save()
        _ereignis(
            akteur,
            projekt=modul.projekt,
            objekt_typ="Modul",
            objekt_kennung=str(modul),
            ereignis="Modul geändert",
            anlass="; ".join(teile),
            revision=str(modul.revision),
        )
    return modul


def modul_archivieren(
    akteur: Akteur, modul: Modul, *, aktiv: bool, erwartete_revision: int | None = None
) -> Modul:
    """Modul archivieren oder zurückholen; Aufträge und Unterlagen behalten ihr
    Modul. Archivieren nur ohne aktive Untermodule, Zurückholen nur unter aktivem Elternmodul."""
    _planung_erforderlich(akteur, "Module archivieren")
    _projekt_sichtbar(akteur, modul.projekt)
    with transaction.atomic():
        modul = Modul.objects.select_related("projekt", "eltern").get(pk=modul.pk)
        _modul_revision(modul, erwartete_revision)
        if modul.aktiv == aktiv:
            return modul
        if not aktiv and modul.kinder.filter(aktiv=True).exists():
            raise Konflikt(
                f"„{modul.name}“ hat aktive Untermodule; zuerst diese archivieren oder umhängen."
            )
        if aktiv and modul.eltern is not None and not modul.eltern.aktiv:
            raise Konflikt(f"Zuerst das Elternmodul „{modul.eltern.name}“ zurückholen.")
        modul.aktiv = aktiv
        modul.revision += 1
        modul.save()
        _ereignis(
            akteur,
            projekt=modul.projekt,
            objekt_typ="Modul",
            objekt_kennung=str(modul),
            ereignis="Modul wieder aktiv" if aktiv else "Modul archiviert",
            revision=str(modul.revision),
        )
    return modul


def modul_teilbaum(modul: Modul) -> list[int]:
    """Das Modul und alle seine Untermodule (ids), gleich in welchem Zustand."""
    kinder: dict[int | None, list[int]] = {}
    for pk, eltern_id in Modul.objects.filter(projekt_id=modul.projekt_id).values_list(
        "pk", "eltern_id"
    ):
        kinder.setdefault(eltern_id, []).append(pk)
    ergebnis: list[int] = []
    stapel = [modul.pk]
    while stapel:
        pk = stapel.pop()
        if pk in ergebnis:
            continue
        ergebnis.append(pk)
        stapel.extend(kinder.get(pk, []))
    return ergebnis


ZAHLEN_LEER = {"auftraege": 0, "issues": 0, "unterlagen": 0}


@dataclass
class Modulknoten:
    modul: Modul
    tiefe: int
    kinder: list["Modulknoten"]
    zahlen: dict  # Aufträge, Issues, Unterlagen – einschließlich Untermodule


def modulbaum(projekt: Projekt) -> dict:
    """Aktive Module als Baum unter „Gesamtes Projekt“. Die Zahlen je Knoten schließen alle
    Untermodule ein, auch archivierte; „projektweit“ zählt, was keinem Modul zugeordnet ist."""
    from .models import Unterlage

    module = {m.pk: m for m in projekt.module.order_by("name")}
    eigene: dict[int | None, dict] = {}

    def zaehle(modul_id, art):
        eigene.setdefault(modul_id, dict(ZAHLEN_LEER))[art] += 1

    for mid, typ in (
        Auftrag.objects.filter(projekt=projekt)
        .exclude(zustand=Auftrag.ABGEBROCHEN)
        .values_list("modul_id", "typ")
    ):
        zaehle(mid, "issues" if typ == Auftrag.TYP_ISSUE else "auftraege")
    for mid in Unterlage.objects.filter(projekt=projekt, archiviert=False).values_list(
        "modul_id", flat=True
    ):
        zaehle(mid, "unterlagen")
    kinder: dict[int | None, list[Modul]] = {}
    for m in module.values():
        kinder.setdefault(m.eltern_id, []).append(m)

    def summe(m: Modul, weg: frozenset) -> dict:
        # Ganzer Teilbaum, auch archivierte Untermodule; ``weg`` schützt vor Kreisen.
        zahlen = dict(eigene.get(m.pk, ZAHLEN_LEER))
        for k in kinder.get(m.pk, []):
            if k.pk not in weg:
                for art, wert in summe(k, weg | {k.pk}).items():
                    zahlen[art] += wert
        return zahlen

    def knoten(m: Modul, tiefe: int, weg: frozenset) -> Modulknoten:
        weg = weg | {m.pk}
        unter = [
            knoten(k, tiefe + 1, weg) for k in kinder.get(m.pk, []) if k.aktiv and k.pk not in weg
        ]
        return Modulknoten(m, tiefe, unter, summe(m, frozenset({m.pk})))

    wurzeln = [knoten(m, 1, frozenset()) for m in kinder.get(None, []) if m.aktiv]
    gesamt = dict(ZAHLEN_LEER)
    for werte in eigene.values():
        for art, wert in werte.items():
            gesamt[art] += wert
    return {
        "knoten": wurzeln,
        "gesamt": gesamt,
        "projektweit": dict(eigene.get(None, ZAHLEN_LEER)),
        "flach": _flach(wurzeln),
    }


def _flach(knoten: list[Modulknoten]) -> list[Modulknoten]:
    liste: list[Modulknoten] = []
    for k in knoten:
        liste.append(k)
        liste.extend(_flach(k.kinder))
    return liste


def arbeitsort_anlegen(
    akteur: Akteur,
    projekt: Projekt,
    *,
    kennung: str,
    name: str,
    rechner: str = "",
    pfad: str = "",
    repository: str = "",
    modul: Modul | None = None,
) -> Arbeitsort:
    """Arbeitsort anlegen. Legt ihn die KI an, ist er ein Vorschlag: er ordnet keinen Ordner zu
    und zählt nicht für die Freigabe, bis ein Mensch ihn bestätigt. Schlägt die KI einen Ordner
    vor, den das Projekt schon hat, gilt der vorhandene Arbeitsort (keine Doppelanlage)."""
    _planung_erforderlich(akteur, "Arbeitsorte anlegen")
    _projekt_sichtbar(akteur, projekt)
    kennung = slugify(kennung)
    if not kennung or not name.strip():
        raise DienstFehler("Kennung und Name sind erforderlich.")
    _modul_pruefen(modul, projekt)
    if not akteur.ist_mensch and pfad.strip():
        norm = _ordner_normiert(pfad)
        for vorhanden in projekt.arbeitsorte.filter(aktiv=True).exclude(pfad=""):
            if _ordner_normiert(vorhanden.pfad) == norm:
                return vorhanden
    try:
        with transaction.atomic():
            ort = Arbeitsort.objects.create(
                projekt=projekt,
                kennung=kennung,
                name=name.strip(),
                rechner=rechner.strip(),
                pfad=pfad.strip(),
                repository=repository.strip(),
                modul=modul,
                bestaetigt=akteur.ist_mensch,
                vorgeschlagen_von="" if akteur.ist_mensch else akteur.name,
            )
            _ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="Arbeitsort",
                objekt_kennung=str(ort),
                ereignis="Arbeitsort angelegt" if ort.bestaetigt else "Arbeitsort vorgeschlagen",
                anlass=f"{ort.rechner} {ort.pfad}".strip(),
            )
    except IntegrityError as fehler:
        raise DienstFehler(f"Arbeitsortkennung „{kennung}“ gibt es schon.") from fehler
    return ort


def _modul_pruefen(modul: Modul | None, projekt: Projekt) -> None:
    if modul is not None and (modul.projekt_id != projekt.pk or not modul.aktiv):
        raise DienstFehler("Das Modul gehört nicht zu diesem Projekt oder ist archiviert.")


def laufende_eintraege(projekt: Projekt) -> list[Auftrag]:
    return list(projekt.auftraege.filter(zustand=Auftrag.IN_ARBEIT).order_by("id"))


def projekt_archivieren(akteur: Akteur, projekt: Projekt, *, aktiv: bool = False) -> Projekt:
    """Projekt archivieren oder zurückholen. Es wird nichts gelöscht: Einträge, Wissen und Verlauf
    bleiben unverändert, nur Startseite, Chats und Sitzungsstart sehen das Projekt nicht mehr.
    Solange ein Eintrag in Arbeit ist, geht das Archivieren nicht."""
    _planung_erforderlich(akteur, "Projekte archivieren")
    _projekt_sichtbar(akteur, projekt)
    with transaction.atomic():
        projekt = Projekt.objects.get(pk=projekt.pk)
        if projekt.aktiv == aktiv:
            return projekt
        if not aktiv:
            if WerkbankAusfuehrung.objects.filter(
                auftrag__projekt=projekt, zustand__in=AUSFUEHRUNG_AKTIV
            ).exists():
                raise Konflikt(
                    f"„{projekt.name}“ hat eine laufende Werkbank-Ausführung; zuerst abschließen "
                    "oder abbrechen."
                )
            laufend = ", ".join(a.nummer for a in laufende_eintraege(projekt))
            if laufend:
                raise Konflikt(
                    f"„{projekt.name}“ hat Einträge in Arbeit ({laufend}); zuerst abschließen oder "
                    "abbrechen."
                )
        projekt.aktiv = aktiv
        projekt.save(update_fields=["aktiv"])
        _ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="Projekt",
            objekt_kennung=projekt.kennung,
            ereignis="Projekt zurückgeholt" if aktiv else "Projekt archiviert",
        )
    return projekt


def freie_kennung(modell, projekt: Projekt, name: str, ersatz: str) -> str:
    """Kennung aus dem Namen, mit -2, -3 … falls schon vergeben."""
    basis = slugify(name)[:30] or ersatz
    kennung, nr = basis, 2
    while modell.objects.filter(projekt=projekt, kennung=kennung).exists():
        kennung, nr = f"{basis}-{nr}", nr + 1
    return kennung


def arbeitsort_aendern(
    akteur: Akteur,
    ort: Arbeitsort,
    *,
    erwartete_revision: int | None,
    grund: str = "",
    bestaetigen: bool = False,
    **felder,
) -> Arbeitsort:
    """Name, Rechner, Ordner, Repository oder Aktiv ändern. Bestätigt wird ein Vorschlag nur
    ausdrücklich (``bestaetigen``, das Häkchen oder „Speichern und übernehmen“), nie durch bloßes
    Bearbeiten oder Archivieren. Ändert die KI Ordner oder Repository oder holt sie einen Ort zurück, wird
    er wieder ein Vorschlag."""
    _planung_erforderlich(akteur, "Arbeitsorte ändern")
    _projekt_sichtbar(akteur, ort.projekt)
    with transaction.atomic():
        ort = Arbeitsort.objects.select_related("projekt").get(pk=ort.pk)
        if erwartete_revision is not None and int(erwartete_revision) != ort.revision:
            raise Konflikt(
                f"Arbeitsort „{ort.name}“ wurde inzwischen geändert (jetzt Revision "
                f"{ort.revision}, gelesen {erwartete_revision})."
            )
        geaendert = []
        for feld in ("name", "rechner", "pfad", "repository"):
            wert = felder.get(feld)
            if wert is not None and getattr(ort, feld) != str(wert).strip():
                setattr(ort, feld, str(wert).strip())
                geaendert.append(feld)
        if "modul" in felder and (felder["modul"].pk if felder["modul"] else None) != ort.modul_id:
            _modul_pruefen(felder["modul"], ort.projekt)
            ort.modul = felder["modul"]
            geaendert.append("modul")
        if felder.get("aktiv") is not None and bool(felder["aktiv"]) != ort.aktiv:
            ort.aktiv = bool(felder["aktiv"])
            geaendert.append("aktiv")
        if not ort.name:
            raise DienstFehler("Der Name darf nicht leer sein.")
        if akteur.ist_mensch and not ort.bestaetigt and bestaetigen:
            ort.bestaetigt = True
            geaendert.append("bestätigt")
        elif not akteur.ist_mensch and (
            {"pfad", "repository"} & set(geaendert) or ("aktiv" in geaendert and ort.aktiv)
        ):
            ort.bestaetigt = False
            ort.vorgeschlagen_von = akteur.name
        if not geaendert:
            return ort
        ort.revision += 1
        ort.save()
        _ereignis(
            akteur,
            projekt=ort.projekt,
            objekt_typ="Arbeitsort",
            objekt_kennung=str(ort),
            ereignis="Arbeitsort archiviert"
            if not ort.aktiv
            else ("Arbeitsort geändert" if ort.bestaetigt else "Arbeitsort-Vorschlag geändert"),
            anlass="; ".join(t for t in (", ".join(geaendert), grund.strip()) if t),
            revision=str(ort.revision),
        )
        if "bestätigt" in geaendert:
            _abgedeckte_anfragen_erledigen()
        from . import werkbank_aktionen

        werkbank_aktionen.kontext_geaendert(akteur, ort.projekt)
    return ort


def arbeitsort_bestaetigen(
    akteur: Akteur, ort: Arbeitsort, *, erwartete_revision: int | None
) -> Arbeitsort:
    """Den Vorschlag einer KI übernehmen (das Häkchen); nur ein Mensch, und nur genau die
    angezeigte Revision: hat die KI den Vorschlag inzwischen geändert, ist es ein Konflikt."""
    _mensch_erforderlich(akteur, "Arbeitsorte bestätigen")
    _owner_erforderlich(akteur, "Arbeitsorte bestätigen")
    if erwartete_revision is None:
        raise DienstFehler("Die angezeigte Revision des Vorschlags fehlt; Seite neu laden.")
    return arbeitsort_aendern(akteur, ort, erwartete_revision=erwartete_revision, bestaetigen=True)


def arbeitsort_setzen(akteur: Akteur, auftrag: Auftrag, ort: Arbeitsort) -> Revision:
    """Arbeitsort eines Auftrags ändern; alle übrigen Angaben der aktuellen Revision bleiben.
    Gelesen wird unter Sperre aus der Datenbank, nie aus einer womöglich veralteten Instanz.
    Trennt das Lieferketten, lehnt ``auftrag_bearbeiten`` ab; bestätigt wird nur über Planen."""
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().select_related("modul").get(pk=auftrag.pk)
        rev = auftrag.aktuelle_revision
        if rev.arbeitsort_id == ort.pk:
            return rev
        if not ort.aktiv:
            raise DienstFehler(f"Arbeitsort „{ort.name}“ ist archiviert.")
        return auftrag_bearbeiten(
            akteur,
            auftrag,
            titel=auftrag.titel,
            beschreibung=rev.beschreibung,
            ziel=rev.ziel,
            nicht_ziele=rev.nicht_ziele,
            arbeitsort=ort,
            schreibumfang=rev.schreibumfang,
            quellen=rev.quellen,
            abnahmekriterien=rev.abnahmekriterien,
            prioritaet=auftrag.prioritaet,
            modul=auftrag.modul,
            anlass=f"Arbeitsort {ort.name}",
        )


def _schluessel_hash(schluessel: str) -> str:
    return hashlib.sha256(schluessel.encode("utf-8")).hexdigest()


def bindung_anlegen(
    akteur: Akteur,
    projekt: Projekt | None,
    *,
    name: str,
    gueltig_tage: int = 90,
    notiz: str = "",
) -> tuple[ChatBindung, str]:
    """Zugang anlegen: mit Projekt ein Chat-Schlüssel, ohne Projekt ein PC-Zugang für alle
    Projekte (Projekt aus dem Arbeitsordner). Gibt den Klartextschlüssel genau einmal zurück."""
    _owner_erforderlich(akteur, "Zugänge anlegen")
    if not name.strip():
        raise DienstFehler("Der Name darf nicht leer sein.")
    if not 1 <= gueltig_tage <= 365:
        raise DienstFehler("Die Gültigkeit muss zwischen 1 und 365 Tagen liegen.")
    praefix = secrets.token_hex(6)
    geheim = secrets.token_urlsafe(32)
    schluessel = f"kipm_{praefix}_{geheim}"
    with transaction.atomic():
        bindung = ChatBindung.objects.create(
            projekt=projekt,
            art=ChatBindung.ART_PROJEKT if projekt else ChatBindung.ART_PC,
            name=name.strip(),
            notiz=notiz.strip(),
            praefix=praefix,
            schluessel_hash=_schluessel_hash(schluessel),
            gueltig_bis=timezone.now() + timedelta(days=gueltig_tage),
        )
        _ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="Bindung",
            objekt_kennung=bindung.name,
            ereignis="Zugang angelegt",
            anlass=f"{bindung.get_art_display()}, gültig {gueltig_tage} Tage",
        )
    return bindung, schluessel


def bindung_widerrufen(akteur: Akteur, bindung: ChatBindung, grund: str = "") -> None:
    _owner_erforderlich(akteur, "Zugänge widerrufen")
    if bindung.widerrufen_am is not None:
        raise DienstFehler("Diese Bindung ist bereits widerrufen.")
    with transaction.atomic():
        bindung.widerrufen_am = timezone.now()
        bindung.save(update_fields=["widerrufen_am"])
        _ereignis(
            akteur,
            projekt=bindung.projekt,
            objekt_typ="Bindung",
            objekt_kennung=bindung.name,
            ereignis="Zugang widerrufen",
            anlass=grund,
        )


# Alle Tabellen, die einen Zugang vor dem Löschen schützen, auch die früherer Stufen.
BINDUNG_VERWEISE = (
    ("Auftrag", "bearbeiter_bindung"),
    ("Auftrag", "letzter_bearbeiter_bindung"),
    ("Meldung", "bindung"),
    ("Ergebnis", "bindung"),
    ("Installation", "bindung"),
    ("Nachweis", "bindung"),
    ("ExterneAktion", "bindung"),
    ("WerkbankInstanz", "bindung"),  # eigene Regel in bindung_benutzt
)


def bindung_benutzt(bindung: ChatBindung) -> bool:
    """Hängen Aufträge, Meldungen, Ergebnisse oder frühere Meldungen an dem Zugang, bleibt er für
    den Verlauf. Bei einer Werkbank zählt deren Verwaltung (``werkbank.instanz_benutzt``)."""
    if bindung.art == ChatBindung.ART_WERKBANK:
        from . import werkbank

        instanz = getattr(bindung, "werkbank", None)
        return instanz is not None and werkbank.instanz_benutzt(instanz)
    return any(
        apps.get_model("steuerung", modell).objects.filter(**{feld: bindung}).exists()
        for modell, feld in BINDUNG_VERWEISE
    )


def bindung_loeschen(akteur: Akteur, bindung: ChatBindung) -> None:
    """Unbenutzten Zugang endgültig löschen; benutzte werden nur widerrufen (Archiv)."""
    _owner_erforderlich(akteur, "Zugänge löschen")
    if bindung.art == ChatBindung.ART_WERKBANK:
        from . import werkbank

        instanz = getattr(bindung, "werkbank", None)
        if instanz is not None:
            werkbank.instanz_loeschen(akteur, instanz)
            return
    if bindung_benutzt(bindung):
        raise DienstFehler(
            f"„{bindung.name}“ hat schon an Aufträgen gearbeitet und bleibt für den Verlauf "
            "erhalten. Widerrufen schiebt ihn ins Archiv."
        )
    try:
        with transaction.atomic():
            _ereignis(
                akteur,
                projekt=bindung.projekt,
                objekt_typ="Bindung",
                objekt_kennung=bindung.name,
                ereignis="Zugang gelöscht",
            )
            bindung.delete()
    except ProtectedError as fehler:  # Der Chat hat gerade erst zu arbeiten begonnen.
        raise DienstFehler(
            f"„{bindung.name}“ hat gerade zu arbeiten begonnen und bleibt für den Verlauf erhalten."
        ) from fehler


# --- Ordner ohne Projekt -------------------------------------------------------------------


def ordner_unbekannt_melden(bindung: ChatBindung, ordner: str) -> None:
    """Unbekannten Arbeitsordner unter „Konto & KI verbinden“ zur Zuordnung vorlegen (einmal je Ordner)."""
    norm = _ordner_normiert(ordner)
    if not norm:
        return
    OrdnerAnfrage.objects.update_or_create(
        normiert=norm[:300], defaults={"ordner": ordner.strip()[:300], "bindung": bindung}
    )


def ordner_zuordnen(akteur: Akteur, anfrage: OrdnerAnfrage, projekt: Projekt) -> Arbeitsort:
    """Ordner dem Projekt zuordnen; erledigt alle Anfragen, die er abdeckt. Hat das Projekt den
    Ordner schon, gilt der vorhandene Arbeitsort, ein Vorschlag der KI wird damit übernommen."""
    _owner_erforderlich(akteur, "Ordner zuordnen")
    ordner = anfrage.ordner.rstrip("/\\")
    norm = _ordner_normiert(ordner)
    with transaction.atomic():
        vorhanden = next(
            (
                o
                for o in projekt.arbeitsorte.filter(aktiv=True).order_by("-bestaetigt", "pk")
                if _ordner_normiert(o.pfad) == norm
            ),
            None,
        )
        if vorhanden is not None:
            ort = arbeitsort_aendern(akteur, vorhanden, erwartete_revision=None, bestaetigen=True)
        else:
            name = ordner.replace("\\", "/").rsplit("/", 1)[-1] or ordner
            ort = arbeitsort_anlegen(
                akteur,
                projekt,
                kennung=freie_kennung(Arbeitsort, projekt, name, "ordner"),
                name=name,
                rechner=anfrage.bindung.name,
                pfad=ordner,
            )
        _abgedeckte_anfragen_erledigen()
    return ort


def _abgedeckte_anfragen_erledigen() -> None:
    """Ordneranfragen entfernen, deren Ordner ein bestätigter Arbeitsort inzwischen abdeckt."""
    for offen in OrdnerAnfrage.objects.all():
        if projekt_fuer_ordner(offen.ordner)[0] is not None:
            offen.delete()


def ordner_verwerfen(akteur: Akteur, anfrage: OrdnerAnfrage) -> None:
    _owner_erforderlich(akteur, "Ordneranfragen verwerfen")
    anfrage.delete()


def bindung_pruefen(schluessel: str) -> ChatBindung | None:
    """Schlüssel prüfen und „zuletzt aktiv“ vermerken; gibt die aktive Bindung zurück oder None.
    Ein Werkbank-Schlüssel gilt hier nie: Chat-Wege liefert er nicht."""
    bindung = bindung_finden(schluessel)
    if bindung is not None:
        ChatBindung.objects.filter(pk=bindung.pk).update(zuletzt_aktiv=timezone.now())
    return bindung


def _bindung_laden(schluessel: str) -> ChatBindung | None:
    teile = (schluessel or "").strip().split("_", 2)
    if len(teile) != 3 or teile[0] != "kipm":
        return None
    bindung = ChatBindung.objects.select_related("projekt").filter(praefix=teile[1]).first()
    if bindung is None or not bindung.aktiv:
        return None
    if bindung.projekt is not None and not bindung.projekt.aktiv:
        return None
    if not secrets.compare_digest(bindung.schluessel_hash, _schluessel_hash(schluessel.strip())):
        return None
    return bindung


def bindung_finden(schluessel: str) -> ChatBindung | None:
    """Schlüssel nur lesend prüfen; gibt die aktive Chat- oder PC-Bindung zurück oder None. Nie
    Details nach außen. Werkbank-Schlüssel liefert nur ``werkbank_bindung_finden``."""
    bindung = _bindung_laden(schluessel)
    if bindung is None or bindung.art == ChatBindung.ART_WERKBANK:
        return None
    return bindung


def werkbank_bindung_finden(schluessel: str) -> ChatBindung | None:
    """Die aktive Bindung der Art „werkbank“ zu diesem Schlüssel, sonst None (nur lesend). Auch
    die Chat-Wege nutzen das, um einen Werkbank-Schlüssel verständlich abzuweisen."""
    bindung = _bindung_laden(schluessel)
    return bindung if bindung is not None and bindung.art == ChatBindung.ART_WERKBANK else None


WERKBANK_ABWEISUNG = (
    "Dieser Schlüssel gehört einer KI-Werkbank und gilt nur für die Werkbank-Schnittstelle "
    "(/api/werkbank/). Für einen Chat legt der Owner unter „Konto & KI verbinden“ einen "
    "eigenen Zugang an."
)


# --- Aufträge: Sicht und Reifegrad ----------------------------------------------------------


def auftrag_id_aus_nummer(text) -> int:
    """Der Schlüssel zu NEX-12 oder zur früheren Schreibweise A-171 und 171. Prüft keine Befugnis:
    Das tut ``auftrag_holen``, und eine unbekannte Nummer verrät nichts über fremde Projekte."""
    wert = str(text).strip().upper()
    if wert.isdigit():
        return int(wert)
    treffer = re.fullmatch(r"([A-Z][A-Z0-9]{0,7})-(\d+)", wert)
    if not treffer:
        raise DienstFehler(f"Ungültige Nummer: {text}. Erwartet wird z. B. NEX-12 oder A-171.")
    if treffer.group(1) == "A":
        return int(treffer.group(2))
    pk = (
        Auftrag.objects.filter(
            projekt__kuerzel=treffer.group(1), projekt_nummer=int(treffer.group(2))
        )
        .values_list("pk", flat=True)
        .first()
    )
    if pk is None:
        # Nach einem Projektwechsel führt die alte Nummer weiter zum selben Eintrag.
        pk = (
            FruehereNummer.objects.filter(
                projekt__kuerzel=treffer.group(1), projekt_nummer=int(treffer.group(2))
            )
            .values_list("auftrag_id", flat=True)
            .first()
        )
    if pk is None:
        raise KeinZugriff("Nicht gefunden.")
    return pk


def auftrag_holen(akteur: Akteur, auftrag_id: int) -> Auftrag:
    """Auftrag für den Akteur laden; fremde Projekte sind für Chats nicht vorhanden."""
    auftrag = (
        Auftrag.objects.select_related("projekt", "modul", "bearbeiter_bindung")
        .filter(pk=auftrag_id)
        .first()
    )
    if auftrag is None:
        raise KeinZugriff("Nicht gefunden.")
    _projekt_sichtbar(akteur, auftrag.projekt)
    return auftrag


def _ordner_normiert(pfad: str) -> str:
    return pfad.strip().replace("\\", "/").rstrip("/").lower()


def projekt_fuer_ordner(ordner: str) -> tuple[Projekt | None, Arbeitsort | None]:
    """Projekt und Arbeitsort, dessen Ordnerpfad den Arbeitsordner umfasst (längster Treffer).
    Unbestätigte Vorschläge einer KI ordnen nichts zu."""
    norm = _ordner_normiert(ordner)
    if not norm:
        return None, None
    bester: Arbeitsort | None = None
    for ort in (
        Arbeitsort.objects.filter(aktiv=True, bestaetigt=True, projekt__aktiv=True)
        .exclude(pfad="")
        .select_related("projekt")
    ):
        pfad = _ordner_normiert(ort.pfad)
        if norm == pfad or norm.startswith(pfad + "/"):
            if bester is None or len(pfad) > len(_ordner_normiert(bester.pfad)):
                bester = ort
    return (bester.projekt, bester) if bester else (None, None)


def auftraege_fuer_bindung(
    bindung: ChatBindung,
    projekt: Projekt | None,
    *,
    nur_meine: bool = False,
    alle: bool = False,
    typ: str | None = None,
):
    """Aufträge des Projekts (ohne Projekt: aller Projekte): standardmäßig alle offenen,
    wahlweise nur übernehmbare und eigene, oder einschließlich abgeschlossener."""
    qs = Auftrag.objects.select_related("projekt", "modul", "meilenstein")
    qs = qs.filter(projekt=projekt) if projekt is not None else qs.filter(projekt__aktiv=True)
    if typ:
        qs = qs.filter(typ=typ)
    if nur_meine:
        qs = qs.filter(
            Q(zustand=Auftrag.FREIGEGEBEN, bearbeiter_bindung=None, bearbeiter_benutzer=None)
            | Q(bearbeiter_bindung=bindung)
        )
    if not alle:
        qs = qs.exclude(zustand__in=Auftrag.ENDZUSTAENDE)
    return qs.order_by("prioritaet", "id")


def _titel_normiert(titel: str) -> str:
    return " ".join(titel.lower().split())


def doppelte_auftraege(projekt: Projekt, titel: str) -> list[Auftrag]:
    """Offene Aufträge desselben Projekts mit gleichem Titel (ohne Groß-/Kleinschreibung)."""
    norm = _titel_normiert(titel)
    return [
        a
        for a in Auftrag.objects.filter(projekt=projekt).exclude(zustand__in=Auftrag.ENDZUSTAENDE)
        if _titel_normiert(a.titel) == norm
    ]


@dataclass(frozen=True)
class Pruefpunkt:
    erfuellt: bool
    text: str
    hinweis: str = ""
    vorgaenger_angebot: Auftrag | None = None
    # Oberfläche zeigt an diesem Punkt die Auswahl vorhandener Arbeitsorte und „+ Arbeitsort“.
    arbeitsort_wahl: bool = False


def _ueberschneidungen(
    auftrag: Auftrag, revision: Revision, *, projekt: Projekt | None = None, ort_id=None
) -> list[tuple[Auftrag, str, str]]:
    """Andere belegende Aufträge im selben Arbeitsort mit überschneidenden Mustern; mit ``projekt``
    und ``ort_id`` an einem Ziel, an dem der Auftrag noch nicht steht."""
    ort_id = ort_id if ort_id is not None else revision.arbeitsort_id
    if ort_id is None or not revision.muster:
        return []
    vorgaenger_ids = set(auftrag.abhaengigkeiten.values_list("vorgaenger_id", flat=True))
    treffer: list[tuple[Auftrag, str, str]] = []
    andere = (
        Auftrag.objects.filter(
            projekt=projekt or auftrag.projekt, zustand__in=Auftrag.BELEGENDE_ZUSTAENDE
        )
        .exclude(pk=auftrag.pk)
        .exclude(pk__in=vorgaenger_ids)
    )
    for anderer in andere:
        rev = anderer.revisionen.filter(nummer=anderer.revision_nr).first()
        if rev is None or rev.arbeitsort_id != ort_id:
            continue
        for m1 in revision.muster:
            for m2 in rev.muster:
                if muster_ueberschneiden(m1, m2):
                    treffer.append((anderer, m1, m2))
    return treffer


def _erzeugt_zyklus(auftrag: Auftrag, neuer_vorgaenger: Auftrag) -> bool:
    """Wahr, wenn ``neuer_vorgaenger`` direkt oder indirekt von ``auftrag`` abhängt."""
    gesehen: set[int] = set()
    stapel = [neuer_vorgaenger.pk]
    while stapel:
        aktuell = stapel.pop()
        if aktuell == auftrag.pk:
            return True
        if aktuell in gesehen:
            continue
        gesehen.add(aktuell)
        stapel.extend(
            Abhaengigkeit.objects.filter(auftrag_id=aktuell).values_list("vorgaenger_id", flat=True)
        )
    return False


def _arbeitsort_punkt(rev: Revision) -> Pruefpunkt:
    ort = rev.arbeitsort
    if ort is None:
        return Pruefpunkt(False, "Arbeitsort angegeben", "Wo liegt der Code?", arbeitsort_wahl=True)
    if not ort.aktiv:
        return Pruefpunkt(
            False,
            "Arbeitsort angegeben",
            f"„{ort.name}“ ist archiviert; einen anderen wählen.",
            arbeitsort_wahl=True,
        )
    if not ort.bestaetigt:
        return Pruefpunkt(
            False,
            "Arbeitsort bestätigt",
            f"„{ort.name}“ ist ein Vorschlag von {ort.vorgeschlagen_von or 'einer KI'}.",
            arbeitsort_wahl=True,
        )
    return Pruefpunkt(True, "Arbeitsort angegeben")


def freigabepruefung(auftrag: Auftrag) -> list[Pruefpunkt]:
    """Alle Bedingungen, die für Freigabe und Übernahme gelten, als anzeigbare Liste."""
    rev = auftrag.aktuelle_revision
    punkte = [
        Pruefpunkt(bool(rev.ziel.strip()), "Ziel vorhanden", "Ein beobachtbares Ergebnis."),
        Pruefpunkt(
            bool(rev.nicht_ziele.strip()),
            "Nicht-Ziele angegeben",
            "Mindestens ein Eintrag oder „keine“.",
        ),
        _arbeitsort_punkt(rev),
        Pruefpunkt(
            bool(rev.muster),
            "Schreibumfang angegeben",
            "Welche Dateien oder Ordner darf der Bearbeiter ändern?",
        ),
        Pruefpunkt(
            bool(rev.kriterien),
            "Mindestens ein Abnahmekriterium",
            "Je Zeile eine prüfbare Aussage.",
        ),
    ]
    offene = [
        a.vorgaenger
        for a in auftrag.abhaengigkeiten.select_related("vorgaenger")
        if a.vorgaenger.zustand != Auftrag.ERLEDIGT
    ]
    punkte.append(
        Pruefpunkt(
            not offene,
            "Vorgänger erledigt"
            if auftrag.abhaengigkeiten.exists()
            else "Abhängigkeiten erfüllt (keine)",
            ", ".join(f"{v.nummer} ist {v.get_zustand_display()}" for v in offene),
        )
    )
    ueberschneidungen = _ueberschneidungen(auftrag, rev)
    if ueberschneidungen:
        anderer, m1, m2 = ueberschneidungen[0]
        angebot = None if _erzeugt_zyklus(auftrag, anderer) else anderer
        punkte.append(
            Pruefpunkt(
                False,
                f"Überschneidung mit {anderer.nummer} ({anderer.get_zustand_display()})",
                f"„{m1}“ überschneidet „{m2}“. Ausweg: {anderer.nummer} als Vorgänger eintragen "
                "oder den Umfang einschränken.",
                angebot,
            )
        )
    else:
        punkte.append(Pruefpunkt(True, "Keine Überschneidung mit laufenden Aufträgen"))
    return punkte


def freigabefaehig(auftrag: Auftrag) -> bool:
    return all(p.erfuellt for p in freigabepruefung(auftrag))


# --- Aufträge: Anlegen und Bearbeiten -------------------------------------------------------


def wunsch_erfassen(
    akteur: Akteur,
    projekt: Projekt,
    *,
    titel: str,
    beschreibung: str,
    modul: Modul | None = None,
    prioritaet: str = "mittel",
    erzwingen: bool = False,
    typ: str = Auftrag.TYP_AUFTRAG,
    planung: dict | None = None,
    meilenstein: Meilenstein | None = None,
    faellig=None,
) -> Auftrag:
    """Auftrag im Entwurf anlegen. Menschen und Chats des Projekts; mit ``planung`` (Ziel,
    Nicht-Ziele, Arbeitsort, Schreibumfang, Quellen, Abnahmekriterien) gleich geplant, das dürfen
    nur Owner und Assistent. Gleicher Titel wie ein offener Auftrag gilt als Doppelung, außer
    ``erzwingen``."""
    _projekt_sichtbar(akteur, projekt)
    faellig = datum_lesen(faellig)
    planung = {k: v for k, v in (planung or {}).items() if v not in (None, "")}
    if planung:
        _planung_erforderlich(akteur, "Aufträge planen")
        if set(planung) - set(PLANFELDER):
            fremd = ", ".join(sorted(set(planung) - set(PLANFELDER)))
            raise DienstFehler(f"Unbekanntes Planungsfeld: {fremd}")
        ort = planung.get("arbeitsort")
        if ort is not None and ort.projekt_id != projekt.pk:
            raise DienstFehler("Der Arbeitsort gehört nicht zu diesem Projekt.")
    if not titel.strip():
        raise DienstFehler("Der Titel darf nicht leer sein.")
    if modul is not None and modul.projekt_id != projekt.pk:
        raise DienstFehler("Das Modul gehört nicht zu diesem Projekt.")
    if meilenstein is not None and meilenstein.projekt_id != projekt.pk:
        raise DienstFehler("Der Meilenstein gehört nicht zu diesem Projekt.")
    if typ not in dict(Auftrag.TYPEN):
        raise DienstFehler("Die Art muss „auftrag“ oder „issue“ sein.")
    if not erzwingen:
        doppelt = doppelte_auftraege(projekt, titel)
        if doppelt:
            raise Konflikt(
                "Gleicher Titel wie offener Auftrag "
                + ", ".join(a.nummer for a in doppelt)
                + "; zuerst dort nachsehen oder das Anlegen ausdrücklich erzwingen."
            )
    with transaction.atomic():
        # Die Transaktion hält die Schreibsperre (IMMEDIATE): Zwei Anlagen bekommen nie dieselbe Zahl.
        Projekt.objects.filter(pk=projekt.pk).update(naechste_nummer=F("naechste_nummer") + 1)
        nummer = Projekt.objects.values_list("naechste_nummer", flat=True).get(pk=projekt.pk) - 1
        auftrag = Auftrag.objects.create(
            projekt=projekt,
            projekt_nummer=nummer,
            modul=modul,
            meilenstein=meilenstein,
            faellig=faellig,
            titel=titel.strip(),
            prioritaet=prioritaet,
            typ=typ,
            revision_nr=1,
        )
        Revision.objects.create(
            auftrag=auftrag,
            nummer=1,
            beschreibung=beschreibung.strip(),
            ziel=str(planung.get("ziel", "")).strip(),
            nicht_ziele=str(planung.get("nicht_ziele", "")).strip(),
            arbeitsort=planung.get("arbeitsort"),
            schreibumfang="\n".join(zeilen(planung.get("schreibumfang", ""))),
            quellen=str(planung.get("quellen", "")).strip(),
            abnahmekriterien="\n".join(zeilen(planung.get("abnahmekriterien", ""))),
            erstellt_von_name=akteur.name,
            anlass="Angelegt und geplant" if planung else "Angelegt",
        )
        wort = "Issue" if typ == Auftrag.TYP_ISSUE else "Auftrag"
        anlass = "geplant" if planung else ""
        if meilenstein is not None:
            anlass = "; ".join(t for t in (anlass, f"Meilenstein {meilenstein.nummer}") if t)
        if faellig is not None:
            anlass = "; ".join(t for t in (anlass, f"fällig am {faellig:%d.%m.%Y}") if t)
        _ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"{wort} angelegt",
            anlass=anlass,
            revision="1",
        )
    return auftrag


PLANFELDER = ("ziel", "nicht_ziele", "arbeitsort", "schreibumfang", "quellen", "abnahmekriterien")


def auftrag_anlegen(
    akteur: Akteur,
    projekt: Projekt,
    *,
    titel: str,
    beschreibung: str = "",
    modul: Modul | None = None,
    prioritaet: str = "mittel",
    typ: str = Auftrag.TYP_AUFTRAG,
    erzwingen: bool = False,
    planung: dict | None = None,
    freigeben: bool = False,
    meilenstein: Meilenstein | None = None,
    faellig=None,
) -> Auftrag:
    """Auftrag in einem Schritt anlegen, planen und auf Anweisung freigeben. Alles oder nichts:
    scheitert die Freigabeprüfung, wird auch nichts angelegt."""
    with transaction.atomic():
        auftrag = wunsch_erfassen(
            akteur,
            projekt,
            titel=titel,
            beschreibung=beschreibung,
            modul=modul,
            prioritaet=prioritaet,
            erzwingen=erzwingen,
            typ=typ,
            planung=planung,
            meilenstein=meilenstein,
            faellig=faellig,
        )
        if freigeben:
            auftrag_freigeben(akteur, auftrag)
    auftrag.refresh_from_db()
    return auftrag


BEARBEITBARE_ZUSTAENDE = (
    Auftrag.ENTWURF,
    Auftrag.FREIGEGEBEN,
    Auftrag.IN_ARBEIT,
    Auftrag.BLOCKIERT,
)


@_ohne_aktive_werkbank("Neu planen")
def auftrag_bearbeiten(
    akteur: Akteur,
    auftrag: Auftrag,
    *,
    titel: str,
    beschreibung: str,
    ziel: str,
    nicht_ziele: str,
    arbeitsort: Arbeitsort | None,
    schreibumfang: str,
    quellen: str,
    abnahmekriterien: str,
    prioritaet: str,
    modul: Modul | None,
    anlass: str = "",
    typ: str | None = None,
    lieferketten_bestaetigt: bool = False,
) -> Revision:
    """Neue Revision anlegen. Freigabe und laufende Übernahme erlöschen; der Auftrag geht zurück
    in den Entwurf und braucht eine neue Freigabe. Trennt die Revision offene technische
    Lieferketten (wartende Verbraucher oder eigener Lieferbedarf), braucht es
    ``lieferketten_bestaetigt``; dann hält der Verlauf fest, welche Bedarfe verwaist sind."""
    from . import lieferungen

    _planung_erforderlich(akteur, "Aufträge bearbeiten")
    if auftrag.zustand not in BEARBEITBARE_ZUSTAENDE:
        raise Konflikt(
            f"Ein Auftrag im Zustand „{auftrag.get_zustand_display()}“ kann nicht geändert werden."
        )
    if arbeitsort is not None and arbeitsort.projekt_id != auftrag.projekt_id:
        raise DienstFehler("Der Arbeitsort gehört nicht zu diesem Projekt.")
    if modul is not None and modul.projekt_id != auftrag.projekt_id:
        raise DienstFehler("Das Modul gehört nicht zu diesem Projekt.")
    if not titel.strip():
        raise DienstFehler("Der Titel darf nicht leer sein.")
    if typ is not None and typ not in dict(Auftrag.TYPEN):
        raise DienstFehler("Die Art muss „auftrag“ oder „issue“ sein.")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.zustand not in BEARBEITBARE_ZUSTAENDE:
            raise Konflikt(
                f"Der Auftrag ist inzwischen „{auftrag.get_zustand_display()}“ "
                "und kann nicht geändert werden."
            )
        ketten = lieferungen.betroffene_ketten(auftrag)
        if ketten and lieferketten_bestaetigt is not True:
            raise Konflikt(lieferungen.warnung_bearbeiten(auftrag, ketten))
        nummer = auftrag.revision_nr + 1
        rev = Revision.objects.create(
            auftrag=auftrag,
            nummer=nummer,
            beschreibung=beschreibung.strip(),
            ziel=ziel.strip(),
            nicht_ziele=nicht_ziele.strip(),
            arbeitsort=arbeitsort,
            schreibumfang="\n".join(zeilen(schreibumfang)),
            quellen=quellen.strip(),
            abnahmekriterien="\n".join(zeilen(abnahmekriterien)),
            erstellt_von_name=akteur.name,
            anlass=anlass.strip() or "Bearbeitet",
        )
        auftrag.titel = titel.strip()
        auftrag.prioritaet = prioritaet
        auftrag.modul = modul
        if typ is not None:
            auftrag.typ = typ
        auftrag.revision_nr = nummer
        folge = ""
        if auftrag.zustand == Auftrag.FREIGEGEBEN:
            auftrag.zustand = Auftrag.ENTWURF
            auftrag.freigegeben_am = None
            folge = "Freigabe erloschen"
            if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
                from . import werkbank

                werkbank.freigabe_aufgehoben(auftrag)
        elif auftrag.zustand in Auftrag.BEARBEITER_ZUSTAENDE:
            _bearbeiter_abloesen(auftrag)
            auftrag.zustand = Auftrag.ENTWURF
            auftrag.freigegeben_am = None
            folge = f"Übernahme von {auftrag.letzter_bearbeiter_name} erloschen, zurück in Entwurf"
        auftrag.save()
        getrennt = f"Lieferketten getrennt: {lieferungen.ketten_text(ketten)}" if ketten else ""
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Revision {nummer} angelegt",
            anlass="; ".join(t for t in (rev.anlass, folge, getrennt) if t),
            revision=str(nummer),
        )
        for k in ketten:
            if k["anbieter"] != auftrag.nummer:
                continue  # eigener Lieferbedarf: steht im Ereignis oben
            _ereignis(
                akteur,
                projekt=auftrag.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=k["verbraucher"],
                ereignis="Technischer Lieferbedarf verwaist",
                anlass=f"{auftrag.nummer} hat jetzt Revision {nummer}; der Bedarf "
                f"{k['lieferkennung']} war an Revision {nummer - 1} gebunden. Neu binden kann "
                "der Manager (kipm_lieferbedarf_festlegen).",
                revision=str(k["verbraucher_revision"]),
            )
    return rev


def meilenstein_zuordnen(akteur: Akteur, auftrag: Auftrag, meilenstein: Meilenstein | None) -> None:
    """Auftrag einem Meilenstein des eigenen Projekts zuordnen oder ihn lösen. In jedem Zustand
    möglich; ändert weder Revision noch Freigabe noch die Zeit „zuletzt erledigt“."""
    _planung_erforderlich(akteur, "Meilensteine zuordnen")
    _projekt_sichtbar(akteur, auftrag.projekt)
    if meilenstein is not None and meilenstein.projekt_id != auftrag.projekt_id:
        raise DienstFehler("Der Meilenstein gehört nicht zu diesem Projekt.")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        neu = meilenstein.pk if meilenstein is not None else None
        if auftrag.meilenstein_id == neu:
            return
        auftrag.meilenstein = meilenstein
        auftrag.save(update_fields=["meilenstein"])
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Meilenstein {meilenstein.nummer} zugeordnet"
            if meilenstein is not None
            else "Meilenstein gelöst",
            anlass=meilenstein.name if meilenstein is not None else "",
            revision=str(auftrag.revision_nr),
        )


def faellig_setzen(akteur: Akteur, auftrag: Auftrag, wert) -> None:
    """Fälligkeit setzen, leer entfernt sie. Chats des Projekts, von Hand nur der Owner; nur an
    offenen Einträgen. Ändert weder Revision noch Freigabe noch die Zeit „zuletzt erledigt“."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    if akteur.bindung is None:
        _owner_erforderlich(akteur, "Fälligkeit ändern")
    faellig = datum_lesen(wert)
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        if auftrag.ist_endzustand:
            raise Konflikt(
                f"{auftrag.nummer} ist „{auftrag.get_zustand_display()}“ und braucht keinen Termin."
            )
        if auftrag.faellig == faellig:
            return
        auftrag.faellig = faellig
        auftrag.save(update_fields=["faellig"])
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Fällig am {faellig:%d.%m.%Y} gesetzt" if faellig else "Termin entfernt",
            revision=str(auftrag.revision_nr),
        )


@_ohne_aktive_werkbank("Abhängigkeiten ändern")
def vorgaenger_eintragen(
    akteur: Akteur, auftrag: Auftrag, vorgaenger: Auftrag, grund: str = ""
) -> None:
    _planung_erforderlich(akteur, "Abhängigkeiten eintragen")
    if vorgaenger.pk == auftrag.pk:
        raise DienstFehler("Ein Auftrag kann nicht von sich selbst abhängen.")
    if vorgaenger.projekt_id != auftrag.projekt_id:
        raise DienstFehler("Vorgänger müssen zum selben Projekt gehören.")
    try:
        with transaction.atomic():
            # BEGIN IMMEDIATE sperrt Schreiber; Zyklus- und Doppelprüfung sind damit atomar.
            Auftrag.objects.select_for_update().get(pk=auftrag.pk)
            if _erzeugt_zyklus(auftrag, vorgaenger):
                raise Konflikt(
                    f"{vorgaenger.nummer} hängt bereits von {auftrag.nummer} ab; "
                    "das ergäbe einen Kreis."
                )
            Abhaengigkeit.objects.create(
                auftrag=auftrag, vorgaenger=vorgaenger, grund=grund.strip()
            )
    except IntegrityError as fehler:
        raise DienstFehler(
            f"{vorgaenger.nummer} ist bereits als Vorgänger eingetragen."
        ) from fehler
    with transaction.atomic():
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Vorgänger {vorgaenger.nummer} eingetragen",
            anlass=grund,
            revision=str(auftrag.revision_nr),
        )


@_ohne_aktive_werkbank("Abhängigkeiten ändern")
def vorgaenger_entfernen(akteur: Akteur, auftrag: Auftrag, vorgaenger: Auftrag) -> None:
    _planung_erforderlich(akteur, "Abhängigkeiten entfernen")
    with transaction.atomic():
        geloescht, _ = Abhaengigkeit.objects.filter(auftrag=auftrag, vorgaenger=vorgaenger).delete()
        if not geloescht:
            raise DienstFehler("Diese Abhängigkeit gibt es nicht.")
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Vorgänger {vorgaenger.nummer} entfernt",
            revision=str(auftrag.revision_nr),
        )


# --- Aufträge: Verschieben ------------------------------------------------------------------

ARCHIV_REAKTIVIEREN = "reaktivieren"
ARCHIV_LASSEN = "im_archiv"


class VerschiebungAbgelehnt(Konflikt):
    """Die Prüfung ließ die Verschiebung nicht zu; ``vorschau`` nennt die Gründe je Eintrag."""

    def __init__(self, text: str, vorschau: dict):
        super().__init__(text)
        self.vorschau = vorschau


def modulpfad(modul: Modul | None) -> str:
    """„Core / Bank“: das Modul samt seinen Elternmodulen; Kreise verhindert ``modul_aendern``."""
    teile: list[str] = []
    while modul is not None:
        teile.insert(0, modul.name)
        modul = modul.eltern
    return " / ".join(teile)


def verschiebeziele(akteur: Akteur) -> list[dict]:
    """Alle Projekte, auch archivierte, mit dem, was sich dort zuordnen lässt."""
    _planung_erforderlich(akteur, "Einträge verschieben")
    return [
        {
            "kennung": p.kennung,
            "kuerzel": p.kuerzel,
            "name": p.name,
            "archiviert": not p.aktiv,
            "module": sorted(
                (
                    {"kennung": m.kennung, "pfad": modulpfad(m)}
                    for m in p.module.filter(aktiv=True).select_related("eltern")
                ),
                key=lambda m: m["pfad"].lower(),
            ),
            "arbeitsorte": [
                {"kennung": o.kennung, "name": o.name, "bestaetigt": o.bestaetigt}
                for o in p.arbeitsorte.filter(aktiv=True).order_by("name")
            ],
            "meilensteine": [
                {"nummer": m.nummer, "name": m.name} for m in p.meilensteine.all().order_by("pk")
            ],
        }
        for p in Projekt.objects.order_by("-aktiv", "name")
    ]


def verschieben_gesperrt(auftrag: Auftrag) -> str:
    """Warum dieser Eintrag gerade nicht verschoben werden kann, mit dem nächsten Schritt."""
    if auftrag.zustand != Auftrag.IN_ARBEIT:
        return ""
    return (
        f"{auftrag.nummer} ist In Arbeit bei {auftrag.bearbeiter_name}; unter einem laufenden "
        "Bearbeiter wird nicht verschoben. Zuerst beenden oder übergeben: Der Bearbeiter reicht "
        "sein Ergebnis ein oder meldet ein Hindernis; ist er weg, „Weitere Aktionen“ → "
        "„Bearbeitung ist unklar“. Danach erneut verschieben."
    )


def auftrag_ereignisse(auftrag: Auftrag):
    """Verlauf eines Eintrags über alle Projekte, in denen er stand; was vor den Nummern je Projekt
    geschah, steht unter A-<id>. Der Verlauf ist nur anhängend und wird nie umgeschrieben."""
    orte = Q(projekt=auftrag.projekt, objekt_kennung__in=(auftrag.nummer, auftrag.alte_nummer))
    for frueher in auftrag.fruehere_nummern.select_related("projekt"):
        orte |= Q(projekt=frueher.projekt, objekt_kennung__in=(frueher.nummer, auftrag.alte_nummer))
    return Ereignis.objects.filter(orte, objekt_typ="Auftrag")


def _ort_text(projekt: Projekt, modul: Modul | None) -> str:
    return f"{projekt.name} › {modulpfad(modul)}" if modul else f"{projekt.name} › ohne Modul"


def _nach_kennung_im_ziel(modell, projekt: Projekt, kennung: str):
    return modell.objects.filter(projekt=projekt, kennung=kennung, aktiv=True).first()


def _meilenstein_im_ziel(projekt: Projekt, wert: str) -> Meilenstein | None:
    nummer = wert.strip().upper().removeprefix("M-")
    if not nummer.isdecimal():
        return None
    return Meilenstein.objects.filter(projekt=projekt, pk=int(nummer)).first()


def _zuordnungen_planen(roh: dict, a: Auftrag, ziel: Projekt, wechsel: bool, p: dict) -> None:
    """Modul, Arbeitsort und Meilenstein des Ziels aus den ausdrücklichen Angaben; nichts wird
    nach gleichem Namen erraten. Ohne Angabe bleibt es im selben Projekt, sonst wird es aufgehoben."""
    aenderungen, konflikte = p["eintrag"]["aenderungen"], p["eintrag"]["konflikte"]
    rev = a.aktuelle_revision
    # Modul
    modul = None if wechsel else a.modul
    if roh.get("modul") is not None:
        modul = None
        if str(roh["modul"]).strip():
            modul = _nach_kennung_im_ziel(Modul, ziel, str(roh["modul"]).strip())
            if modul is None:
                konflikte.append(f"Modul „{roh['modul']}“ gibt es in „{ziel.name}“ nicht (aktiv).")
    if not konflikte and (modul.pk if modul else None) != a.modul_id:
        alt, neu = modulpfad(a.modul) or "ohne Modul", modulpfad(modul) or "ohne Modul"
        aenderungen.append(f"Modul: {alt} → {neu}")
    # Arbeitsort: gehört zur Revision; Abgeschlossenes behält den Ort seiner Arbeit.
    ort = None if wechsel else rev.arbeitsort
    if a.ist_endzustand:
        ort = rev.arbeitsort
        if str(roh.get("arbeitsort") or "").strip():
            konflikte.append(
                f"{a.get_zustand_display()} behält den Arbeitsort der abgeschlossenen Arbeit; "
                "die Angabe weglassen."
            )
        if wechsel and ort is not None:
            aenderungen.append(
                f"Arbeitsort „{ort.name}“ bleibt als früheres Projekt der abgeschlossenen Arbeit "
                "stehen."
            )
    elif roh.get("arbeitsort") is not None:
        ort = None
        if str(roh["arbeitsort"]).strip():
            ort = _nach_kennung_im_ziel(Arbeitsort, ziel, str(roh["arbeitsort"]).strip())
            if ort is None:
                konflikte.append(
                    f"Arbeitsort „{roh['arbeitsort']}“ gibt es in „{ziel.name}“ nicht (aktiv)."
                )
    p["ort_aendert"] = not a.ist_endzustand and (ort.pk if ort else None) != rev.arbeitsort_id
    if p["ort_aendert"]:
        if ort is None:
            aenderungen.append(
                f"Arbeitsort „{rev.arbeitsort.name}“ aufgehoben; im Ziel später wählen."
            )
        else:
            vorschlag = "" if ort.bestaetigt else " (Vorschlag, bleibt unbestätigt)"
            aenderungen.append(f"Arbeitsort: „{ort.name}“{vorschlag}")
        aenderungen.append(f"Neue Revision {a.revision_nr + 1}; frühere bleiben unverändert.")
    # Meilenstein
    ms = None if wechsel else a.meilenstein
    if roh.get("meilenstein") is not None:
        ms = None
        if str(roh["meilenstein"]).strip():
            ms = _meilenstein_im_ziel(ziel, str(roh["meilenstein"]))
            if ms is None:
                konflikte.append(
                    f"Meilenstein „{roh['meilenstein']}“ gibt es in „{ziel.name}“ nicht."
                )
    if (ms.pk if ms else None) != a.meilenstein_id:
        if ms is None:
            aenderungen.append(
                f"Meilenstein {a.meilenstein.nummer} „{a.meilenstein.name}“ aufgehoben."
            )
        else:
            aenderungen.append(f"Meilenstein: {ms.nummer} „{ms.name}“")
    p.update(modul=modul, ort=ort, meilenstein=ms)


def _zustand_planen(a: Auftrag, wechsel: bool, p: dict) -> None:
    """Folgen für Zustand, Freigabe und Bearbeiter; kein Weg führt in eine neue Arbeitsfreigabe."""
    e = p["eintrag"]
    if a.zustand == Auftrag.IN_ARBEIT:
        e["konflikte"].append(verschieben_gesperrt(a))
    elif a.zustand == Auftrag.FREIGEGEBEN:
        # Nur die Planung zählt: Auf einen Vorgänger zu warten ist bei Freigegebenem normal.
        planung_gueltig = all(q.erfuellt for q in freigabepruefung(a)[:5])
        if wechsel or p["ort_aendert"] or not planung_gueltig:
            e["zustand_danach"] = Auftrag.ENTWURF
            e["aenderungen"].append("Freigabe erlischt; der Eintrag wird Entwurf.")
        else:
            e["aenderungen"].append("Freigabe bleibt bestehen.")
    elif a.zustand == Auftrag.BLOCKIERT:
        e["aenderungen"].append("Bleibt blockiert; Frage oder Hindernis bleibt offen.")
        if a.bearbeiter_name:
            e["aenderungen"].append(f"Bearbeiter {a.bearbeiter_name} bleibt.")
        bindung = a.bearbeiter_bindung
        if wechsel and bindung is not None and bindung.art != ChatBindung.ART_PC:
            e["konflikte"].append(
                f"Bearbeiter {a.bearbeiter_name} hat einen Projektschlüssel und erreicht das "
                "Ziel nicht. Zuerst „Weitere Aktionen“ → „Bearbeitung ist unklar“; die Frage "
                "bleibt im Verlauf. Danach erneut verschieben."
            )
        ort = p["ort"]
        if ort is None or not ort.bestaetigt:
            e["konflikte"].append(
                "Blockierte Arbeit läuft nach der Antwort weiter: Im Ziel einen bestätigten "
                "Arbeitsort wählen."
            )
    elif a.ist_endzustand:
        e["aenderungen"].append("Abschluss, Revision und Ergebnisse bleiben unverändert.")
    e["zustand_danach_text"] = dict(Auftrag.ZUSTAENDE)[e["zustand_danach"]]


def _ueberschneidungen_planen(punkte: dict[int, dict]) -> None:
    """Blockierte Arbeit läuft nach der Antwort ungeprüft weiter; darum zählt für sie der Stand nach
    dieser Verschiebung: Mitgewählte stehen dort, wo der Plan sie hinstellt, nicht in der Datenbank."""
    for pk, p in punkte.items():
        a, ort = p["auftrag"], p.get("ort")
        if (
            a.zustand != Auftrag.BLOCKIERT
            or ort is None
            or not p["eintrag"]["aendert"]
            or not (p["wechsel"] or p["ort_aendert"])
        ):
            continue
        rev = a.aktuelle_revision
        treffer = [
            t
            for t in _ueberschneidungen(a, rev, projekt=p["ziel"], ort_id=ort.pk)
            if t[0].pk not in punkte
        ]
        vorgaenger = set(a.abhaengigkeiten.values_list("vorgaenger_id", flat=True))
        for anderer_pk, q in punkte.items():
            if (
                anderer_pk == pk
                or anderer_pk in vorgaenger
                or q.get("ziel") != p["ziel"]
                or q.get("ort") != ort
                or q["eintrag"]["zustand_danach"] not in Auftrag.BELEGENDE_ZUSTAENDE
            ):
                continue
            treffer.extend(
                (q["auftrag"], m1, m2)
                for m1 in rev.muster
                for m2 in q["auftrag"].aktuelle_revision.muster
                if muster_ueberschneiden(m1, m2)
            )
        for anderer, m1, m2 in treffer:
            p["eintrag"]["konflikte"].append(
                f"Überschneidung im Ziel mit {anderer.nummer} "
                f"({anderer.get_zustand_display()}): „{m1}“ und „{m2}“. Nach der Antwort "
                "schrieben zwei Bearbeiter in dieselben Dateien; anderen Arbeitsort wählen."
            )


def _abhaengigkeiten_planen(punkte: dict[int, dict]) -> list[Abhaengigkeit]:
    """Abhängigkeiten gelten nur im selben Projekt: Gehen beide mit, bleiben sie, sonst werden sie
    sichtbar aufgehoben. Ein freigegebener Nachfolger würde dadurch übernehmbar: das sperrt."""
    aufheben: dict[int, Abhaengigkeit] = {}

    def endprojekt(auftrag: Auftrag) -> int:
        punkt = punkte.get(auftrag.pk)
        return punkt["ziel"].pk if punkt and punkt.get("ziel") else auftrag.projekt_id

    for pk, p in punkte.items():
        if not p.get("wechsel"):
            continue
        for d in Abhaengigkeit.objects.filter(
            Q(auftrag_id=pk) | Q(vorgaenger_id=pk)
        ).select_related("auftrag__projekt", "vorgaenger__projekt"):
            if endprojekt(d.auftrag) == endprojekt(d.vorgaenger):
                continue
            aufheben[d.pk] = d
            nach, vor = d.auftrag, d.vorgaenger
            e = p["eintrag"]
            if pk == nach.pk:
                e["aenderungen"].append(f"Abhängigkeit von Vorgänger {vor.nummer} aufgehoben.")
            else:
                e["aenderungen"].append(f"Abhängigkeit von Nachfolger {nach.nummer} aufgehoben.")
            # Bleibt die andere Seite trotz Auswahl stehen, gehört es auch in ihre Vorschau.
            punkt_anderer = punkte.get(nach.pk if pk == vor.pk else vor.pk)
            if punkt_anderer and punkt_anderer.get("ziel") and not punkt_anderer["wechsel"]:
                wer = vor.nummer if pk == vor.pk else nach.nummer
                punkt_anderer["eintrag"]["aenderungen"].append(
                    f"Abhängigkeit zu {wer} aufgehoben; {wer} wird verschoben."
                )
                punkt_anderer["eintrag"]["aendert"] = True
            punkt_nach = punkte.get(nach.pk)
            zustand_nach = punkt_nach["eintrag"]["zustand_danach"] if punkt_nach else nach.zustand
            # Übernehmbar macht nur ein erledigter Vorgänger (freigabepruefung), kein abgebrochener.
            if zustand_nach == Auftrag.FREIGEGEBEN and vor.zustand != Auftrag.ERLEDIGT:
                e["konflikte"].append(
                    f"{nach.nummer} ist freigegeben und wartet auf {vor.nummer}; ohne die "
                    f"Abhängigkeit würde er übernehmbar. Zuerst die Freigabe von {nach.nummer} "
                    "zurücknehmen oder ihn mitverschieben."
                )
    return list(aufheben.values())


def _verschiebeplan(eintraege, archiv) -> tuple[dict, dict[int, dict], list[Abhaengigkeit]]:
    """Die eine Prüfung für Vorschau und Ausführung. Ändert nichts."""
    archiv = archiv if isinstance(archiv, dict) else {}
    punkte: dict[int, dict] = {}
    zeilen: list[dict] = []
    allgemein: list[str] = []
    for roh in eintraege if isinstance(eintraege, list) else []:
        e = {"nummer": "", "aendert": False, "aenderungen": [], "konflikte": []}
        zeilen.append(e)
        if not isinstance(roh, dict):
            e["konflikte"].append("Eintrag ist kein Objekt mit auftrag, revision und projekt.")
            continue
        e["nummer"] = str(roh.get("auftrag", ""))
        try:
            a = auftrag_holen_alle(auftrag_id_aus_nummer(roh.get("auftrag", "")))
        except DienstFehler as fehler:
            e["konflikte"].append(f"{e['nummer']}: {fehler}")
            continue
        if a.pk in punkte:
            e["konflikte"].append(f"{a.nummer} ist doppelt ausgewählt.")
            continue
        e.update(
            id=a.pk,
            nummer=a.nummer,
            alte_nummer=a.alte_nummer,
            titel=a.titel,
            art=a.typ,
            zustand=a.zustand,
            zustand_text=a.get_zustand_display(),
            zustand_danach=a.zustand,
            zustand_danach_text=a.get_zustand_display(),
            revision=a.revision_nr,
            bearbeiter=a.bearbeiter_name or None,
            von={
                "projekt": a.projekt.kennung,
                "modul": a.modul.kennung if a.modul else None,
                "archiviert": not a.projekt.aktiv,
                "text": _ort_text(a.projekt, a.modul),
            },
            nach=None,
        )
        p = {"auftrag": a, "eintrag": e}
        punkte[a.pk] = p
        try:
            if int(roh.get("revision", "")) != a.revision_nr:
                e["konflikte"].append(
                    f"Revision {roh['revision']} ist veraltet; aktuell ist {a.revision_nr}."
                )
        except (TypeError, ValueError):
            e["konflikte"].append("Die Revision aus dem letzten Lesen fehlt.")
        kennung = str(roh.get("projekt") or "").strip()
        ziel = Projekt.objects.filter(kennung=kennung).first()
        if ziel is None:
            e["konflikte"].append(
                f"Zielprojekt „{kennung}“ gibt es nicht." if kennung else "Das Ziel fehlt noch."
            )
            continue
        wechsel = ziel.pk != a.projekt_id
        p.update(ziel=ziel, wechsel=wechsel)
        _zuordnungen_planen(roh, a, ziel, wechsel, p)
        modul = p["modul"]
        e["nach"] = {
            "projekt": ziel.kennung,
            "modul": modul.kennung if modul else None,
            "archiviert": not ziel.aktiv,
            "text": _ort_text(ziel, modul),
        }
        e["aendert"] = bool(wechsel or e["aenderungen"])
        if not e["aendert"]:
            continue
        if (wechsel or p["modul"] != a.modul or p["ort_aendert"]) and _werkbank_aktiv(a):
            e["konflikte"].append(
                f"{a.nummer} läuft auf einer KI-Werkbank und kann nicht verschoben werden."
            )
        _zustand_planen(a, wechsel, p)
        if wechsel:
            e["aenderungen"].insert(
                0, f"Neue Nummer in „{ziel.name}“; {a.nummer} führt weiter hierher."
            )
    aufheben = _abhaengigkeiten_planen(punkte)
    _ueberschneidungen_planen(punkte)
    archivziele: list[dict] = []
    folgen: list[str] = []
    for p in punkte.values():
        ziel = p.get("ziel")
        if ziel is None or ziel.aktiv or not p.get("wechsel"):
            continue
        if any(z["kennung"] == ziel.kennung for z in archivziele):
            continue
        wahl = str(archiv.get(ziel.kennung) or "")
        archivziele.append({"kennung": ziel.kennung, "name": ziel.name, "wahl": wahl})
        if wahl not in (ARCHIV_REAKTIVIEREN, ARCHIV_LASSEN):
            allgemein.append(
                f"„{ziel.name}“ ist archiviert: Reaktivieren oder ausdrücklich im Archiv lassen; "
                "sonst verschwände der Eintrag unsichtbar."
            )
        if wahl != ARCHIV_LASSEN:
            for w in _alte_freigaben(ziel, punkte):
                folgen.append(
                    f"{w.nummer} „{w.titel}“ in „{ziel.name}“: Freigabe erlischt beim "
                    "Reaktivieren, der Eintrag wird Entwurf."
                )
                if w.pk in punkte:
                    e = punkte[w.pk]["eintrag"]
                    e["aenderungen"] = [t for t in e["aenderungen"] if "Freigabe bleibt" not in t]
                    e["aenderungen"].append("Freigabe erlischt beim Reaktivieren des Projekts.")
                    e.update(
                        aendert=True,
                        zustand_danach=Auftrag.ENTWURF,
                        zustand_danach_text=dict(Auftrag.ZUSTAENDE)[Auftrag.ENTWURF],
                    )
    for p in punkte.values():
        ziel = p.get("ziel")
        if (
            ziel is not None
            and not ziel.aktiv
            and p.get("wechsel")
            and p["auftrag"].zustand == Auftrag.BLOCKIERT
            and archiv.get(ziel.kennung) == ARCHIV_LASSEN
        ):
            p["eintrag"]["konflikte"].append(
                "Blockierte Arbeit läuft nach der Antwort weiter und gehört nicht ins Archiv: "
                f"„{ziel.name}“ reaktivieren oder ein anderes Ziel wählen."
            )
    if not zeilen:
        allgemein.append("Nichts ausgewählt.")
    elif not any(e["aendert"] for e in zeilen) and not any(e["konflikte"] for e in zeilen):
        allgemein.append("Alle ausgewählten Einträge stehen schon am Ziel.")
    konflikte = [*allgemein, *(k for e in zeilen for k in e["konflikte"])]
    plan = {
        "eintraege": zeilen,
        "archivziele": archivziele,
        "reaktivierung_folgen": folgen,
        "konflikte": konflikte,
        "ausfuehrbar": not konflikte,
    }
    plan["pruefcode"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return plan, punkte, aufheben


def _alte_freigaben(ziel: Projekt, punkte: dict[int, dict]) -> list[Auftrag]:
    """Freigegebenes im archivierten Ziel: Reaktivieren machte es sofort übernehmbar, obwohl dabei
    keine alte Arbeit anlaufen soll. Was das Ziel gerade verlässt, regelt seine eigene Zeile."""
    gehen = [pk for pk, p in punkte.items() if p.get("wechsel")]
    return list(
        ziel.auftraege.filter(zustand=Auftrag.FREIGEGEBEN)
        .exclude(pk__in=gehen)
        .select_related("projekt")
        .order_by("id")
    )


def auftrag_holen_alle(auftrag_id: int) -> Auftrag:
    """Für Owner und Assistent, die alle Projekte sehen; die Befugnis ist vorher geprüft."""
    auftrag = (
        Auftrag.objects.select_related(
            "projekt", "modul__eltern", "meilenstein", "bearbeiter_bindung"
        )
        .filter(pk=auftrag_id)
        .first()
    )
    if auftrag is None:
        raise KeinZugriff("Nicht gefunden.")
    return auftrag


def verschiebung_pruefen(akteur: Akteur, eintraege, *, archiv=None) -> dict:
    """Vorschau: was mit jedem Eintrag geschähe und was dagegen spricht. Ändert nichts. Der
    ``pruefcode`` bindet die Ausführung an genau diese Vorschau."""
    _planung_erforderlich(akteur, "Einträge verschieben")
    return _verschiebeplan(eintraege, archiv)[0]


def verschiebung_ausfuehren(akteur: Akteur, eintraege, *, archiv=None, pruefcode: str = "") -> dict:
    """Alle oder keiner: Die Prüfung läuft in derselben Transaktion erneut; hat sich seit der
    Vorschau etwas geändert, geschieht nichts."""
    _planung_erforderlich(akteur, "Einträge verschieben")
    with transaction.atomic():
        plan, punkte, aufheben = _verschiebeplan(eintraege, archiv)
        if not plan["ausfuehrbar"]:
            raise VerschiebungAbgelehnt("Nichts verschoben: " + " ".join(plan["konflikte"]), plan)
        if pruefcode != plan["pruefcode"]:
            raise VerschiebungAbgelehnt(
                "Nichts verschoben: Angaben oder Lage haben sich seit der Vorschau geändert; "
                "bitte neu prüfen.",
                plan,
            )
        reaktiviert = []
        for z in plan["archivziele"]:
            if z["wahl"] == ARCHIV_REAKTIVIEREN:
                ziel = Projekt.objects.get(kennung=z["kennung"])
                for w in _alte_freigaben(ziel, punkte):
                    Auftrag.objects.filter(pk=w.pk).update(
                        zustand=Auftrag.ENTWURF, freigegeben_am=None, aktualisiert_am=timezone.now()
                    )
                    _ereignis(
                        akteur,
                        projekt=ziel,
                        objekt_typ="Auftrag",
                        objekt_kennung=w.nummer,
                        ereignis="Freigabe erloschen",
                        anlass="Projekt reaktiviert; alte Arbeit läuft nicht von selbst wieder an.",
                        revision=str(w.revision_nr),
                    )
                projekt_archivieren(akteur, ziel, aktiv=True)
                reaktiviert.append(z["kennung"])
        for d in aufheben:
            d.delete()
        verschoben = [
            _verschieben_anwenden(akteur, p) for p in punkte.values() if p["eintrag"]["aendert"]
        ]
        for d in aufheben:
            for bleibt, weg in ((d.auftrag, d.vorgaenger), (d.vorgaenger, d.auftrag)):
                if bleibt.pk not in punkte:
                    _ereignis(
                        akteur,
                        projekt=bleibt.projekt,
                        objekt_typ="Auftrag",
                        objekt_kennung=bleibt.nummer,
                        ereignis=f"Abhängigkeit zu {weg.nummer} aufgehoben",
                        anlass=f"{weg.nummer} wurde verschoben. Grund der Abhängigkeit: {d.grund}"
                        if d.grund
                        else f"{weg.nummer} wurde verschoben.",
                        revision=str(bleibt.revision_nr),
                    )
    return {
        "verschoben": verschoben,
        "unveraendert": [
            p["eintrag"]["nummer"] for p in punkte.values() if not p["eintrag"]["aendert"]
        ],
        "reaktiviert": reaktiviert,
    }


def _verschieben_anwenden(akteur: Akteur, p: dict) -> dict:
    """Ein geprüfter Eintrag. ``update`` statt ``save``: „zuletzt erledigt“ bleibt, wie es war."""
    a, e, ziel = p["auftrag"], p["eintrag"], p["ziel"]
    quelle, alt = a.projekt, a.nummer
    felder: dict = {"modul": p["modul"], "meilenstein": p["meilenstein"]}
    neu = alt
    if p["wechsel"]:
        if a.projekt_nummer is not None:
            FruehereNummer.objects.create(
                auftrag=a, projekt=quelle, projekt_nummer=a.projekt_nummer
            )
        # Die Transaktion hält die Schreibsperre (IMMEDIATE): Zwei Umzüge bekommen nie dieselbe Zahl.
        Projekt.objects.filter(pk=ziel.pk).update(naechste_nummer=F("naechste_nummer") + 1)
        nummer = Projekt.objects.values_list("naechste_nummer", flat=True).get(pk=ziel.pk) - 1
        felder.update(projekt=ziel, projekt_nummer=nummer)
        neu = f"{ziel.kuerzel}-{nummer}"
    revision = a.revision_nr
    if p["ort_aendert"]:
        rev = a.aktuelle_revision
        revision += 1
        Revision.objects.create(
            auftrag=a,
            nummer=revision,
            beschreibung=rev.beschreibung,
            ziel=rev.ziel,
            nicht_ziele=rev.nicht_ziele,
            arbeitsort=p["ort"],
            schreibumfang=rev.schreibumfang,
            quellen=rev.quellen,
            abnahmekriterien=rev.abnahmekriterien,
            erstellt_von_name=akteur.name,
            anlass=f"Verschoben: {e['von']['text']} → {e['nach']['text']}"[:200],
        )
        felder.update(revision_nr=revision, freigegeben_am=None)
    if e["zustand_danach"] != a.zustand:
        felder.update(
            zustand=e["zustand_danach"], freigegeben_am=None, aktualisiert_am=timezone.now()
        )
    Auftrag.objects.filter(pk=a.pk).update(**felder)
    anlass = f"{e['von']['text']} → {e['nach']['text']}; " + " ".join(e["aenderungen"])
    if p["wechsel"]:
        anlass = f"{alt} → {neu}; {anlass}"
        _ereignis(
            akteur,
            projekt=quelle,
            objekt_typ="Auftrag",
            objekt_kennung=alt,
            ereignis=f"Verschoben nach {neu} ({ziel.name})"[:120],
            anlass=anlass,
            revision=str(revision),
        )
        _ereignis(
            akteur,
            projekt=ziel,
            objekt_typ="Auftrag",
            objekt_kennung=neu,
            ereignis=f"Hierher verschoben von {alt} ({quelle.name})"[:120],
            anlass=anlass,
            revision=str(revision),
        )
    else:
        _ereignis(
            akteur,
            projekt=quelle,
            objekt_typ="Auftrag",
            objekt_kennung=alt,
            ereignis="Zuordnung im Projekt geändert",
            anlass=anlass,
            revision=str(revision),
        )
    return {
        "id": a.pk,
        "alt": alt,
        "neu": neu,
        "alte_nummer": a.alte_nummer,
        "titel": a.titel,
        "art": a.typ,
        "zustand": e["zustand_danach"],
        "zustand_text": e["zustand_danach_text"],
        "revision": revision,
        "von": e["von"],
        "nach": e["nach"],
        "aenderungen": e["aenderungen"],
        "adresse": reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}),
    }


# --- Aufträge: Zustandswechsel durch Menschen ----------------------------------------------


IM_CHAT_FREIGEGEBEN = "freigegeben auf Freds Anweisung im Chat"


def auftrag_freigeben(akteur: Akteur, auftrag: Auftrag) -> None:
    """Freigabe bindet die aktuelle Revision; nur aus dem Entwurf. Der Owner gibt in der
    Oberfläche frei, sein Assistent auf Freds Anweisung im Chat (mit Vermerk)."""
    _projekt_sichtbar(akteur, auftrag.projekt)  # Fremdes gilt als nicht vorhanden
    _planung_erforderlich(akteur, "Aufträge freigeben")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        if auftrag.zustand != Auftrag.ENTWURF:
            raise Konflikt(
                f"Freigabe ist aus dem Zustand „{auftrag.get_zustand_display()}“ nicht möglich."
            )
        offen = [p for p in freigabepruefung(auftrag) if not p.erfuellt]
        if offen:
            raise Konflikt("Freigabe nicht möglich: " + "; ".join(p.text for p in offen))
        auftrag.zustand = Auftrag.FREIGEGEBEN
        auftrag.freigegeben_am = timezone.now()
        auftrag.save(update_fields=["zustand", "freigegeben_am", "aktualisiert_am"])
        if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
            from . import werkbank

            werkbank.kontext_binden(auftrag)  # die Freigabe bindet auch die Kontextbasis
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Freigegeben",
            anlass=IM_CHAT_FREIGEGEBEN if akteur.bindung is not None else "",
            revision=str(auftrag.revision_nr),
        )


def freigabe_zuruecknehmen(akteur: Akteur, auftrag: Auftrag, grund: str = "") -> None:
    _owner_erforderlich(akteur, "Freigaben zurücknehmen")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.zustand != Auftrag.FREIGEGEBEN:
            raise Konflikt(
                "Nur ein freigegebener, noch nicht übernommener Auftrag kann zurückgenommen werden."
            )
        auftrag.zustand = Auftrag.ENTWURF
        auftrag.freigegeben_am = None
        auftrag.save(update_fields=["zustand", "freigegeben_am", "aktualisiert_am"])
        if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
            from . import werkbank

            werkbank.freigabe_aufgehoben(auftrag)
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Freigabe zurückgenommen",
            anlass=grund,
            revision=str(auftrag.revision_nr),
        )


@_ohne_aktive_werkbank("Abbrechen")
def auftrag_abbrechen(akteur: Akteur, auftrag: Auftrag, grund: str) -> None:
    _owner_erforderlich(akteur, "Aufträge abbrechen")
    if not grund.strip():
        raise DienstFehler("Ein Abbruch braucht eine Begründung.")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.ist_endzustand:
            raise Konflikt("Der Auftrag ist bereits abgeschlossen.")
        _bearbeiter_abloesen(auftrag)
        auftrag.zustand = Auftrag.ABGEBROCHEN
        auftrag.save()
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Abgebrochen",
            anlass=grund.strip(),
            revision=str(auftrag.revision_nr),
        )


@_ohne_aktive_werkbank("Entkoppeln")
def bearbeitung_unklar(akteur: Akteur, auftrag: Auftrag, grund: str) -> None:
    """Bearbeiter verschwunden oder Widerspruch zu Projektdateien: Übernahme erlischt, der
    Auftrag geht zurück in den Entwurf und braucht eine neue Freigabe."""
    _owner_erforderlich(akteur, "Bearbeitung als unklar markieren")
    if not grund.strip():
        raise DienstFehler("Bitte angeben, was unklar ist.")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.zustand not in Auftrag.BEARBEITER_ZUSTAENDE:
            raise Konflikt("Nur ein Auftrag mit Bearbeiter kann als unklar markiert werden.")
        _bearbeiter_abloesen(auftrag)
        auftrag.zustand = Auftrag.ENTWURF
        auftrag.freigegeben_am = None
        auftrag.save()
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Übernahme beendet, zurück in Entwurf",
            anlass=grund.strip(),
            revision=str(auftrag.revision_nr),
        )


# --- Aufträge: Bearbeiter (Chat oder Mensch) -----------------------------------------------


def auftrag_uebernehmen(akteur: Akteur, auftrag: Auftrag, revision_nr: int) -> Auftrag:
    """Exklusive Übernahme in einer Transaktion. Genau eine konkurrierende Anfrage gewinnt."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    if akteur.bindung is None and not akteur.ist_owner:
        raise KeinZugriff("Übernehmen dürfen Chat-Bindungen oder der Owner.")
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        if not aktuell.projekt.aktiv:
            raise Konflikt(f"{aktuell.nummer} gehört zu einem archivierten Projekt.")
        if aktuell.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
            raise Konflikt(
                f"{aktuell.nummer} ist einer KI-Werkbank zugewiesen und kann nicht von einem "
                "Chat übernommen werden. Der Owner hebt die Zuweisung auf, wenn ein Chat ran soll."
            )
        if aktuell.zustand != Auftrag.FREIGEGEBEN or aktuell.hat_bearbeiter:
            if aktuell.hat_bearbeiter:
                raise Konflikt(
                    f"{aktuell.nummer} wurde bereits von {aktuell.bearbeiter_name} übernommen."
                )
            raise Konflikt(
                f"{aktuell.nummer} ist nicht übernehmbar (Zustand {aktuell.get_zustand_display()})."
            )
        if aktuell.revision_nr != revision_nr:
            raise Konflikt(
                f"Revision {revision_nr} ist veraltet; aktuell ist Revision {aktuell.revision_nr}."
            )
        offen = [p for p in freigabepruefung(aktuell) if not p.erfuellt]
        if offen:
            raise Konflikt("Übernahme nicht möglich: " + "; ".join(p.text for p in offen))
        if akteur.bindung is not None and (
            akteur.projekt is None or akteur.projekt.pk != aktuell.projekt_id
        ):
            raise KeinZugriff("Nicht gefunden.")
        geaendert = Auftrag.objects.filter(
            pk=aktuell.pk,
            zustand=Auftrag.FREIGEGEBEN,
            bearbeiter_bindung=None,
            bearbeiter_benutzer=None,
            revision_nr=revision_nr,
        ).update(
            zustand=Auftrag.IN_ARBEIT,
            bearbeiter_bindung=akteur.bindung,
            bearbeiter_benutzer=akteur.benutzer,
            bearbeiter_name=akteur.name,
            uebernommen_am=timezone.now(),
            uebernommene_revision=revision_nr,
            aktualisiert_am=timezone.now(),
        )
        if geaendert != 1:
            raise Konflikt(f"{aktuell.nummer} wurde gerade von jemand anderem übernommen.")
        _ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis="Übernommen",
            revision=str(revision_nr),
        )
    auftrag.refresh_from_db()
    return auftrag


def _bearbeiter_pruefen(akteur: Akteur, auftrag: Auftrag, revision_nr: int) -> str | None:
    """Gibt einen Grund zurück, warum die Meldung historisch ist, sonst None.

    Wirft ``KeinZugriff``, wenn der Akteur den Auftrag nie bearbeitet hat.
    """
    _projekt_sichtbar(akteur, auftrag.projekt)
    if akteur.ist_bearbeiter_von(auftrag):
        if revision_nr != auftrag.revision_nr:
            return f"bezieht sich auf Revision {revision_nr}, aktuell ist {auftrag.revision_nr}"
        return None
    war_bearbeiter = (
        akteur.bindung is not None and auftrag.letzter_bearbeiter_bindung_id == akteur.bindung.pk
    ) or (
        akteur.benutzer is not None and auftrag.letzter_bearbeiter_benutzer_id == akteur.benutzer.pk
    )
    if war_bearbeiter:
        return f"Übernahme erloschen; Auftrag ist {auftrag.get_zustand_display()}"
    raise KeinZugriff(f"{auftrag.nummer} wird nicht von {akteur.name} bearbeitet.")


def _meldung(
    akteur: Akteur,
    auftrag: Auftrag,
    revision_nr: int,
    art: str,
    text: str,
    ereignis: str,
    *,
    neuer_zustand: str | None = None,
) -> Meldung:
    if not text.strip():
        raise DienstFehler("Der Text darf nicht leer sein.")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        grund = _bearbeiter_pruefen(akteur, auftrag, revision_nr)
        gewechselt = False
        if grund is None and neuer_zustand is not None and auftrag.zustand == Auftrag.IN_ARBEIT:
            auftrag.zustand = neuer_zustand
            auftrag.save(update_fields=["zustand", "aktualisiert_am"])
            gewechselt = True
        meldung = Meldung.objects.create(
            auftrag=auftrag,
            revision_nr=revision_nr,
            art=art,
            text=text.strip(),
            akteur_name=akteur.name,
            bindung=akteur.bindung,
            historisch=grund is not None,
        )
        if grund is None:
            Auftrag.objects.filter(pk=auftrag.pk).update(zuletzt_gemeldet_am=timezone.now())
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=ereignis
            + (" (historisch)" if grund else "")
            + (f", Auftrag {auftrag.get_zustand_display()}" if gewechselt else ""),
            anlass=(grund + ": " if grund else "") + text.strip()[:200],
            revision=str(revision_nr),
        )
    if grund is not None:
        raise Konflikt(f"Als historisch abgelegt: {grund}.")
    return meldung


def fortschritt_melden(akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str) -> Meldung:
    return _meldung(akteur, auftrag, revision_nr, Meldung.FORTSCHRITT, text, "Fortschritt gemeldet")


def rueckfrage_melden(akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str) -> Meldung:
    """Rückfrage des Bearbeiters; blockiert den Auftrag, bis ein Mensch antwortet."""
    meldung = _meldung(
        akteur,
        auftrag,
        revision_nr,
        Meldung.RUECKFRAGE,
        text,
        "Rückfrage gestellt",
        neuer_zustand=Auftrag.BLOCKIERT,
    )
    auftrag.refresh_from_db()
    return meldung


def fehler_melden(akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str) -> Meldung:
    """Fehler des Bearbeiters (Abbruch, Werkzeugproblem); blockiert den Auftrag."""
    meldung = _meldung(
        akteur,
        auftrag,
        revision_nr,
        Meldung.FEHLER,
        text,
        "Fehler gemeldet",
        neuer_zustand=Auftrag.BLOCKIERT,
    )
    auftrag.refresh_from_db()
    return meldung


@_ohne_aktive_werkbank("Antworten")
def rueckfrage_beantworten(
    akteur: Akteur, auftrag: Auftrag, text: str, *, lieferketten_bestaetigt: bool = False
) -> Meldung:
    """Antwort des Menschen auf Rückfrage, Hindernis oder Fehler; hebt die Blockade auf. Wartet
    der Auftrag auf eine technische Lieferung, verdrängt die Antwort diesen Lieferbedarf; das
    braucht ``lieferketten_bestaetigt``."""
    from . import lieferungen

    _planung_erforderlich(akteur, "Rückfragen beantworten")
    if not text.strip():
        raise DienstFehler("Die Antwort darf nicht leer sein.")
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        ketten = lieferungen.betroffene_ketten(aktuell, als_anbieter=False)
        if ketten and lieferketten_bestaetigt is not True:
            raise Konflikt(lieferungen.warnung_antworten(ketten))
        meldung = Meldung.objects.create(
            auftrag=aktuell,
            revision_nr=aktuell.revision_nr,
            art=Meldung.ANTWORT,
            text=text.strip(),
            akteur_name=akteur.name,
        )
        aufgehoben = False
        if aktuell.zustand == Auftrag.BLOCKIERT:
            aktuell.zustand = Auftrag.IN_ARBEIT
            aktuell.save(update_fields=["zustand", "aktualisiert_am"])
            aufgehoben = True
        _ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis="Antwort gegeben" + (", Blockade aufgehoben" if aufgehoben else ""),
            anlass=(
                "Technischer Lieferbedarf entfällt: "
                + ", ".join(f"{k['anbieter']}/{k['lieferkennung']}" for k in ketten)
                + " · "
                if ketten
                else ""
            )
            + text.strip()[:200],
            revision=str(aktuell.revision_nr),
        )
    auftrag.refresh_from_db()
    return meldung


def hindernis_melden(akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str) -> Meldung:
    """Hindernis melden; Wechsel nach BLOCKIERT im selben Commit wie das Ereignis."""
    meldung = _meldung(
        akteur,
        auftrag,
        revision_nr,
        Meldung.HINDERNIS,
        text,
        "Hindernis gemeldet",
        neuer_zustand=Auftrag.BLOCKIERT,
    )
    auftrag.refresh_from_db()
    return meldung


@_ohne_aktive_werkbank("Abschließen")
def ergebnis_einreichen(
    akteur: Akteur,
    auftrag: Auftrag,
    revision_nr: int,
    *,
    zusammenfassung: str,
    dateien: str = "",
    pruefungen: str = "",
    fehlgeschlagen: str = "",
    luecken: str = "",
) -> Ergebnis:
    """Ergebnis einreichen; die technischen Prüfungen schließen ab, es gibt keine
    Nutzerabnahme. Alle bestanden → Erledigt; eine fehlgeschlagen → Blockiert mit dem Grund als
    Fehlermeldung, der Bearbeiter bleibt; keine Prüfungen → Erledigt „ohne automatische Tests“."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        if not akteur.ist_bearbeiter_von(aktuell):
            raise KeinZugriff(f"{aktuell.nummer} wird nicht von {akteur.name} bearbeitet.")
        if aktuell.revision_nr != revision_nr:
            raise Konflikt(
                f"Revision {revision_nr} ist veraltet; aktuell ist Revision {aktuell.revision_nr}."
            )
        if aktuell.zustand not in Auftrag.BEARBEITER_ZUSTAENDE:
            raise Konflikt(f"Einreichen ist aus „{aktuell.get_zustand_display()}“ nicht möglich.")
        if not zusammenfassung.strip():
            raise DienstFehler("Die Zusammenfassung darf nicht leer sein.")
        if not zeilen(dateien):
            raise DienstFehler("Mindestens eine geänderte Datei mit Revision ist anzugeben.")
        ergebnis = Ergebnis.objects.create(
            auftrag=aktuell,
            revision_nr=revision_nr,
            zusammenfassung=zusammenfassung.strip(),
            dateien="\n".join(zeilen(dateien)),
            pruefschritte="\n".join(zeilen(pruefungen)),
            fehlgeschlagen="\n".join(zeilen(fehlgeschlagen)),
            luecken=luecken.strip(),
            eingereicht_von_name=akteur.name,
            bindung=akteur.bindung,
            benutzer=akteur.benutzer,
        )
        jetzt = timezone.now()
        aktuell.zuletzt_gemeldet_am = jetzt
        if ergebnis.fehlgeschlagenliste:
            aktuell.zustand = Auftrag.BLOCKIERT
            grund = "Prüfung fehlgeschlagen: " + "; ".join(ergebnis.fehlgeschlagenliste)
            Meldung.objects.create(
                auftrag=aktuell,
                revision_nr=revision_nr,
                art=Meldung.FEHLER,
                text=grund,
                akteur_name=akteur.name,
                bindung=akteur.bindung,
            )
            ereignis = "Ergebnis eingereicht, Prüfung fehlgeschlagen, Auftrag Blockiert"
        else:
            aktuell.zustand = Auftrag.ERLEDIGT
            aktuell.abgenommene_revision = revision_nr
            _bearbeiter_abloesen(aktuell)
            ereignis = "Ergebnis eingereicht, erledigt" + (
                " ohne automatische Tests" if ergebnis.ohne_tests else ""
            )
        aktuell.save()
        _ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis=ereignis,
            anlass=zusammenfassung.strip()[:200],
            revision=str(revision_nr),
        )
    auftrag.refresh_from_db()
    return ergebnis


# --- Was als Nächstes dran ist --------------------------------------------------------------

PRIO_RANG = {"hoch": 0, "mittel": 1, "niedrig": 2}
STILL_STUNDEN = 4


def sitzungsende_melden(akteur: Akteur, auftrag: Auftrag) -> bool:
    """Vermerk des Hooks beim Schließen eines Chats, der den Auftrag noch hält. Er ist kein Stand:
    Er ändert weder Zustand noch ``zuletzt_gemeldet_am``, sonst verdeckte er die letzte echte
    Meldung. Falsch zurück, wenn nichts zu vermerken ist; das ist kein Fehler."""
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        _projekt_sichtbar(akteur, aktuell.projekt)
        # Auch blockiert: Antwortet Fred später, soll er sofort sehen, dass der Chat weg ist.
        if aktuell.zustand not in Auftrag.BEARBEITER_ZUSTAENDE or not akteur.ist_bearbeiter_von(
            aktuell
        ):
            return False
        neueste = aktuell.meldungen.filter(historisch=False).first()
        if (
            neueste is not None
            and neueste.art == Meldung.SITZUNGSENDE
            and (not aktuell.uebernommen_am or neueste.zeitpunkt >= aktuell.uebernommen_am)
        ):
            return False  # schon vermerkt; ein Vermerk von vor einer neuen Übernahme zählt nicht
        Meldung.objects.create(
            auftrag=aktuell,
            revision_nr=aktuell.revision_nr,
            art=Meldung.SITZUNGSENDE,
            text="Chat beendet, Ergebnis fehlt.",
            akteur_name=akteur.name,
            bindung=akteur.bindung,
        )
        _ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis="Chat beendet, Ergebnis fehlt",
            revision=str(aktuell.revision_nr),
        )
    return True


def _nach_rang(auftraege) -> list[Auftrag]:
    """Priorität, dann die älteste Nummer; keine Sortierung von Hand."""
    return sorted(auftraege, key=lambda a: (PRIO_RANG[a.prioritaet], a.pk))


def _mit_offenem_vorgaenger(projekt: Projekt):
    """Abgebrochene Vorgänger halten nichts auf: Sie werden nie mehr erledigt."""
    return Abhaengigkeit.objects.filter(auftrag__projekt=projekt).exclude(
        vorgaenger__zustand__in=Auftrag.ENDZUSTAENDE
    )


def _eintraege_im_modul(projekt: Projekt, modul: Modul | None):
    """Einträge des Projekts; mit ``modul`` nur die dieses Moduls samt Untermodulen."""
    if modul is None:
        return projekt.auftraege.all()
    return projekt.auftraege.filter(modul__in=modul_teilbaum(modul))


def weiter_reihenfolge(projekt: Projekt, modul: Modul | None = None) -> list[Auftrag]:
    """Die einzige Stelle mit der Regel für „weiter“: freigegebene Aufträge und Issues ohne
    Bearbeiter und ohne offenen Vorgänger, nach Priorität, dann die älteste Nummer."""
    return _nach_rang(
        _eintraege_im_modul(projekt, modul)
        .filter(
            zustand=Auftrag.FREIGEGEBEN,
            bearbeiter_bindung=None,
            bearbeiter_benutzer=None,
            ausfuehrungsweg=Auftrag.WEG_CHAT,  # Werkbank-Aufträge gehören keinem freien Chat
        )
        .exclude(pk__in=_mit_offenem_vorgaenger(projekt).values("auftrag_id"))
    )


def offene_eintraege(projekt: Projekt, modul: Modul | None = None) -> list[Auftrag]:
    """Alles nicht Abgeschlossene: übernehmbar in „weiter“-Reihenfolge, dann Freigegebenes, das
    auf einen Vorgänger wartet, dann In Arbeit und Blockiert, dann Entwürfe."""
    weiter = weiter_reihenfolge(projekt, modul)
    uebernehmbar = {a.pk for a in weiter}
    offen = list(_eintraege_im_modul(projekt, modul).exclude(zustand__in=Auftrag.ENDZUSTAENDE))
    wartend = [a for a in offen if a.zustand == Auftrag.FREIGEGEBEN and a.pk not in uebernehmbar]
    laufend = [a for a in offen if a.zustand in Auftrag.BEARBEITER_ZUSTAENDE]
    entwuerfe = [a for a in offen if a.zustand == Auftrag.ENTWURF]
    return [*weiter, *_nach_rang(wartend), *_nach_rang(laufend), *_nach_rang(entwuerfe)]


# --- Wartet auf dich ------------------------------------------------------------------------


def ist_faellig(auftrag: Auftrag) -> bool:
    """Offen, und der Termin ist heute oder vorbei (Ortszeit)."""
    return (
        auftrag.faellig is not None
        and not auftrag.ist_endzustand
        and auftrag.faellig <= timezone.localdate()
    )


def verstummt(auftrag: Auftrag) -> dict | None:
    """Auftrag in Arbeit, dessen Chat beendet ist oder seit ``STILL_STUNDEN`` nichts gemeldet hat.
    Schweigen beweist nicht, dass der Chat weg ist; darum wird nur angezeigt, nichts geändert.
    ``stand`` ist die letzte Meldung des Bearbeiters, ohne Antworten und ohne den Ende-Vermerk."""
    if auftrag.zustand != Auftrag.IN_ARBEIT or auftrag.bearbeiter_bindung_id is None:
        return None  # Bearbeitet Fred selbst, gibt es keinen Chat, der schweigen könnte.
    if auftrag.ausfuehrungsweg == Auftrag.WEG_WERKBANK:
        return None  # Schweigen führt bei der Werkbank nie zur Neuvergabe (Erreichbarkeitswarnung)
    zeiten = [auftrag.zuletzt_gemeldet_am, auftrag.uebernommen_am]
    zuletzt = max((z for z in zeiten if z), default=auftrag.aktualisiert_am)
    ende = auftrag.meldungen.filter(art=Meldung.SITZUNGSENDE, historisch=False).first()
    beendet = ende is not None and ende.zeitpunkt >= zuletzt
    if not beendet:
        # Auch Freds Antwort startet die Frist neu: Die Wartezeit davor lag bei ihm, nicht beim
        # Chat. Ist der Chat schon beendet, ändert seine Antwort daran nichts.
        antwort = auftrag.meldungen.filter(art=Meldung.ANTWORT).first()
        if antwort is not None:
            zuletzt = max(zuletzt, antwort.zeitpunkt)
        if timezone.now() - zuletzt < timedelta(hours=STILL_STUNDEN):
            return None
    stand = (
        auftrag.meldungen.filter(historisch=False)
        .exclude(art__in=(Meldung.ANTWORT, Meldung.SITZUNGSENDE))
        .first()
    )
    return {"seit": zuletzt, "beendet": beendet, "stand": stand}


def laeuft_gerade(grenze: int = 8) -> list[dict]:
    """Startseite „Läuft gerade“: Aufträge in Arbeit aus aktiven Projekten mit ihrem letzten Stand,
    zuletzt Gemeldetes zuerst. Verstummte fehlen hier, sie stehen in „Wartet auf dich“. Nur Lesen.
    ``stand`` ist die letzte Meldung ohne Antworten und Ende-Vermerk, sonst None."""
    zeilen = []
    for a in Auftrag.objects.filter(projekt__aktiv=True, zustand=Auftrag.IN_ARBEIT).select_related(
        "projekt"
    ):
        if verstummt(a):
            continue
        stand = (
            a.meldungen.filter(historisch=False)
            .exclude(art__in=(Meldung.ANTWORT, Meldung.SITZUNGSENDE))
            .first()
        )
        zeit = max((z for z in (a.zuletzt_gemeldet_am, a.uebernommen_am) if z), default=None)
        zeilen.append({"a": a, "stand": stand, "zeit": zeit or a.aktualisiert_am})
    zeilen.sort(key=lambda z: (z["zeit"], z["a"].pk), reverse=True)
    return zeilen[:grenze]


def wartet_auf_dich(projekt: Projekt, modul: Modul | None = None) -> list[Auftrag]:
    """Was in diesem Projekt auf Fred wartet. Zuoberst jeder offene Eintrag ab seinem Termin, der
    älteste Termin zuerst, in jedem Zustand. Danach blockierte Aufträge (Rückfrage, Hindernis,
    Fehler), verstummte Aufträge und Entwürfe, die er freigeben kann oder denen nur sein Arbeitsort
    fehlt. Entwürfe in Planung warten ohne Termin auf einen Chat, nicht auf ihn."""
    eintraege = _eintraege_im_modul(projekt, modul)
    faellige = list(
        eintraege.exclude(zustand__in=Auftrag.ENDZUSTAENDE)
        .filter(faellig__lte=timezone.localdate())
        .select_related("projekt", "modul")
        .order_by("faellig", "id")
    )
    offen = (
        eintraege.filter(zustand__in=(Auftrag.BLOCKIERT, Auftrag.ENTWURF))
        .exclude(pk__in=[a.pk for a in faellige])
        .select_related("projekt", "modul")
    )
    blockiert, entwuerfe = [], []
    for a in offen.order_by("-aktualisiert_am", "-id"):
        if a.zustand == Auftrag.BLOCKIERT:
            blockiert.append(a)
        elif not a.aktuelle_revision.ist_wunsch:
            offene = [p for p in freigabepruefung(a) if not p.erfuellt]
            if all(p.arbeitsort_wahl for p in offene):
                entwuerfe.append(a)
    verstummte = [
        a
        for a in eintraege.filter(zustand=Auftrag.IN_ARBEIT)
        .exclude(pk__in=[f.pk for f in faellige])
        .select_related("projekt", "modul")
        .order_by("id")
        if verstummt(a)
    ]
    return faellige + blockiert + verstummte + entwuerfe

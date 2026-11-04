"""Fachlicher Dienst. Oberfläche und HTTP-Schnittstelle rufen nur diese Funktionen.

Regeln:
- Jede Funktion bekommt den handelnden Akteur und prüft dessen Befugnis selbst.
- Jede Änderung läuft in einer Transaktion und schreibt im selben Commit ein Ereignis.
- Fehler sind ``DienstFehler`` mit einem Text, der dem Menschen angezeigt werden kann.
  ``Konflikt`` ist ein DienstFehler für konkurrierende oder veraltete Anfragen.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import date, timedelta

from django.apps import apps
from django.contrib.auth.models import AbstractBaseUser
from django.db import IntegrityError, transaction
from django.db.models import F, ProtectedError, Q
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Abhaengigkeit,
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Ereignis,
    Ergebnis,
    Meilenstein,
    Meldung,
    Modul,
    OrdnerAnfrage,
    Projekt,
    Revision,
)


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


def projekt_registrieren(
    akteur: Akteur,
    *,
    kennung: str,
    name: str,
    beschreibung: str = "",
    module: list[tuple[str, str]] | None = None,
) -> Projekt:
    """Projekt mit optionalen Modulen anlegen. Erteilt keinem Chat Rechte."""
    _planung_erforderlich(akteur, "Projekte registrieren")
    kennung = slugify(kennung)
    if not kennung:
        raise DienstFehler("Die Kennung darf nur Buchstaben, Ziffern und Bindestriche enthalten.")
    if not name.strip():
        raise DienstFehler("Der Name darf nicht leer sein.")
    try:
        with transaction.atomic():
            projekt = Projekt.objects.create(
                kennung=kennung, name=name.strip(), beschreibung=beschreibung.strip()
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
    except IntegrityError as fehler:
        raise DienstFehler(f"Die Kennung „{kennung}“ ist bereits vergeben.") from fehler
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
    Gelesen wird unter Sperre aus der Datenbank, nie aus einer womöglich veralteten Instanz."""
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
)


def bindung_benutzt(bindung: ChatBindung) -> bool:
    """Hängen Aufträge, Meldungen, Ergebnisse oder frühere Meldungen an dem Zugang, bleibt er für
    den Verlauf."""
    return any(
        apps.get_model("steuerung", modell).objects.filter(**{feld: bindung}).exists()
        for modell, feld in BINDUNG_VERWEISE
    )


def bindung_loeschen(akteur: Akteur, bindung: ChatBindung) -> None:
    """Unbenutzten Zugang endgültig löschen; benutzte werden nur widerrufen (Archiv)."""
    _owner_erforderlich(akteur, "Zugänge löschen")
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
    """Schlüssel prüfen und „zuletzt aktiv“ vermerken; gibt die aktive Bindung zurück oder None."""
    bindung = bindung_finden(schluessel)
    if bindung is not None:
        ChatBindung.objects.filter(pk=bindung.pk).update(zuletzt_aktiv=timezone.now())
    return bindung


def bindung_finden(schluessel: str) -> ChatBindung | None:
    """Schlüssel nur lesend prüfen; gibt die aktive Bindung zurück oder None. Nie Details nach
    außen."""
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


# --- Aufträge: Sicht und Reifegrad ----------------------------------------------------------


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


def _ueberschneidungen(auftrag: Auftrag, revision: Revision) -> list[tuple[Auftrag, str, str]]:
    """Andere belegende Aufträge im selben Arbeitsort mit überschneidenden Mustern."""
    if revision.arbeitsort_id is None or not revision.muster:
        return []
    vorgaenger_ids = set(auftrag.abhaengigkeiten.values_list("vorgaenger_id", flat=True))
    treffer: list[tuple[Auftrag, str, str]] = []
    andere = (
        Auftrag.objects.filter(projekt=auftrag.projekt, zustand__in=Auftrag.BELEGENDE_ZUSTAENDE)
        .exclude(pk=auftrag.pk)
        .exclude(pk__in=vorgaenger_ids)
    )
    for anderer in andere:
        rev = anderer.revisionen.filter(nummer=anderer.revision_nr).first()
        if rev is None or rev.arbeitsort_id != revision.arbeitsort_id:
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
        auftrag = Auftrag.objects.create(
            projekt=projekt,
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
) -> Revision:
    """Neue Revision anlegen. Freigabe und laufende Übernahme erlöschen; der Auftrag geht zurück
    in den Entwurf und braucht eine neue Freigabe."""
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
        elif auftrag.zustand in Auftrag.BEARBEITER_ZUSTAENDE:
            _bearbeiter_abloesen(auftrag)
            auftrag.zustand = Auftrag.ENTWURF
            auftrag.freigegeben_am = None
            folge = f"Übernahme von {auftrag.letzter_bearbeiter_name} erloschen, zurück in Entwurf"
        auftrag.save()
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=f"Revision {nummer} angelegt",
            anlass="; ".join(t for t in (rev.anlass, folge) if t),
            revision=str(nummer),
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
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Freigabe zurückgenommen",
            anlass=grund,
            revision=str(auftrag.revision_nr),
        )


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


def rueckfrage_beantworten(akteur: Akteur, auftrag: Auftrag, text: str) -> Meldung:
    """Antwort des Menschen auf Rückfrage, Hindernis oder Fehler; hebt die Blockade auf."""
    _planung_erforderlich(akteur, "Rückfragen beantworten")
    if not text.strip():
        raise DienstFehler("Die Antwort darf nicht leer sein.")
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
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
            anlass=text.strip()[:200],
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
        .filter(zustand=Auftrag.FREIGEGEBEN, bearbeiter_bindung=None, bearbeiter_benutzer=None)
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


def wartet_auf_dich(projekt: Projekt, modul: Modul | None = None) -> list[Auftrag]:
    """Was in diesem Projekt auf Fred wartet. Zuoberst jeder offene Eintrag ab seinem Termin, der
    älteste Termin zuerst, in jedem Zustand. Danach blockierte Aufträge (Rückfrage, Hindernis,
    Fehler) und Entwürfe, die er freigeben kann oder denen nur sein Arbeitsort fehlt. Entwürfe in
    Planung warten ohne Termin auf einen Chat, nicht auf ihn."""
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
    return faellige + blockiert + entwuerfe

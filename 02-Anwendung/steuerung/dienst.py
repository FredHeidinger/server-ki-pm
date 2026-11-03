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
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.text import slugify

from .models import (
    Abhaengigkeit,
    Arbeitsort,
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
    Ergebnis,
    Kriteriumsbewertung,
    Meldung,
    Modul,
    OrdnerAnfrage,
    Projekt,
    Pruefung,
    Revision,
    Uebernahmebestaetigung,
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
    """Modul archivieren oder zurückholen; Aufträge, Funktionen und Unterlagen behalten ihr
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


ZAHLEN_LEER = {"funktionen": 0, "auftraege": 0, "issues": 0, "unterlagen": 0}


@dataclass
class Modulknoten:
    modul: Modul
    tiefe: int
    kinder: list["Modulknoten"]
    zahlen: dict  # Funktionen, Aufträge, Issues, Unterlagen – einschließlich Untermodule


def modulbaum(projekt: Projekt) -> dict:
    """Aktive Module als Baum unter „Gesamtes Projekt“. Die Zahlen je Knoten schließen alle
    Untermodule ein, auch archivierte; „projektweit“ zählt, was keinem Modul zugeordnet ist."""
    from .models import Funktion, Unterlage

    module = {m.pk: m for m in projekt.module.order_by("name")}
    eigene: dict[int | None, dict] = {}

    def zaehle(modul_id, art):
        eigene.setdefault(modul_id, dict(ZAHLEN_LEER))[art] += 1

    for mid in (
        Funktion.objects.filter(projekt=projekt)
        .exclude(planung=Funktion.VERWORFEN)
        .values_list("modul_id", flat=True)
    ):
        zaehle(mid, "funktionen")
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
) -> Arbeitsort:
    """Arbeitsort anlegen. Legt ihn die KI an, ist er ein Vorschlag: er ordnet keinen Ordner zu
    und zählt nicht für die Freigabe, bis ein Mensch ihn bestätigt. Schlägt die KI einen Ordner
    vor, den das Projekt schon hat, gilt der vorhandene Arbeitsort (keine Doppelanlage)."""
    _planung_erforderlich(akteur, "Arbeitsorte anlegen")
    _projekt_sichtbar(akteur, projekt)
    kennung = slugify(kennung)
    if not kennung or not name.strip():
        raise DienstFehler("Kennung und Name sind erforderlich.")
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


def bindung_benutzt(bindung: ChatBindung) -> bool:
    """Hängen Aufträge, Meldungen oder Ergebnisse an dem Zugang, bleibt er für den Verlauf."""
    return (
        bindung.auftraege.exists()
        or bindung.meldungen.exists()
        or bindung.ergebnisse.exists()
        or Auftrag.objects.filter(letzter_bearbeiter_bindung=bindung).exists()
    )


def bindung_loeschen(akteur: Akteur, bindung: ChatBindung) -> None:
    """Unbenutzten Zugang endgültig löschen; benutzte werden nur widerrufen (Archiv)."""
    _owner_erforderlich(akteur, "Zugänge löschen")
    if bindung_benutzt(bindung):
        raise DienstFehler(
            f"„{bindung.name}“ hat schon an Aufträgen gearbeitet und bleibt für den Verlauf "
            "erhalten. Widerrufen schiebt ihn ins Archiv."
        )
    with transaction.atomic():
        _ereignis(
            akteur,
            projekt=bindung.projekt,
            objekt_typ="Bindung",
            objekt_kennung=bindung.name,
            ereignis="Zugang gelöscht",
        )
        bindung.delete()


# --- Ordner ohne Projekt -------------------------------------------------------------------


def ordner_unbekannt_melden(bindung: ChatBindung, ordner: str) -> None:
    """Unbekannten Arbeitsordner im Posteingang zur Zuordnung vorlegen (einmal je Ordner)."""
    norm = _ordner_normiert(ordner)
    if not norm:
        return
    OrdnerAnfrage.objects.update_or_create(
        normiert=norm[:300], defaults={"ordner": ordner.strip()[:300], "bindung": bindung}
    )


def ordner_zuordnen(akteur: Akteur, anfrage: OrdnerAnfrage, projekt: Projekt) -> Arbeitsort:
    """Ordner als Arbeitsort des Projekts anlegen; erledigt alle Anfragen, die er abdeckt."""
    _owner_erforderlich(akteur, "Ordner zuordnen")
    ordner = anfrage.ordner.rstrip("/\\")
    name = ordner.replace("\\", "/").rsplit("/", 1)[-1] or ordner
    kennung = freie_kennung(Arbeitsort, projekt, name, "ordner")
    with transaction.atomic():
        ort = arbeitsort_anlegen(
            akteur, projekt, kennung=kennung, name=name, rechner=anfrage.bindung.name, pfad=ordner
        )
        for offen in OrdnerAnfrage.objects.all():
            if projekt_fuer_ordner(offen.ordner)[0] is not None:
                offen.delete()
    return ort


def ordner_verwerfen(akteur: Akteur, anfrage: OrdnerAnfrage) -> None:
    _owner_erforderlich(akteur, "Ordneranfragen verwerfen")
    anfrage.delete()


def bindung_pruefen(schluessel: str) -> ChatBindung | None:
    """Schlüssel prüfen; gibt die aktive Bindung zurück oder None. Nie Details nach außen."""
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
    ChatBindung.objects.filter(pk=bindung.pk).update(zuletzt_aktiv=timezone.now())
    return bindung


# --- Aufträge: Sicht und Reifegrad ----------------------------------------------------------


def auftrag_holen(akteur: Akteur, auftrag_id: int) -> Auftrag:
    """Auftrag für den Akteur laden; fremde Projekte sind für Chats nicht vorhanden."""
    auftrag = (
        Auftrag.objects.select_related("projekt", "modul", "pruefer", "bearbeiter_bindung")
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
    qs = Auftrag.objects.select_related("projekt", "modul")
    if projekt is not None:
        qs = qs.filter(projekt=projekt)
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
    punkte.append(
        Pruefpunkt(
            auftrag.pruefer_id is not None, "Prüfer festgelegt", "Ein Mensch, nicht der Bearbeiter."
        )
    )
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
    abgeleitet_von: Auftrag | None = None,
    entscheidung: Entscheidung | None = None,
    erzwingen: bool = False,
    typ: str = Auftrag.TYP_AUFTRAG,
    planung: dict | None = None,
) -> Auftrag:
    """Auftrag im Entwurf anlegen. Menschen und Chats des Projekts; mit ``planung`` (Ziel,
    Nicht-Ziele, Arbeitsort, Schreibumfang, Quellen, Abnahmekriterien) gleich geplant, das dürfen
    nur Owner und Assistent. Gleicher Titel wie ein offener Auftrag gilt als Doppelung, außer
    ``erzwingen``."""
    _projekt_sichtbar(akteur, projekt)
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
            titel=titel.strip(),
            prioritaet=prioritaet,
            typ=typ,
            revision_nr=1,
            abgeleitet_von=abgeleitet_von,
            entscheidung=entscheidung,
            entscheidung_stand=entscheidung.stand if entscheidung else 0,
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
        if abgeleitet_von is not None:
            anlass = f"abgeleitet aus {abgeleitet_von.nummer}"
        if entscheidung is not None:
            anlass = f"Umsetzungsauftrag zu {entscheidung.nummer} Stand {entscheidung.stand}"
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
    pruefer=None,
    freigeben: bool = False,
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
        )
        if pruefer is not None:
            pruefer_festlegen(akteur, auftrag, pruefer)
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


def pruefer_festlegen(akteur: Akteur, auftrag: Auftrag, pruefer) -> None:
    _planung_erforderlich(akteur, "Prüfer festlegen")
    if not pruefer.is_active:
        raise DienstFehler("Der Prüfer muss ein aktives Benutzerkonto sein.")
    with transaction.atomic():
        auftrag.pruefer = pruefer
        auftrag.save(update_fields=["pruefer", "aktualisiert_am"])
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Prüfer festgelegt",
            anlass=pruefer.get_username(),
            revision=str(auftrag.revision_nr),
        )


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
            anlass=IM_CHAT_FREIGEGEBEN
            if akteur.bindung is not None
            else f"Prüfer {auftrag.pruefer.get_username()}",
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


def auftrag_pausieren(akteur: Akteur, auftrag: Auftrag, grund: str = "") -> None:
    """Den Zustand „Pausiert“ gibt es im Ablauf ohne Abnahme nicht mehr."""
    _owner_erforderlich(akteur, "Aufträge pausieren")
    raise Konflikt("Pausieren gibt es nicht mehr: Freigabe zurücknehmen oder abbrechen.")


def auftrag_fortsetzen(akteur: Akteur, auftrag: Auftrag) -> None:
    """Nur für pausierte Aufträge aus der Zeit vor Migration 0008."""
    _owner_erforderlich(akteur, "Aufträge fortsetzen")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.zustand != Auftrag.PAUSIERT:
            raise Konflikt("Nur ein pausierter Auftrag kann fortgesetzt werden.")
        auftrag.zustand = auftrag.zustand_vor_pause or Auftrag.ENTWURF
        auftrag.zustand_vor_pause = ""
        auftrag.save(update_fields=["zustand", "zustand_vor_pause", "aktualisiert_am"])
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Fortgesetzt",
            anlass=f"wieder {auftrag.get_zustand_display()}",
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


def hindernis_aufheben(akteur: Akteur, auftrag: Auftrag, grund: str = "") -> None:
    _owner_erforderlich(akteur, "Hindernisse aufheben")
    with transaction.atomic():
        auftrag = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if auftrag.zustand != Auftrag.BLOCKIERT:
            raise Konflikt("Der Auftrag ist nicht blockiert.")
        auftrag.zustand = Auftrag.IN_ARBEIT
        auftrag.save(update_fields=["zustand", "aktualisiert_am"])
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Hindernis aufgehoben",
            anlass=grund,
            revision=str(auftrag.revision_nr),
        )


def vorschlag_bewerten(akteur: Akteur, meldung: Meldung, *, uebernommen: bool) -> None:
    _owner_erforderlich(akteur, "Vorschläge bewerten")
    if meldung.art != Meldung.VORSCHLAG or meldung.vorschlag_status != Meldung.OFFEN:
        raise DienstFehler("Dieser Vorschlag ist nicht offen.")
    with transaction.atomic():
        meldung.vorschlag_status = Meldung.UEBERNOMMEN if uebernommen else Meldung.ABGELEHNT
        meldung.save(update_fields=["vorschlag_status"])
        _ereignis(
            akteur,
            projekt=meldung.auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=meldung.auftrag.nummer,
            ereignis="Vorschlag übernommen" if uebernommen else "Vorschlag abgelehnt",
            anlass=f"von {meldung.akteur_name}: {meldung.text[:120]}",
            revision=str(meldung.auftrag.revision_nr),
        )


# --- Aufträge: Bearbeiter (Chat oder Mensch) -----------------------------------------------


def auftrag_uebernehmen(akteur: Akteur, auftrag: Auftrag, revision_nr: int) -> Auftrag:
    """Exklusive Übernahme in einer Transaktion. Genau eine konkurrierende Anfrage gewinnt."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    if akteur.bindung is None and not akteur.ist_owner:
        raise KeinZugriff("Übernehmen dürfen Chat-Bindungen oder der Owner.")
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
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
        if akteur.benutzer is not None and aktuell.pruefer_id == akteur.benutzer.pk:
            raise Konflikt(
                "Der Prüfer darf den Auftrag nicht selbst bearbeiten. Zuerst den Prüfer ändern."
            )
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


def vorschlag_einreichen(akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str) -> Meldung:
    """Vorschlag zu einem sichtbaren Auftrag; wird erst durch den Owner wirksam."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    if not text.strip():
        raise DienstFehler("Der Text darf nicht leer sein.")
    if auftrag.ist_endzustand:
        raise Konflikt("Zu einem abgeschlossenen Auftrag sind keine Vorschläge mehr möglich.")
    with transaction.atomic():
        meldung = Meldung.objects.create(
            auftrag=auftrag,
            revision_nr=revision_nr,
            art=Meldung.VORSCHLAG,
            text=text.strip(),
            akteur_name=akteur.name,
            bindung=akteur.bindung,
            historisch=revision_nr != auftrag.revision_nr,
        )
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Vorschlag eingereicht",
            anlass=text.strip()[:200],
            revision=str(revision_nr),
        )
    return meldung


def pruefbericht_einreichen(
    akteur: Akteur, auftrag: Auftrag, revision_nr: int, text: str
) -> Meldung:
    """Prüfbericht eines anderen Chats; ersetzt keine Abnahme."""
    _projekt_sichtbar(akteur, auftrag.projekt)
    if akteur.ist_bearbeiter_von(auftrag):
        raise KeinZugriff(
            "Der Bearbeiter darf keinen Prüfbericht zu seinem eigenen Auftrag abgeben."
        )
    if not text.strip():
        raise DienstFehler("Der Text darf nicht leer sein.")
    with transaction.atomic():
        meldung = Meldung.objects.create(
            auftrag=auftrag,
            revision_nr=revision_nr,
            art=Meldung.PRUEFBERICHT,
            text=text.strip(),
            akteur_name=akteur.name,
            bindung=akteur.bindung,
            historisch=revision_nr != auftrag.revision_nr,
        )
        _ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis="Prüfbericht eingereicht",
            anlass=text.strip()[:200],
            revision=str(revision_nr),
        )
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
    """Ergebnis mit Nachweisen einreichen; die technischen Prüfungen schließen ab, es gibt keine
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


# --- Prüfung ---------------------------------------------------------------------------


def ergebnis_pruefen(
    akteur: Akteur,
    ergebnis: Ergebnis,
    *,
    bewertungen: list[tuple[str, str]],
    entscheidung: str,
    begruendung: str = "",
    integration_offen: bool = False,
) -> Pruefung:
    """Abnahme oder Nacharbeit. Nur Menschen; Prüfer darf nicht der Einreicher sein."""
    _mensch_erforderlich(akteur, "Prüfen")
    assert akteur.benutzer is not None
    auftrag = ergebnis.auftrag
    if auftrag.pruefer_id != akteur.benutzer.pk and not akteur.ist_owner:
        raise KeinZugriff("Prüfen darf nur der festgelegte Prüfer oder der Owner.")
    if ergebnis.benutzer_id is not None and ergebnis.benutzer_id == akteur.benutzer.pk:
        raise KeinZugriff("Selbstabnahme ist nicht erlaubt: Prüfer und Einreicher sind identisch.")
    if entscheidung not in (Pruefung.ABGENOMMEN, Pruefung.NACHARBEIT):
        raise DienstFehler("Entscheidung muss Abnahme oder Nacharbeit sein.")
    kriterien = auftrag.revisionen.get(nummer=ergebnis.revision_nr).kriterien
    if len(bewertungen) != len(kriterien):
        raise DienstFehler("Jedes Abnahmekriterium braucht eine Bewertung.")
    for (bewertung, anmerkung), kriterium in zip(bewertungen, kriterien, strict=True):
        if bewertung not in (
            Kriteriumsbewertung.ERFUELLT,
            Kriteriumsbewertung.NICHT_ERFUELLT,
            Kriteriumsbewertung.NICHT_GEPRUEFT,
        ):
            raise DienstFehler(f"Ungültige Bewertung für „{kriterium}“.")
        if entscheidung == Pruefung.ABGENOMMEN:
            if bewertung == Kriteriumsbewertung.NICHT_ERFUELLT:
                raise Konflikt(f"Abnahme nicht möglich: „{kriterium}“ ist nicht erfüllt.")
            if bewertung == Kriteriumsbewertung.NICHT_GEPRUEFT and not anmerkung.strip():
                raise DienstFehler(f"„{kriterium}“ ist nicht geprüft; das braucht eine Begründung.")
    if entscheidung == Pruefung.NACHARBEIT and not begruendung.strip():
        raise DienstFehler("Nacharbeit braucht eine Begründung.")
    with transaction.atomic():
        aktuell = Auftrag.objects.select_for_update().get(pk=auftrag.pk)
        if aktuell.zustand != Auftrag.ZUR_PRUEFUNG:
            raise Konflikt("Dieser Auftrag wartet nicht auf Prüfung.")
        if aktuell.revision_nr != ergebnis.revision_nr:
            raise Konflikt("Das Ergebnis gehört zu einer überholten Revision.")
        neuestes = aktuell.ergebnisse.first()
        if neuestes is None or neuestes.pk != ergebnis.pk:
            raise Konflikt("Nur das zuletzt eingereichte Ergebnis kann geprüft werden.")
        pruefung = Pruefung.objects.create(
            ergebnis=ergebnis,
            pruefer=akteur.benutzer,
            entscheidung=entscheidung,
            begruendung=begruendung.strip(),
            integration_offen=integration_offen,
        )
        for pos, ((bewertung, anmerkung), kriterium) in enumerate(
            zip(bewertungen, kriterien, strict=True), start=1
        ):
            Kriteriumsbewertung.objects.create(
                pruefung=pruefung,
                position=pos,
                kriterium=kriterium,
                bewertung=bewertung,
                anmerkung=anmerkung.strip(),
            )
        if entscheidung == Pruefung.ABGENOMMEN:
            aktuell.zustand = Auftrag.ABGENOMMEN
            aktuell.abgenommene_revision = ergebnis.revision_nr
            _bearbeiter_abloesen(aktuell)
            ereignis = "Abgenommen" + (" (Integrationsprüfung offen)" if integration_offen else "")
        else:
            aktuell.zustand = Auftrag.NACHARBEIT
            ereignis = "Nacharbeit angefordert"
        aktuell.save()
        _ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis=ereignis,
            anlass=begruendung.strip()
            or f"Dateistand: {ergebnis.dateiliste[0] if ergebnis.dateiliste else '–'}",
            revision=str(ergebnis.revision_nr),
        )
    return pruefung


# --- Entscheidungen --------------------------------------------------------------------


def entscheidung_vorschlagen(
    akteur: Akteur, *, titel: str, text: str, projekte: list[Projekt], verweis: str = ""
) -> Entscheidung:
    if not titel.strip() or not text.strip():
        raise DienstFehler("Titel und Text sind erforderlich.")
    if not projekte:
        raise DienstFehler("Mindestens ein betroffenes Projekt ist anzugeben.")
    if akteur.bindung is not None and (
        akteur.projekt is None or akteur.projekt.pk not in {p.pk for p in projekte}
    ):
        raise KeinZugriff(
            "Ein Chat kann nur Entscheidungen vorschlagen, die sein Projekt betreffen."
        )
    with transaction.atomic():
        entscheidung = Entscheidung.objects.create(
            titel=titel.strip(),
            text=text.strip(),
            verweis=verweis.strip(),
            vorgeschlagen_von_name=akteur.name,
        )
        entscheidung.betroffene_projekte.set(projekte)
        _ereignis(
            akteur,
            projekt=None,
            objekt_typ="Entscheidung",
            objekt_kennung=entscheidung.nummer,
            ereignis="Entscheidung vorgeschlagen",
            anlass=", ".join(p.kennung for p in projekte),
        )
    return entscheidung


def entscheidung_treffen(akteur: Akteur, entscheidung: Entscheidung, *, text: str = "") -> None:
    """Entscheiden darf nur ein Mensch (Owner). Jeder neue Stand verlangt neue Bestätigungen."""
    _owner_erforderlich(akteur, "Entscheiden")
    if entscheidung.zustand == Entscheidung.ZURUECKGEZOGEN:
        raise Konflikt("Eine zurückgezogene Entscheidung kann nicht mehr entschieden werden.")
    with transaction.atomic():
        entscheidung.zustand = Entscheidung.ENTSCHIEDEN
        entscheidung.stand += 1
        if text.strip():
            entscheidung.text = text.strip()
        entscheidung.entschieden_von = akteur.benutzer
        entscheidung.entschieden_am = timezone.now()
        entscheidung.save()
        _ereignis(
            akteur,
            projekt=None,
            objekt_typ="Entscheidung",
            objekt_kennung=entscheidung.nummer,
            ereignis="Entschieden",
            anlass=f"Stand {entscheidung.stand}",
            revision=f"Stand {entscheidung.stand}",
        )


def entscheidung_zurueckziehen(akteur: Akteur, entscheidung: Entscheidung, grund: str) -> None:
    _owner_erforderlich(akteur, "Entscheidungen zurückziehen")
    if not grund.strip():
        raise DienstFehler("Zurückziehen braucht eine Begründung.")
    with transaction.atomic():
        entscheidung.zustand = Entscheidung.ZURUECKGEZOGEN
        entscheidung.save(update_fields=["zustand"])
        _ereignis(
            akteur,
            projekt=None,
            objekt_typ="Entscheidung",
            objekt_kennung=entscheidung.nummer,
            ereignis="Zurückgezogen",
            anlass=grund.strip(),
            revision=f"Stand {entscheidung.stand}",
        )


def uebernahme_bestaetigen(akteur: Akteur, entscheidung: Entscheidung, projekt: Projekt) -> None:
    """Chat bestätigt für sein Projekt, der Owner für jedes betroffene Projekt."""
    if entscheidung.zustand != Entscheidung.ENTSCHIEDEN:
        raise Konflikt("Nur eine entschiedene Entscheidung kann übernommen werden.")
    if akteur.bindung is not None and (akteur.projekt is None or akteur.projekt.pk != projekt.pk):
        raise KeinZugriff("Ein Chat kann nur für sein eigenes Projekt bestätigen.")
    if akteur.bindung is None and not akteur.ist_owner:
        raise KeinZugriff("Bestätigen dürfen Chat-Bindungen oder der Owner.")
    if not entscheidung.betroffene_projekte.filter(pk=projekt.pk).exists():
        raise DienstFehler(f"{projekt.name} ist von dieser Entscheidung nicht betroffen.")
    try:
        with transaction.atomic():
            Uebernahmebestaetigung.objects.create(
                entscheidung=entscheidung,
                projekt=projekt,
                stand=entscheidung.stand,
                bestaetigt_von_name=akteur.name,
            )
            _ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="Entscheidung",
                objekt_kennung=entscheidung.nummer,
                ereignis="Übernahme bestätigt",
                anlass=f"für {projekt.kennung}",
                revision=f"Stand {entscheidung.stand}",
            )
    except IntegrityError as fehler:
        raise Konflikt(
            f"Stand {entscheidung.stand} ist für {projekt.name} bereits bestätigt."
        ) from fehler


def umsetzungsauftrag_anlegen(
    akteur: Akteur, entscheidung: Entscheidung, projekt: Projekt
) -> Auftrag:
    _owner_erforderlich(akteur, "Umsetzungsaufträge anlegen")
    if entscheidung.zustand != Entscheidung.ENTSCHIEDEN:
        raise Konflikt("Umsetzungsaufträge gibt es nur zu entschiedenen Entscheidungen.")
    if not entscheidung.betroffene_projekte.filter(pk=projekt.pk).exists():
        raise DienstFehler(f"{projekt.name} ist von dieser Entscheidung nicht betroffen.")
    return wunsch_erfassen(
        akteur,
        projekt,
        titel=f"Umsetzung {entscheidung.nummer}: {entscheidung.titel}"[:200],
        beschreibung=entscheidung.text,
        entscheidung=entscheidung,
    )


# --- Posteingang und Meine Aufträge --------------------------------------------------------


def posteingang(benutzer) -> list[dict]:
    """Alles, was jetzt eine menschliche Entscheidung braucht, als konkrete Einträge."""
    eintraege: list[dict] = []
    offen = Auftrag.objects.exclude(zustand__in=Auftrag.ENDZUSTAENDE).select_related(
        "projekt", "modul", "pruefer"
    )
    for a in offen.filter(zustand=Auftrag.ZUR_PRUEFUNG):
        e = a.ergebnisse.first()
        if e is None:
            continue
        selbst = e.benutzer_id == benutzer.pk
        eintraege.append(
            {
                "art": "pruefung",
                "auftrag": a,
                "ergebnis": e,
                "zeit": e.eingereicht_am,
                "titel": f"{a.nummer} wartet auf Prüfung"
                + (" (eigene Arbeit, prüft ein anderer)" if selbst else ""),
                "text": e.zusammenfassung,
                "von": e.eingereicht_von_name,
                "selbst": selbst,
            }
        )
    for m in (
        Meldung.objects.filter(
            auftrag__zustand=Auftrag.BLOCKIERT, art__in=Meldung.BLOCKIERENDE, historisch=False
        )
        .select_related("auftrag", "auftrag__projekt")
        .order_by("-zeitpunkt")
    ):
        if any(x["art"] == "blockade" and x["auftrag"].pk == m.auftrag_id for x in eintraege):
            continue
        eintraege.append(
            {
                "art": "blockade",
                "auftrag": m.auftrag,
                "meldung": m,
                "zeit": m.zeitpunkt,
                "titel": f"{m.auftrag.nummer} {m.get_art_display()}: {m.auftrag.titel}",
                "text": m.text,
                "von": m.akteur_name,
            }
        )
    for a in offen.filter(zustand=Auftrag.KLAERUNG_ERFORDERLICH):
        eintraege.append(
            {
                "art": "klaerung",
                "auftrag": a,
                "zeit": a.aktualisiert_am,
                "titel": f"{a.nummer} braucht Klärung: {a.titel}",
                "text": f"Übernahme von {a.letzter_bearbeiter_name or '–'} erloschen; neu freigeben oder abbrechen.",
                "von": "",
            }
        )
    for m in (
        Meldung.objects.filter(art=Meldung.VORSCHLAG, vorschlag_status=Meldung.OFFEN)
        .exclude(auftrag__zustand__in=Auftrag.ENDZUSTAENDE)
        .select_related("auftrag", "auftrag__projekt")
    ):
        eintraege.append(
            {
                "art": "vorschlag",
                "auftrag": m.auftrag,
                "meldung": m,
                "zeit": m.zeitpunkt,
                "titel": f"Vorschlag zu {m.auftrag.nummer}: {m.auftrag.titel}",
                "text": m.text,
                "von": m.akteur_name,
            }
        )
    for a in offen.filter(zustand=Auftrag.ENTWURF):
        rev = a.revisionen.filter(nummer=1).first()
        if rev is None or rev.erstellt_von_name == benutzer.get_username():
            continue
        eintraege.append(
            {
                "art": "wunsch",
                "auftrag": a,
                "zeit": a.angelegt_am,
                "titel": f"{a.nummer} {a.titel}",
                "text": rev.beschreibung,
                "von": rev.erstellt_von_name,
            }
        )
    for e in Entscheidung.objects.filter(zustand=Entscheidung.VORSCHLAG):
        eintraege.append(
            {
                "art": "entscheidung",
                "entscheidung": e,
                "zeit": e.vorgeschlagen_am,
                "titel": f"Entscheidung {e.nummer} vorgeschlagen: {e.titel}",
                "text": e.text,
                "von": e.vorgeschlagen_von_name,
            }
        )
    for o in OrdnerAnfrage.objects.select_related("bindung"):
        eintraege.append(
            {
                "art": "ordner",
                "anfrage": o,
                "zeit": o.angelegt_am,
                "titel": f"Neuer Arbeitsordner: {o.ordner}",
                "text": "Ein Chat arbeitet in diesem Ordner. Zu welchem Projekt gehört er?",
                "von": o.bindung.name,
            }
        )
    eintraege.sort(key=lambda x: x["zeit"] or timezone.now())
    return eintraege


def meine_auftraege(benutzer):
    """Aufträge, die der Mensch selbst bearbeitet oder als Prüfer offen hat."""
    return (
        Auftrag.objects.filter(
            Q(bearbeiter_benutzer=benutzer) | Q(pruefer=benutzer, zustand=Auftrag.ZUR_PRUEFUNG)
        )
        .exclude(zustand__in=Auftrag.ENDZUSTAENDE)
        .select_related("projekt", "modul")
        .order_by("prioritaet", "id")
    )


# --- Übersicht ---------------------------------------------------------------------------


def kacheln(projekt: Projekt | None) -> dict[str, int]:
    auftraege = Auftrag.objects.all()
    entscheidungen = Entscheidung.objects.filter(zustand=Entscheidung.VORSCHLAG)
    if projekt is not None:
        auftraege = auftraege.filter(projekt=projekt)
        entscheidungen = entscheidungen.filter(betroffene_projekte=projekt)
    wuensche = sum(
        1
        for a in auftraege.filter(zustand=Auftrag.ENTWURF).prefetch_related("revisionen")
        if a.aktuelle_revision.ist_wunsch
    )
    return {
        "zur_pruefung": auftraege.filter(zustand=Auftrag.ZUR_PRUEFUNG).count(),
        "wuensche": wuensche,
        "blockiert": auftraege.filter(zustand=Auftrag.BLOCKIERT).count(),
        "entscheidungen": entscheidungen.distinct().count(),
        "klaerung": auftraege.filter(zustand=Auftrag.KLAERUNG_ERFORDERLICH).count(),
    }


def projektzeilen(projekte, *, mit_modulen: bool = True) -> list[dict]:
    """Je Projekt und Modul: Auftragszahlen je Zustand, zuletzt gemeldet, zuletzt geprüft."""
    zaehlzustaende = [
        ("entwurf", Auftrag.ENTWURF),
        ("freigegeben", Auftrag.FREIGEGEBEN),
        ("in_arbeit", (Auftrag.IN_ARBEIT, Auftrag.NACHARBEIT, Auftrag.BLOCKIERT)),
        ("zur_pruefung", Auftrag.ZUR_PRUEFUNG),
        ("abgenommen", Auftrag.ABGENOMMEN),
    ]
    grenze = timezone.now() - timedelta(days=7)

    def zeile(name, kennung, auftraege, ist_modul=False):
        zahlen = {}
        for schluessel, zustand in zaehlzustaende:
            z = zustand if isinstance(zustand, tuple) else (zustand,)
            zahlen[schluessel] = auftraege.filter(zustand__in=z).count()
        gemeldet = (
            auftraege.exclude(zuletzt_gemeldet_am=None)
            .order_by("-zuletzt_gemeldet_am")
            .values_list("zuletzt_gemeldet_am", "bearbeiter_name", "letzter_bearbeiter_name")
            .first()
        )
        geprueft = (
            Pruefung.objects.filter(ergebnis__auftrag__in=auftraege)
            .select_related("pruefer")
            .order_by("-zeitpunkt")
            .first()
        )
        zahlen["offen"] = sum(v for k, v in zahlen.items() if k != "abgenommen")
        return {
            "name": name,
            "kennung": kennung,
            "ist_modul": ist_modul,
            **zahlen,
            "gemeldet_am": gemeldet[0] if gemeldet else None,
            "gemeldet_von": (gemeldet[1] or gemeldet[2]) if gemeldet else "",
            "gemeldet_alt": bool(gemeldet and gemeldet[0] < grenze),
            "geprueft_am": geprueft.zeitpunkt if geprueft else None,
            "geprueft_von": geprueft.pruefer.get_username() if geprueft else "",
        }

    ergebnis = []
    for projekt in projekte:
        alle = Auftrag.objects.filter(projekt=projekt)
        ergebnis.append(zeile(projekt.name, projekt.kennung, alle))
        if mit_modulen:
            for modul in projekt.module.filter(aktiv=True):
                ergebnis.append(zeile(modul.name, str(modul), alle.filter(modul=modul), True))
    return ergebnis


def menschen():
    return get_user_model().objects.filter(is_active=True).order_by("username")

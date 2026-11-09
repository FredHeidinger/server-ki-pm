"""Werkbank-Anbindung: Instanzen und ihre Zugänge (Vertrag kipm-werkbank/1, Auftrag Abschnitt 5).

Dieses Modul bündelt die fachlichen Regeln der Serverhälfte; Views und API übersetzen nur. Es
startet nichts auf dem PC, liest keine PC-Ordner und führt keinen Projektcode aus. Jede Änderung
prüft die Befugnis des Owners, läuft in einer kurzen Transaktion und schreibt im selben Commit ein
Ereignis. Der Zugang einer Werkbank ist eine ``ChatBindung`` der Art „werkbank“: weder Owner noch
Assistent, ohne eigenes Projekt, mit den erlaubten Projekten an ``WerkbankInstanz``.
"""

import base64
import binascii
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import dienst, werkbank_vertrag
from .models import (
    AUSFUEHRUNG_AKTIV,
    Auftrag,
    ChatBindung,
    Projekt,
    ServerKennung,
    WerkbankAusfuehrung,
    WerkbankInstanz,
    WerkbankOperation,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)


def server_id() -> uuid.UUID:
    """Die dauerhafte Kennung dieser KI-PM-Installation (liegt in der Datenbank)."""
    return ServerKennung.holen().server_id


def _projekte_pruefen(projekte: list[Projekt]) -> list[Projekt]:
    eindeutig = {p.pk: p for p in projekte}
    if not eindeutig:
        raise dienst.DienstFehler("Mindestens ein Projekt muss erlaubt sein.")
    for projekt in eindeutig.values():
        if not Projekt.objects.filter(pk=projekt.pk, aktiv=True).exists():
            raise dienst.DienstFehler(f"Das Projekt „{projekt.name}“ ist archiviert.")
    return list(eindeutig.values())


def _bindungen_sichern(instanz: WerkbankInstanz, projekte: list[Projekt]) -> None:
    """Jedes erlaubte Projekt bekommt seine Projektbindung (Ziel einer Zuweisung), mit dem ersten
    aktiven, bestätigten Arbeitsort des Projekts, falls es einen gibt. Bestehende Bindungen
    bleiben (Fassung und gemeldete Bereitschaft gehen nicht verloren)."""
    for projekt in projekte:
        ort = projekt.arbeitsorte.filter(aktiv=True, bestaetigt=True).order_by("id").first()
        WerkbankProjektbindung.objects.get_or_create(
            instanz=instanz, projekt=projekt, defaults={"arbeitsort": ort}
        )


KOPPLUNGSCODE_KOPF = "kipm-kopplung/1:"


def kopplungscode(
    instanz: WerkbankInstanz,
    schluessel: str | None,
    adresse: str,
    *,
    bindung: WerkbankProjektbindung | None = None,
) -> str:
    """Ein Text mit allen Einrichtungswerten, damit Fred sie nicht einzeln abtippen muss.

    Mit Schlüssel und allen Projekten nur beim Anlegen gezeigt, nie gespeichert. Mit ``bindung``
    und ``schluessel=None`` ein Code nur für diese Projektbindung ohne Schlüssel (``null``: die
    Werkbank behält ihren Zugang); den darf die Seite jederzeit zeigen. Das Format ist eine
    Bequemlichkeit außerhalb des Vertrags ``kipm-werkbank/1``: ``kipm-kopplung/1:`` und danach
    URL-sicheres Base64 eines JSON-Objekts (``01-Planung/werkbank-anbindung/KOPPLUNGSCODE.md``).
    Der Arbeitsort ist der aktuelle bestätigte Ordner des Projekts oder ``null``.
    """
    if bindung is not None and bindung.instanz_id != instanz.pk:
        raise ValueError("Die Projektbindung gehört zu einer anderen Werkbank.")
    bindungen = (
        [bindung]
        if bindung is not None
        else instanz.projekte.select_related("projekt").order_by("projekt__name")
    )
    projekte = []
    for b in bindungen:
        ort = (
            b.projekt.arbeitsorte.filter(aktiv=True, bestaetigt=True)
            .exclude(pfad="")
            .order_by("id")
            .first()
        )
        projekte.append(
            {
                "projektbindung_id": b.pk,
                "projektbindung_version": b.version,
                "projekt": b.projekt.kennung,
                "arbeitsort": ort.pfad if ort else None,
            }
        )
    daten = {
        "adresse": adresse,
        "schluessel": schluessel,
        "server_id": str(server_id()),
        "werkbank_id": str(instanz.werkbank_id),
        "projekte": projekte,
    }
    roh = json.dumps(daten, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return KOPPLUNGSCODE_KOPF + base64.urlsafe_b64encode(roh.encode("utf-8")).decode(
        "ascii"
    ).rstrip("=")


def instanz_anlegen(
    akteur: dienst.Akteur,
    *,
    name: str,
    projekte: list[Projekt],
    gueltig_tage: int = 90,
    notiz: str = "",
) -> tuple[WerkbankInstanz, str]:
    """Werkbank-Instanz mit eigenem Zugang anlegen. Gibt den Klartextschlüssel genau einmal
    zurück; gespeichert wird nur sein Hash."""
    dienst._owner_erforderlich(akteur, "Werkbänke anlegen")
    if not name.strip():
        raise dienst.DienstFehler("Der Name darf nicht leer sein.")
    if not 1 <= gueltig_tage <= 365:
        raise dienst.DienstFehler("Die Gültigkeit muss zwischen 1 und 365 Tagen liegen.")
    projekte = _projekte_pruefen(projekte)
    praefix = secrets.token_hex(6)
    schluessel = f"kipm_{praefix}_{secrets.token_urlsafe(32)}"
    with transaction.atomic():
        bindung = ChatBindung.objects.create(
            projekt=None,
            art=ChatBindung.ART_WERKBANK,
            name=name.strip(),
            notiz=notiz.strip(),
            praefix=praefix,
            schluessel_hash=dienst._schluessel_hash(schluessel),
            gueltig_bis=timezone.now() + timedelta(days=gueltig_tage),
        )
        instanz = WerkbankInstanz.objects.create(name=name.strip(), bindung=bindung)
        instanz.erlaubte_projekte.set(projekte)
        _bindungen_sichern(instanz, projekte)
        dienst._ereignis(
            akteur,
            projekt=None,
            objekt_typ="Werkbank",
            objekt_kennung=instanz.name,
            ereignis="Werkbank angelegt",
            anlass="Projekte: " + ", ".join(p.kennung for p in projekte),
        )
    return instanz, schluessel


def instanz_projekte_setzen(
    akteur: dienst.Akteur, instanz: WerkbankInstanz, projekte: list[Projekt]
) -> None:
    dienst._owner_erforderlich(akteur, "Erlaubte Projekte ändern")
    projekte = _projekte_pruefen(projekte)
    with transaction.atomic():
        instanz.erlaubte_projekte.set(projekte)
        _bindungen_sichern(instanz, projekte)
        dienst._ereignis(
            akteur,
            projekt=None,
            objekt_typ="Werkbank",
            objekt_kennung=instanz.name,
            ereignis="Werkbank: erlaubte Projekte geändert",
            anlass="Projekte: " + ", ".join(p.kennung for p in projekte),
        )


def instanz_aktiv_setzen(akteur: dienst.Akteur, instanz: WerkbankInstanz, aktiv: bool) -> None:
    dienst._owner_erforderlich(akteur, "Werkbänke aktivieren oder deaktivieren")
    if instanz.aktiv == aktiv:
        return
    with transaction.atomic():
        instanz.aktiv = aktiv
        instanz.save(update_fields=["aktiv"])
        dienst._ereignis(
            akteur,
            projekt=None,
            objekt_typ="Werkbank",
            objekt_kennung=instanz.name,
            ereignis="Werkbank aktiviert" if aktiv else "Werkbank deaktiviert",
        )


def instanz_widerrufen(akteur: dienst.Akteur, instanz: WerkbankInstanz, grund: str = "") -> None:
    """Zugang sperren; die Instanz bleibt für den Verlauf, ist aber nicht mehr aktiv."""
    dienst._owner_erforderlich(akteur, "Werkbank-Zugänge widerrufen")
    if instanz.bindung is None:
        raise dienst.DienstFehler("Diese Werkbank hat keinen Zugang mehr.")
    with transaction.atomic():
        dienst.bindung_widerrufen(akteur, instanz.bindung, grund)
        instanz.aktiv = False
        instanz.save(update_fields=["aktiv"])


def instanz_benutzt(instanz: WerkbankInstanz) -> bool:
    """Hatte die Werkbank Kontakt, hat sie Bereitschaft gemeldet oder hängen Zuweisungen,
    Operationen, Ereignisse an ihr, bleibt sie für den Verlauf erhalten (die beim Anlegen
    erzeugten Projektbindungen allein zählen nicht)."""
    return bool(
        instanz.letzter_kontakt is not None
        or instanz.projekte.filter(bereit_gemeldet_am__isnull=False).exists()
        or instanz.operationen.exists()
        or instanz.ereignisse.exists()
        or WerkbankZuweisung.objects.filter(instanz=instanz).exists()
    )


def instanz_loeschen(akteur: dienst.Akteur, instanz: WerkbankInstanz) -> None:
    """Nur eine unbenutzte Werkbank samt Zugang endgültig löschen; benutzte werden widerrufen."""
    dienst._owner_erforderlich(akteur, "Werkbänke löschen")
    if instanz_benutzt(instanz):
        raise dienst.DienstFehler(
            f"„{instanz.name}“ war schon in Kontakt oder hat Spuren hinterlassen und bleibt für "
            "den Verlauf erhalten. Widerrufen sperrt den Zugang."
        )
    with transaction.atomic():
        dienst._ereignis(
            akteur,
            projekt=None,
            objekt_typ="Werkbank",
            objekt_kennung=instanz.name,
            ereignis="Werkbank gelöscht",
        )
        bindung = instanz.bindung
        instanz.delete()
        if bindung is not None:
            bindung.delete()


def instanz_zu_schluessel(schluessel: str) -> WerkbankInstanz | None:
    """Die aktive Instanz zu einem gültigen Werkbank-Schlüssel, sonst None. Schreibt nichts."""
    bindung = dienst.werkbank_bindung_finden(schluessel)
    if bindung is None:
        return None
    return WerkbankInstanz.objects.filter(bindung=bindung, aktiv=True).first()


def projekt_erlaubt(instanz: WerkbankInstanz, projekt: Projekt) -> bool:
    """Darf diese Werkbank das Projekt erreichen? Nur ausdrücklich erlaubte, aktive Projekte."""
    return instanz.aktiv and instanz.erlaubte_projekte.filter(pk=projekt.pk, aktiv=True).exists()


# --- Snapshot und Kontextbasis (WP4) ---------------------------------------------------------


def _kurz(wert: str, grenze: int = 500) -> str:
    return (wert or "").strip()[:grenze] or "–"


def _zeilen(text: str) -> list[str]:
    return [z.strip()[:20000] for z in (text or "").splitlines() if z.strip()]


def snapshot_bauen(auftrag: Auftrag) -> dict:
    """Der Auftragssnapshot nach ``auftragssnapshot.schema.json`` aus dem aktuellen Stand:
    Revision des Auftrags, Profil, geltende Entscheidungen, Texte und Links mit ihren Fassungen.
    Fremder Inhalt ist von Geheimnissen bereinigt. Nichts davon wird abgerufen; Links bleiben
    Adressen. Fortschritt anderer Aufträge, Termine, Meilensteine und Besuchszeiten fehlen."""
    from . import akte
    from .models import Entscheidung, Projekt, Unterlage

    projekt = Projekt.objects.get(pk=auftrag.projekt_id)
    rev = auftrag.aktuelle_revision
    ort = rev.arbeitsort
    profil = [
        f"Zweck: {projekt.zweck}",
        f"Zielgruppe: {projekt.zielgruppe}",
        f"Nicht-Ziele: {projekt.nicht_ziele}",
        f"Entwicklungsstufe: {projekt.entwicklungsstufe}",
        f"Phase: {projekt.phase}",
        f"Beschreibung: {projekt.beschreibung}",
    ]
    entscheidungen = [
        {
            "id": e.pk,
            "version": max(e.stand, 1),
            "titel": _kurz(akte._bereinigt(e.titel)),
            "entscheidung": akte._bereinigt(e.text)[:20000] or "–",
            "warum": akte._bereinigt(e.warum)[:20000],
        }
        for e in akte.entscheidungen_des_projekts(projekt)
        .filter(zustand=Entscheidung.ENTSCHIEDEN)
        .order_by("id")
    ]
    texte, links = [], []
    for u in projekt.unterlagen.filter(archiviert=False).order_by("id"):
        v = akte.aktuelle_fassung(u)
        if v is None or not akte.ki_lesbar(v):
            continue
        if u.art == Unterlage.TEXT:
            texte.append(
                {
                    "id": u.pk,
                    "version": v.nummer,
                    "titel": _kurz(akte._bereinigt(u.titel)),
                    "inhalt": akte._bereinigt(v.text)[:100000],
                }
            )
        elif u.art == Unterlage.LINK:
            links.append(
                {
                    "id": u.pk,
                    "version": v.nummer,
                    "titel": _kurz(akte._bereinigt(u.titel)),
                    "url": akte._bereinigt(v.uri)[:2000],
                }
            )
    return {
        "snapshot_version": 1,
        "auftrag_id": auftrag.pk,
        "nummer": auftrag.nummer,
        "titel": _kurz(akte._bereinigt(auftrag.titel)),
        "revision": auftrag.revision_nr,
        "beschreibung": akte._bereinigt(rev.beschreibung)[:20000],
        "ziel": akte._bereinigt(rev.ziel)[:20000],
        "nicht_ziele": [akte._bereinigt(z) for z in _zeilen(rev.nicht_ziele)],
        "schreibumfang": [_kurz(z) for z in rev.muster],
        "arbeitsort": {
            "id": ort.pk if ort else 1,
            "name": _kurz(ort.name if ort else ""),
            "beschreibung": akte._bereinigt(ort.pfad or ort.repository)[:2000] if ort else "",
        },
        "abnahmekriterien": [
            {"id": f"AK-{i}", "text": akte._bereinigt(k)[:20000]}
            for i, k in enumerate(rev.kriterien, start=1)
        ],
        "quellenhinweise": [akte._bereinigt(z) for z in _zeilen(rev.quellen)],
        "profil": {
            "fassung": projekt.profil_revision,
            "text": akte._bereinigt("\n".join(profil))[:50000],
        },
        "entscheidungen": entscheidungen,
        "texte": texte,
        "links": links,
    }


def kontext_hash(snapshot: dict) -> str:
    """Hash der fachlichen Kontextbasis: Arbeitsort, Profil, Entscheidungen, Texte und Links mit
    ihren Fassungen. Der Auftragsinhalt selbst ist durch die Revision gebunden."""
    teil = {k: snapshot[k] for k in ("arbeitsort", "profil", "entscheidungen", "texte", "links")}
    return werkbank_vertrag.sha256_hex(teil)


def kontext_binden(auftrag: Auftrag) -> None:
    """Die Freigabe eines Werkbank-Auftrags bindet die Kontextbasis (``dienst.auftrag_freigeben``
    ruft das im selben Commit)."""
    zuweisung = WerkbankZuweisung.objects.filter(auftrag=auftrag).first()
    if zuweisung is not None:
        zuweisung.kontext_sha256 = kontext_hash(snapshot_bauen(auftrag))
        zuweisung.save(update_fields=["kontext_sha256", "geaendert_am"])


def freigabe_aufgehoben(auftrag: Auftrag) -> None:
    """Die Freigabe ist erloschen oder zurückgenommen: Die Kontextbindung fällt, die
    Zuweisungsfassung steigt, damit keine alte Meldung mehr passt."""
    zuweisung = WerkbankZuweisung.objects.filter(auftrag=auftrag).first()
    if zuweisung is not None and zuweisung.kontext_sha256:
        zuweisung.kontext_sha256 = ""
        zuweisung.version += 1
        zuweisung.save(update_fields=["kontext_sha256", "version", "geaendert_am"])


# --- Zuweisung (WP4) -------------------------------------------------------------------------


def _aktive_ausfuehrung(auftrag: Auftrag) -> bool:
    return WerkbankAusfuehrung.objects.filter(
        auftrag=auftrag, zustand__in=AUSFUEHRUNG_AKTIV
    ).exists()


def _zurueck_in_entwurf(auftrag: Auftrag) -> None:
    if auftrag.zustand == Auftrag.FREIGEGEBEN:
        auftrag.zustand = Auftrag.ENTWURF
        auftrag.freigegeben_am = None


def zuweisen(
    akteur: dienst.Akteur,
    auftrag: Auftrag,
    projektbindung: WerkbankProjektbindung,
    *,
    freigeben: bool = False,
) -> WerkbankZuweisung:
    """Das Ausführungsziel eines Auftrags festlegen (nur der Owner). Reserviert nur das Ziel und
    setzt keinen Bearbeiter. Ein neues oder anderes Ziel hebt die Freigabe auf und erhöht beim
    Wechsel die Zuweisungsfassung; dasselbe Ziel noch einmal ändert nichts. Mit ``freigeben`` wird
    im selben Vorgang freigegeben (alles oder nichts)."""
    dienst._owner_erforderlich(akteur, "Aufträge einer Werkbank zuweisen")
    with transaction.atomic():
        a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        ziel = WerkbankProjektbindung.objects.select_related("instanz", "projekt").get(
            pk=projektbindung.pk
        )
        if ziel.projekt_id != a.projekt_id:
            raise dienst.DienstFehler("Das Ziel gehört zu einem anderen Projekt als der Auftrag.")
        if not a.projekt.aktiv:
            raise dienst.Konflikt(f"{a.nummer} gehört zu einem archivierten Projekt.")
        if not projekt_erlaubt(ziel.instanz, a.projekt):
            raise dienst.DienstFehler(
                f"„{ziel.instanz.name}“ ist abgeschaltet oder darf dieses Projekt nicht erreichen."
            )
        if (
            a.zustand not in (Auftrag.ENTWURF, Auftrag.FREIGEGEBEN)
            or a.hat_bearbeiter
            or _aktive_ausfuehrung(a)
        ):
            raise dienst.Konflikt(
                f"{a.nummer} ist schon in Arbeit ({a.get_zustand_display()}); das Ziel ändert sich "
                "erst nach „Bearbeitung ändern“."
            )
        zuweisung = WerkbankZuweisung.objects.filter(auftrag=a).first()
        ereignis = None
        if zuweisung is None:
            zuweisung = WerkbankZuweisung.objects.create(
                auftrag=a, instanz=ziel.instanz, projektbindung=ziel, geaendert_von=akteur.name
            )
            ereignis = "Werkbank zugewiesen"
            _zurueck_in_entwurf(a)
        elif zuweisung.projektbindung_id != ziel.pk:
            zuweisung.instanz = ziel.instanz
            zuweisung.projektbindung = ziel
            zuweisung.version += 1
            zuweisung.kontext_sha256 = ""
            zuweisung.geaendert_von = akteur.name
            ereignis = "Werkbank-Ziel gewechselt"
            _zurueck_in_entwurf(a)
        if ereignis:
            a.ausfuehrungsweg = Auftrag.WEG_WERKBANK
            a.save()
            zuweisung.ereignis = dienst._ereignis(
                akteur,
                projekt=a.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=a.nummer,
                ereignis=ereignis,
                anlass=f"{ziel.instanz.name}, Zuweisungsfassung {zuweisung.version}",
                revision=str(a.revision_nr),
            )
            zuweisung.save()
        if freigeben and a.zustand == Auftrag.ENTWURF:
            dienst.auftrag_freigeben(akteur, a)
            zuweisung.refresh_from_db()
    return zuweisung


def zuweisung_aufheben(akteur: dienst.Akteur, auftrag: Auftrag) -> None:
    """Das Ausführungsziel wieder entfernen (nur der Owner), solange nie eine Ausführung
    begonnen hat; der Auftrag geht zurück in den Entwurf und wird wieder ein Chat-Auftrag."""
    dienst._owner_erforderlich(akteur, "Die Zuweisung an eine Werkbank aufheben")
    with transaction.atomic():
        a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        zuweisung = WerkbankZuweisung.objects.filter(auftrag=a).first()
        if zuweisung is None:
            raise dienst.DienstFehler(f"{a.nummer} ist keiner Werkbank zugewiesen.")
        if a.hat_bearbeiter or zuweisung.ausfuehrungen.exists():
            raise dienst.Konflikt(
                f"{a.nummer} hatte schon eine Ausführung; die Zuweisung bleibt für den Verlauf."
            )
        ziel = zuweisung.instanz.name
        zuweisung.delete()
        _zurueck_in_entwurf(a)
        a.ausfuehrungsweg = Auftrag.WEG_CHAT
        a.save()
        dienst._ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=a.nummer,
            ereignis="Werkbank-Zuweisung aufgehoben",
            anlass=ziel,
            revision=str(a.revision_nr),
        )


# --- Werkbank-API (WP3): Takt, Fehler, Quellbindung, Wiederholungsschutz --------------------

ABRUF_S = 10
LEBENSZEICHEN_S = 30
WARNUNG_S = 90
SEITE_MAX = 100
MAX_NACHRICHT_BYTES = 1024 * 1024

#: Nachrichtenart (Endpunkt) → Schema der Anfrage.
ANFRAGE_SCHEMA = {
    "uebernehmen": "uebernehmen_anfrage",
    "schritt": "schritt_anfrage",
    "ereignisse": "ereignisse_anfrage",
}
#: Nachrichtenart → ``WerkbankOperation.art``.
OPERATION_ART = {"uebernehmen": "uebernehmen", "schritt": "schritt", "ereignisse": "ereignis"}
_VERTRAGSZUSTAENDE = {
    "plant", "laeuft", "wartet_antwort", "wartet_freigabe", "fehler", "unklar", "offline",
    "halt_angefordert", "angehalten", "abbruch_angefordert", "abgebrochen", "abschluss_gemeldet",
}  # fmt: skip


class WerkbankFehler(Exception):
    """Ein Fehler der Werkbank-API im Vertragsformat (``fehler.schema.json``): stabiler Code,
    verständlicher Text, nächste zulässige Handlung. Nie Geheimnisse oder Stacktraces."""

    def __init__(
        self,
        status: int,
        code: str,
        text: str,
        naechste_handlung: str,
        details: dict | None = None,
    ):
        super().__init__(text)
        self.status = status
        self.code = code
        self.text = text
        self.naechste_handlung = naechste_handlung
        self.details = details

    def koerper(self) -> dict:
        fehler: dict[str, Any] = {
            "code": self.code,
            "text": self.text,
            "naechste_handlung": self.naechste_handlung,
        }
        if self.details:
            fehler["details"] = self.details
        return {"fehler": fehler}


class Ausgang(NamedTuple):
    """Ergebnis einer Operation: HTTP-Status und Antwortkörper (wird als Nachweis gespeichert)."""

    status: int
    body: dict


def kontakt_setzen(instanz: WerkbankInstanz) -> None:
    """Letzten Kontakt auf die Empfangszeit des Servers setzen (nie die Uhr der Werkbank)."""
    WerkbankInstanz.objects.filter(pk=instanz.pk).update(letzter_kontakt=timezone.now())


def nicht_erreichbar(instanz: WerkbankInstanz, jetzt: datetime | None = None) -> bool:
    """Erreichbarkeitswarnung: letzter Kontakt länger als ``WARNUNG_S`` Sekunden her. Eine Werkbank
    ohne jeden Kontakt ist noch nicht verbunden und löst keine Warnung aus."""
    if instanz.letzter_kontakt is None:
        return False
    jetzt = jetzt or timezone.now()
    return (jetzt - instanz.letzter_kontakt).total_seconds() > WARNUNG_S


def bereitschaft_melden(projektbindung: WerkbankProjektbindung, nutzlast: dict) -> list[str]:
    """Die gemeldeten Fingerabdrücke und Fähigkeiten speichern (Auftrag 9: keine eigene
    Regelprüfung). Gibt die Schlüssel der Konfigurationsfingerabdrücke zurück, die sich gegenüber
    der letzten Meldung geändert haben oder weggefallen sind; ein neuer Repository-Fingerabdruck
    (normale Git-Fortschreibung) zählt nicht. WP4 ruft das beim Übernehmen auf."""
    alt = projektbindung.konfig_fingerabdruecke or {}
    neu = dict(nutzlast["konfig_fingerabdruecke"])
    geaendert = sorted(k for k, wert in alt.items() if neu.get(k) != wert)
    projektbindung.repo_fingerabdruck = nutzlast["repo_fingerabdruck"]
    projektbindung.konfig_fingerabdruecke = neu
    projektbindung.bereit = True
    projektbindung.bereit_gruende = []
    projektbindung.bereit_gemeldet_am = timezone.now()
    projektbindung.save(
        update_fields=[
            "repo_fingerabdruck",
            "konfig_fingerabdruecke",
            "bereit",
            "bereit_gruende",
            "bereit_gemeldet_am",
        ]
    )
    instanz = projektbindung.instanz
    instanz.faehigkeiten = {"liste": list(nutzlast["faehigkeiten"])}
    instanz.versionen = {"werkbank": nutzlast["werkbank_version"]}
    instanz.save(update_fields=["faehigkeiten", "versionen"])
    return geaendert


def _zeit(wert: datetime) -> str:
    return wert.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _vertragszustand(zustand: str) -> str:
    """Die Zustände „abgeschlossen“ und „beendet“ kennt der Vertrag nur als „abschluss_gemeldet“."""
    return zustand if zustand in _VERTRAGSZUSTAENDE else "abschluss_gemeldet"


# --- Nachrichten prüfen ---------------------------------------------------------------------


def _ungueltig(text: str, verstoesse: list[str] | None = None) -> WerkbankFehler:
    details = {"verstoesse": [v[:300] for v in verstoesse[:10]]} if verstoesse else None
    return WerkbankFehler(
        400,
        "UNGUELTIGE_NACHRICHT",
        text,
        "Nachricht nach Vertrag kipm-werkbank/1 korrigieren.",
        details,
    )


def nachricht_pruefen(art: str, roh: bytes) -> dict:
    """Größe, JSON, Schema und Inhaltshash einer ändernden Nachricht prüfen."""
    if len(roh) > MAX_NACHRICHT_BYTES:
        raise WerkbankFehler(
            413,
            "ZU_GROSS",
            f"Die Nachricht ist größer als {MAX_NACHRICHT_BYTES} Byte.",
            "Nachricht verkleinern oder in mehrere Nachrichten teilen.",
        )
    try:
        daten = json.loads(roh.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError) as fehler:
        raise _ungueltig("Die Nachricht ist kein gültiges JSON.") from fehler
    if not isinstance(daten, dict):
        raise _ungueltig("Die Nachricht muss ein JSON-Objekt sein.")
    verstoesse = werkbank_vertrag.schema_fehler(ANFRAGE_SCHEMA[art], daten)
    if verstoesse:
        raise _ungueltig("Die Nachricht entspricht nicht dem Vertrag.", verstoesse)
    try:
        hash_ = werkbank_vertrag.inhaltshash(daten)
    except ValueError as fehler:
        raise _ungueltig("Die Nachricht enthält nicht kanonisierbare Werte.") from fehler
    if hash_ != daten["inhaltshash"]:
        raise _ungueltig("Der Inhaltshash passt nicht zur Nachricht.")
    return daten


# --- Quellbindung ---------------------------------------------------------------------------


@dataclass
class QuellePruefung:
    projektbindung: WerkbankProjektbindung
    auftrag: Auftrag
    zuweisung: WerkbankZuweisung
    ausfuehrung: WerkbankAusfuehrung | None = None
    #: Veraltete Felder → aktueller Wert (leer = Quelle aktuell).
    veraltet: dict[str, Any] = field(default_factory=dict)

    @property
    def aktiv(self) -> bool:
        return self.ausfuehrung is None or self.ausfuehrung.zustand in AUSFUEHRUNG_AKTIV


def _nicht_gefunden() -> WerkbankFehler:
    return WerkbankFehler(
        404,
        "NICHT_GEFUNDEN",
        "Das Objekt ist unbekannt oder für diese Werkbank nicht zugänglich.",
        "Abgleich abrufen und mit den dort genannten Objekten fortfahren.",
    )


def quelle_pruefen(instanz: WerkbankInstanz, quelle: dict) -> QuellePruefung:
    """Die vollständige Quellbindung gegen den Datenstand prüfen. Unbekanntes oder Fremdes → 404,
    falsche ``server_id`` → 409, veraltete Fassungen stehen in ``veraltet`` (die Wirkung
    entscheidet: Befehle lehnen mit 409 ab, späte Meldungen werden historisch quittiert)."""
    if quelle["server_id"] != str(server_id()):
        raise WerkbankFehler(
            409,
            "ZUSTANDSKONFLIKT",
            "Die server_id gehört zu einer anderen KI-PM-Installation.",
            "Verbindung prüfen; die Werkbank darf nicht mit diesem Server weiterarbeiten.",
        )
    if quelle["werkbank_id"] != str(instanz.werkbank_id):
        raise _nicht_gefunden()
    bindung = (
        WerkbankProjektbindung.objects.select_related("projekt")
        .filter(pk=quelle["projektbindung_id"], instanz=instanz)
        .first()
    )
    if bindung is None or not projekt_erlaubt(instanz, bindung.projekt):
        raise _nicht_gefunden()
    auftrag = Auftrag.objects.filter(pk=quelle["auftrag_id"], projekt=bindung.projekt).first()
    zuweisung = (
        WerkbankZuweisung.objects.filter(auftrag=auftrag, instanz=instanz).first()
        if auftrag is not None
        else None
    )
    if auftrag is None or zuweisung is None:
        raise _nicht_gefunden()
    ausfuehrung = None
    if "ausfuehrung_id" in quelle:
        ausfuehrung = WerkbankAusfuehrung.objects.filter(
            ausfuehrung_id=quelle["ausfuehrung_id"], auftrag=auftrag, zuweisung=zuweisung
        ).first()
        if ausfuehrung is None:
            raise _nicht_gefunden()
    ergebnis = QuellePruefung(bindung, auftrag, zuweisung, ausfuehrung)
    veraltet = ergebnis.veraltet
    if quelle["projektbindung_version"] != bindung.version:
        veraltet["aktuelle_projektbindung_version"] = bindung.version
    if quelle["zuweisung_version"] != zuweisung.version:
        veraltet["aktuelle_zuweisung_version"] = zuweisung.version
    aktuelle_revision = ausfuehrung.revision if ausfuehrung is not None else auftrag.revision_nr
    if quelle["revision"] != aktuelle_revision:
        veraltet["aktuelle_revision"] = aktuelle_revision
    if ausfuehrung is not None and quelle["snapshot_sha256"] != ausfuehrung.snapshot_sha256:
        veraltet["snapshot_abweichend"] = True
    return ergebnis


def _quelle_aktuell(q: QuellePruefung) -> None:
    """Befehle (``uebernehmen``, ``schritt``) brauchen eine aktuelle Quelle und eine laufende,
    nicht angehaltene Ausführung; sonst 409."""
    if q.veraltet:
        raise WerkbankFehler(
            409,
            "VERALTET",
            "Die Quellfassung ist nicht mehr aktuell.",
            "Abgleich abrufen und mit der neuen Fassung neu beginnen.",
            dict(q.veraltet),
        )
    if q.ausfuehrung is not None:
        if not q.aktiv:
            raise WerkbankFehler(
                409,
                "ZUSTANDSKONFLIKT",
                "Die Ausführung ist beendet.",
                "Abgleich abrufen; in dieser Ausführung startet nichts Neues.",
            )
        if q.ausfuehrung.halt_zweck:
            raise WerkbankFehler(
                409,
                "HALT_ANGEFORDERT",
                "Für diese Ausführung ist ein Halt angefordert.",
                "Keine neuen Schritte starten; den Stillstand mit halt_bestaetigt melden.",
            )


# --- Wiederholungsschutz --------------------------------------------------------------------


def _noch_nicht_verfuegbar(name: str) -> WerkbankFehler:
    return WerkbankFehler(
        501,
        "NOCH_NICHT_VERFUEGBAR",
        f"„{name}“ wirkt in dieser Serverfassung noch nicht.",
        "Nichts wiederholen: Der Server hat die Meldung nicht angenommen und nichts geändert.",
    )


def _konflikt(text: str, handlung: str, code: str = "ZUSTANDSKONFLIKT") -> WerkbankFehler:
    return WerkbankFehler(409, code, text, handlung)


def _uebernehmen(instanz, n, q: QuellePruefung, op: WerkbankOperation) -> Ausgang:
    """Atomare Übernahme (Auftrag 6): prüft alles gegen den gesperrten Stand und erzeugt genau
    eine Ausführung samt Snapshot, setzt den Bearbeiter und IN_ARBEIT. Läuft in der Transaktion
    von ``operation_ausfuehren`` zusammen mit dem Wiederholungsnachweis."""
    pb = q.projektbindung
    a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=q.auftrag.pk)
    zuweisung = q.zuweisung
    abgleich = "Abgleich abrufen und den aktuellen Stand abwarten."
    if not a.projekt.aktiv:
        raise _konflikt(f"{a.nummer} gehört zu einem archivierten Projekt.", abgleich)
    if a.ausfuehrungsweg != Auftrag.WEG_WERKBANK or a.zustand != Auftrag.FREIGEGEBEN:
        raise _konflikt(
            f"{a.nummer} ist nicht übernehmbar (Zustand {a.get_zustand_display()}).", abgleich
        )
    if a.hat_bearbeiter or _aktive_ausfuehrung(a):
        raise _konflikt(f"{a.nummer} hat schon einen Bearbeiter oder eine Ausführung.", abgleich)
    snapshot = snapshot_bauen(a)
    if not zuweisung.kontext_sha256 or zuweisung.kontext_sha256 != kontext_hash(snapshot):
        raise _konflikt(
            "Der fachliche Kontext hat sich seit der Freigabe geändert.",
            "Der Owner gibt den Auftrag in KI-PM neu frei; danach neu übernehmen.",
            "KONTEXT_VERALTET",
        )
    offen = [p.text for p in dienst.freigabepruefung(a) if not p.erfuellt]
    if offen:
        raise _konflikt("Übernahme nicht möglich: " + "; ".join(offen), abgleich)
    sha = werkbank_vertrag.sha256_hex(snapshot)
    ausfuehrung = WerkbankAusfuehrung.objects.create(
        auftrag=a,
        revision=a.revision_nr,
        zuweisung=zuweisung,
        zuweisung_version=zuweisung.version,
        projektbindung=pb,
        projektbindung_version=pb.version,
        snapshot=snapshot,
        snapshot_sha256=sha,
        zustand="plant",
        statusversion=1,
    )
    jetzt = timezone.now()
    geaendert = Auftrag.objects.filter(
        pk=a.pk,
        zustand=Auftrag.FREIGEGEBEN,
        bearbeiter_bindung=None,
        bearbeiter_benutzer=None,
        revision_nr=a.revision_nr,
    ).update(
        zustand=Auftrag.IN_ARBEIT,
        bearbeiter_bindung=instanz.bindung,
        bearbeiter_name=f"{instanz.name} (Werkbank)"[:120],
        uebernommen_am=jetzt,
        uebernommene_revision=a.revision_nr,
        aktualisiert_am=jetzt,
    )
    if geaendert != 1:
        raise _konflikt(f"{a.nummer} wurde gerade anderweitig übernommen.", abgleich)
    op.ausfuehrung = ausfuehrung
    op.save(update_fields=["ausfuehrung"])
    bereitschaft_melden(pb, n["nutzlast"])
    dienst._ereignis(
        dienst.Akteur.aus_bindung(instanz.bindung),
        projekt=a.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=a.nummer,
        ereignis="Übernommen",
        anlass=f"Werkbank {instanz.name}, Ausführung {ausfuehrung.ausfuehrung_id}",
        revision=str(a.revision_nr),
    )
    return Ausgang(
        200,
        {
            "vertrag": werkbank_vertrag.VERTRAG,
            "operation_id": n["operation_id"],
            "ergebnis": "uebernommen",
            "quelle": _quelle_der_ausfuehrung(ausfuehrung, instanz),
            "statusversion": ausfuehrung.statusversion,
            "snapshot": snapshot,
        },
    )


#: Wirkung je Nachrichtenart (``uebernehmen``, ``schritt``) und je Ereignisart. Fehlt eine
#: Ereignisart, antwortet die API „noch nicht verfügbar“. ``werkbank_lauf`` und
#: ``werkbank_aktionen`` tragen ihre Wirkungen am Dateiende ein.
HANDLER: dict[str, Callable[..., Ausgang]] = {
    "uebernehmen": _uebernehmen,
}


def _handler(art: str, n: dict) -> Callable[..., Ausgang]:
    _teilmodule()
    name = n["nutzlast"]["ereignis"]["art"] if art == "ereignisse" else art
    handler = HANDLER.get(name)
    if handler is None:
        raise _noch_nicht_verfuegbar(name)
    return handler


def _schluesselfelder(art: str, n: dict) -> dict:
    if art != "schritt":
        return {"klaerung": "geklaert"}
    nutzlast = n["nutzlast"]
    schritt = nutzlast["schritt"]
    return {
        "planfassung": schritt["planfassung"],
        "paket": schritt["paket"],
        "phase": schritt["phase"],
        "position": schritt["position"],
        "versuch_nr": nutzlast["versuch_nr"],
        "eingabe_sha256": nutzlast["eingabehash"],
    }


def _wiederholung(
    instanz: WerkbankInstanz, art: str, n: dict, bekannt: WerkbankOperation
) -> Ausgang:
    if bekannt.nutzlast_sha256 != n["inhaltshash"] or bekannt.art != OPERATION_ART[art]:
        raise WerkbankFehler(
            409,
            "WIDERSPRUCH_WIEDERHOLUNG",
            "Diese operation_id kam schon mit anderem Inhalt.",
            "Für eine neue Nachricht eine neue operation_id verwenden; für eine Wiederholung "
            "die Nachricht unverändert senden.",
        )
    if art == "schritt":
        # Ein alter Erfolg ist kein neuer Startschein: Berechtigungsstand erneut prüfen.
        q = quelle_pruefen(instanz, n["quelle"])
        _quelle_aktuell(q)
        ablehnung = _teilmodule()[2].wiederholung_pruefen(n, q.ausfuehrung, bekannt)
        if ablehnung is not None:
            return ablehnung
    body = dict(bekannt.ausgang)
    if "ergebnis" in body:
        body["ergebnis"] = "wiederholt"
    return Ausgang(bekannt.ausgang_status or 200, body)


def _operation_einmal(instanz: WerkbankInstanz, art: str, n: dict) -> Ausgang:
    with transaction.atomic():
        bekannt = WerkbankOperation.objects.filter(
            instanz=instanz, operation_id=n["operation_id"]
        ).first()
        if bekannt is not None:
            return _wiederholung(instanz, art, n, bekannt)
        q = quelle_pruefen(instanz, n["quelle"])
        if art in ("uebernehmen", "schritt"):
            _quelle_aktuell(q)
        handler = _handler(art, n)
        if art == "schritt":
            ablehnung = _teilmodule()[2].schritt_vorab(instanz, n, q)
            if ablehnung is not None:
                return ablehnung  # nicht gespeichert: der Versuch bleibt frei
        op = WerkbankOperation.objects.create(
            instanz=instanz,
            operation_id=n["operation_id"],
            art=OPERATION_ART[art],
            nutzlast_sha256=n["inhaltshash"],
            quelle=n["quelle"],
            ausfuehrung=q.ausfuehrung,
            **_schluesselfelder(art, n),
        )
        ausgang = handler(instanz, n, q, op)
        op.ausgang_status = ausgang.status
        op.ausgang = ausgang.body
        op.save(update_fields=["ausgang_status", "ausgang"])
        return ausgang


def operation_ausfuehren(instanz: WerkbankInstanz, art: str, n: dict) -> Ausgang:
    """Eine ändernde Nachricht ausführen. Fachänderung und Operationsnachweis (Hash, Ausgang)
    liegen in derselben kurzen Transaktion, es gibt keine Netzaufrufe darin. Identische
    Wiederholung: gespeicherter Ausgang ohne zweite Wirkung; gleiche ``operation_id`` mit anderem
    Hash: 409. Ein Fehler nimmt alles zurück und speichert keinen Ausgang."""
    for _ in range(2):
        try:
            return _operation_einmal(instanz, art, n)
        except IntegrityError:
            continue  # eine gleichzeitige Wiederholung hat eben gespeichert: neu lesen
    raise WerkbankFehler(
        409,
        "ZUSTANDSKONFLIKT",
        "Die Operation wird gerade verarbeitet.",
        "Kurz warten und mit GET operationen/<uuid> den Ausgang abrufen.",
    )


def operation_abfragen(instanz: WerkbankInstanz, operation_id: str) -> dict:
    """Nur lesend: der gespeicherte Ausgang einer Operation dieser Werkbank. Fremde Operationen
    und unbekannte Kennungen sind gleichermaßen „unbekannt“."""
    op = WerkbankOperation.objects.filter(instanz=instanz, operation_id=operation_id).first()
    if op is None or op.ausgang_status is None:
        return {
            "vertrag": werkbank_vertrag.VERTRAG,
            "operation_id": operation_id,
            "status": "unbekannt",
            "inhaltshash": None,
            "ausgang": None,
        }
    return {
        "vertrag": werkbank_vertrag.VERTRAG,
        "operation_id": operation_id,
        "status": "gespeichert",
        "inhaltshash": op.nutzlast_sha256,
        "ausgang": {"http_status": op.ausgang_status, "antwort": op.ausgang},
    }


# --- Abgleich -------------------------------------------------------------------------------


def _cursor_lesen(text: str | None) -> dict[str, int]:
    if not text:
        return {"z": 0, "h": 0}
    try:
        daten = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        z, h = daten["z"], daten["h"]
    except (ValueError, KeyError, TypeError, binascii.Error) as fehler:
        raise _ungueltig("Der Seitenzeiger „seite“ ist ungültig.") from fehler
    if not (isinstance(z, int) and isinstance(h, int) and z >= 0 and h >= 0):
        raise _ungueltig("Der Seitenzeiger „seite“ ist ungültig.")
    return {"z": z, "h": h}


def _cursor_schreiben(z: int, h: int) -> str:
    roh = json.dumps({"z": z, "h": h}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(roh).decode().rstrip("=")


def _quelle_der_ausfuehrung(ausfuehrung: WerkbankAusfuehrung, instanz: WerkbankInstanz) -> dict:
    return {
        "server_id": str(server_id()),
        "werkbank_id": str(instanz.werkbank_id),
        "projektbindung_id": ausfuehrung.projektbindung_id,
        "projektbindung_version": ausfuehrung.projektbindung_version,
        "auftrag_id": ausfuehrung.auftrag_id,
        "revision": ausfuehrung.revision,
        "zuweisung_version": ausfuehrung.zuweisung_version,
        "ausfuehrung_id": str(ausfuehrung.ausfuehrung_id),
        "snapshot_sha256": ausfuehrung.snapshot_sha256,
    }


def abgleich_seite(
    instanz: WerkbankInstanz, cursor: str | None = None, bekannt: str | None = None
) -> dict:
    """Eine Seite des Abgleichs (``abgleich_antwort``): bis zu ``SEITE_MAX`` Einträge je Liste.
    ``seite.groesse`` ist die größte Listenlänge der Seite. Aktionen und Konflikte füllen WP6/WP7;
    bis dahin sind sie leer. Aufträge ohne Inhaltsrevision (nie freigegeben) fehlen."""
    pos = _cursor_lesen(cursor)
    erlaubt = instanz.erlaubte_projekte.filter(aktiv=True)
    offen = ~Q(auftrag__zustand__in=Auftrag.ENDZUSTAENDE) | Q(
        auftrag__ausfuehrungen__zustand__in=AUSFUEHRUNG_AKTIV
    )
    zuweisungen = list(
        WerkbankZuweisung.objects.filter(
            offen,
            instanz=instanz,
            projektbindung__projekt__in=erlaubt,
            auftrag__revision_nr__gte=1,
            pk__gt=pos["z"],
        )
        .select_related("auftrag", "projektbindung")
        .distinct()
        .order_by("pk")[: SEITE_MAX + 1]
    )
    mehr_z = len(zuweisungen) > SEITE_MAX
    zuweisungen = zuweisungen[:SEITE_MAX]
    aktive = dict(
        WerkbankAusfuehrung.objects.filter(
            zuweisung__in=zuweisungen, zustand__in=AUSFUEHRUNG_AKTIV
        ).values_list("zuweisung_id", "ausfuehrung_id")
    )
    halte = list(
        WerkbankAusfuehrung.objects.filter(
            projektbindung__instanz=instanz,
            projektbindung__projekt__in=erlaubt,
            zustand__in=AUSFUEHRUNG_AKTIV,
            pk__gt=pos["h"],
        )
        .exclude(halt_zweck="")
        .order_by("pk")[: SEITE_MAX + 1]
    )
    mehr_h = len(halte) > SEITE_MAX
    halte = halte[:SEITE_MAX]
    z_pos = zuweisungen[-1].pk if zuweisungen else pos["z"]
    h_pos = halte[-1].pk if halte else pos["h"]
    return {
        "vertrag": werkbank_vertrag.VERTRAG,
        "server_id": str(server_id()),
        "server_zeit": _zeit(timezone.now()),
        "abruf_intervall_s": ABRUF_S,
        "lebenszeichen_max_s": LEBENSZEICHEN_S,
        "warnung_nach_s": WARNUNG_S,
        "seite": {
            "groesse": max(len(zuweisungen), len(halte)),
            "naechste": _cursor_schreiben(z_pos, h_pos) if mehr_z or mehr_h else None,
        },
        "zuweisungen": [
            {
                "auftrag_id": z.auftrag_id,
                "nummer": z.auftrag.nummer,
                "revision": z.auftrag.revision_nr,
                "zuweisung_version": z.version,
                "projektbindung_id": z.projektbindung_id,
                "projektbindung_version": z.projektbindung.version,
                "freigegeben": z.auftrag.zustand in Auftrag.BELEGENDE_ZUSTAENDE,
                "ausfuehrung_id": str(aktive[z.pk]) if z.pk in aktive else None,
            }
            for z in zuweisungen
        ],
        "aktionen": _teilmodule()[1].aktionen_fuer_abgleich(
            instanz,
            WerkbankAusfuehrung.objects.filter(
                projektbindung__instanz=instanz,
                projektbindung__projekt__in=erlaubt,
                zustand__in=AUSFUEHRUNG_AKTIV,
            ),
        ),
        "halte": [
            {
                "ausfuehrung_id": str(a.ausfuehrung_id),
                "zweck": a.halt_zweck,
                "quelle": _quelle_der_ausfuehrung(a, instanz),
            }
            for a in halte
        ],
        "konflikte": _teilmodule()[0].unbekannte_ausfuehrungen(instanz, bekannt),
    }


def _teilmodule():
    """Die Teilmodule laden (sie tragen ihre Wirkungen in ``HANDLER`` ein). Erst beim Gebrauch,
    damit kein Importzyklus entsteht, egal welches Modul zuerst geladen wird."""
    from . import werkbank_abschluss, werkbank_aktionen, werkbank_lauf

    return werkbank_abschluss, werkbank_aktionen, werkbank_lauf


def aktion_erstellen(*args, **kwargs):
    return _teilmodule()[1].aktion_erstellen(*args, **kwargs)


def kontext_geaendert(*args, **kwargs):
    return _teilmodule()[1].kontext_geaendert(*args, **kwargs)

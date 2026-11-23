"""Bestätigungspflichtige Auftragsaktionen.

Die Planung (Owner oder sein KI-Assistent) bereitet eine konkrete Aktion an einem Auftrag vor:
Wiederaufnahme eines blockierten Auftrags beim selben Bearbeiter oder Neuschnitt (neue Revision mit
geänderten Planfeldern, auf Wunsch mit anschließender Freigabe). Der Vorschlag bindet Auftrag,
erwartete Revision, maßgebliche Meldung und Aktion und ändert am Auftrag nichts. Erst Freds
authentifizierte Bestätigung in der Oberfläche führt ihn aus, und zwar ausschließlich über die
bestehende Auftragslogik (``dienst.rueckfrage_beantworten``, ``dienst.auftrag_bearbeiten``,
``dienst.auftrag_freigeben``). Vor der Ausführung werden Revision, Hindernis, Berechtigung und die
angezeigten Auswirkungen (``pruefcode``) erneut geprüft.

Die Auswirkungen entstehen aus einem Probelauf derselben Ausführung in einer zurückgerollten
Transaktion; Anzeige und Ausführung können darum nicht auseinanderlaufen.
"""

import hashlib
import json
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import dienst, lieferungen
from .dienst import Akteur, DienstFehler, KeinZugriff, Konflikt
from .models import (
    Aktionsvollmacht,
    Aktionsvorschlag,
    Auftrag,
    ChatBindung,
    Entscheidung,
    Ereignis,
    Lieferung,
    Meldung,
)

# Felder einer Revision, die ein Neuschnitt ändern darf (wie ``api.PLANFELDER``).
PLANFELDER = ("beschreibung", "ziel", "nicht_ziele", "schreibumfang", "quellen", "abnahmekriterien")
LISTENFELDER = ("schreibumfang", "abnahmekriterien")
TEXT_LAENGE = 4000

# Für den Probelauf: plant wie der Owner, ist aber niemand; alles wird zurückgerollt.
_PROBE = Akteur(benutzer=None, bindung=None, name="Vorschau", ist_owner=True)


class _Zurueckrollen(Exception):
    pass


def _name(auftrag: Auftrag) -> str:
    return auftrag.bearbeiter_name or "niemand"


def _stand(auftrag: Auftrag) -> dict:
    return {
        "zustand": auftrag.get_zustand_display(),
        "bearbeiter": _name(auftrag),
        "revision": auftrag.revision_nr,
    }


def _laden(auftrag_id: int) -> Auftrag:
    return Auftrag.objects.select_related("projekt", "bearbeiter_bindung").get(pk=auftrag_id)


def _grundpruefung(auftrag: Auftrag) -> None:
    if not auftrag.projekt.aktiv:
        raise Konflikt(f"{auftrag.nummer} gehört zu einem archivierten Projekt.")
    if (
        auftrag.ausfuehrungsweg != Auftrag.WEG_CHAT
        or getattr(auftrag, "chat_delegation_id", None) is not None  # W1, wo vorhanden
        or dienst._werkbank_aktiv(auftrag)
    ):
        raise Konflikt(
            f"{auftrag.nummer} läuft über eine Werkbank oder eine W1-Erlaubnis; dort gelten deren "
            "eigene Aktionen."
        )


def _veraltet_grund(v: Aktionsvorschlag, auftrag: Auftrag) -> str:
    """Leer, solange der Vorschlag zum Auftrag passt; sonst der Grund. Einzige Stelle für die
    erneute Prüfung von Revision und Hindernis vor Anzeige und Ausführung."""
    if auftrag.revision_nr != v.erwartete_revision:
        return (
            f"Revision {v.erwartete_revision} ist veraltet; aktuell ist Revision "
            f"{auftrag.revision_nr}."
        )
    if v.art == Aktionsvorschlag.WIEDERAUFNAHME and auftrag.zustand != Auftrag.BLOCKIERT:
        return f"{auftrag.nummer} ist inzwischen {auftrag.get_zustand_display()}, nicht blockiert."
    if (
        v.art == Aktionsvorschlag.NEUSCHNITT
        and auftrag.zustand not in dienst.BEARBEITBARE_ZUSTAENDE
    ):
        return (
            f"{auftrag.nummer} ist {auftrag.get_zustand_display()} und kann nicht neu geplant "
            "werden."
        )
    if v.meldung_id is not None or auftrag.zustand == Auftrag.BLOCKIERT:
        if auftrag.zustand != Auftrag.BLOCKIERT:
            return (
                f"{auftrag.nummer} ist inzwischen {auftrag.get_zustand_display()}, nicht blockiert."
            )
        aktuell = lieferungen._blocker(auftrag)
        if aktuell is None or aktuell.pk != v.meldung_id:
            return (
                "Eine neuere Meldung (Rückfrage, Hindernis, Fehler oder Antwort) hat die "
                "maßgebliche Meldung abgelöst."
            )
    if v.art == Aktionsvorschlag.WIEDERAUFNAHME:
        b = auftrag.bearbeiter_bindung
        if b is not None and not b.aktiv:
            return (
                f"Der Zugang des Bearbeiters {auftrag.bearbeiter_name} ist widerrufen oder "
                "abgelaufen; eine Wiederaufnahme liefe ins Leere. Neu schneiden statt fortsetzen."
            )
        if not auftrag.hat_bearbeiter or auftrag.uebernommene_revision != auftrag.revision_nr:
            return "Der Auftrag hat keine gültige Übernahme dieser Revision."
    return ""


def _plan_lesen(plan) -> dict:
    if not isinstance(plan, dict):
        raise DienstFehler("Ein Neuschnitt braucht einen Plan mit mindestens einem Planfeld.")
    unbekannt = set(plan) - set(PLANFELDER) - {"freigeben"}
    if unbekannt:
        raise DienstFehler(
            "Kein Planfeld: "
            + ", ".join(sorted(unbekannt))
            + f" (erlaubt: {', '.join(PLANFELDER)})."
        )
    werte = {}
    for feld in PLANFELDER:
        if feld not in plan:
            continue
        wert = plan[feld]
        if isinstance(wert, list):
            if not all(isinstance(x, str) for x in wert):
                raise DienstFehler(f"Planfeld {feld}: nur Texte.")
            wert = "\n".join(x.strip() for x in wert if x.strip())
        elif not isinstance(wert, str):
            raise DienstFehler(f"Planfeld {feld}: Text oder Liste von Texten.")
        werte[feld] = wert
    if not werte:
        raise DienstFehler("Ein Neuschnitt braucht mindestens ein Planfeld.")
    freigeben = plan.get("freigeben", False)
    if not isinstance(freigeben, bool):
        raise DienstFehler("freigeben muss true oder false sein.")
    return {**werte, "freigeben": freigeben}


def _ausfuehren(akteur: Akteur, v: Aktionsvorschlag, auftrag: Auftrag) -> None:
    """Die Aktion über die bestehende Auftragslogik. Lieferketten gelten als bestätigt: Fred hat
    sie in der Anzeige gesehen, der ``pruefcode`` bindet die Ausführung an genau diese Anzeige."""
    if v.art == Aktionsvorschlag.WIEDERAUFNAHME:
        if v.vollmacht_id is not None:
            n = v.nachweis
            herkunft = (
                f"mit Vollmacht VM-{v.vollmacht_id} (Nachweis {n['auftrag']} r{n['revision']} "
                f"L-{n['lieferung']})"
            )
        else:
            herkunft = f"bestätigt von {akteur.name}"
        dienst.rueckfrage_beantworten(
            akteur,
            auftrag,
            f"Wiederaufnahme {v.nummer}, {herkunft}: {v.text}",
            lieferketten_bestaetigt=True,
        )
        return
    rev = auftrag.aktuelle_revision
    werte = {feld: getattr(rev, feld) for feld in PLANFELDER}
    werte.update({k: w for k, w in v.plan.items() if k in PLANFELDER})
    dienst.auftrag_bearbeiten(
        akteur,
        auftrag,
        titel=auftrag.titel,
        arbeitsort=rev.arbeitsort,
        prioritaet=auftrag.prioritaet,
        modul=auftrag.modul,
        anlass=f"Neuschnitt {v.nummer}: {v.text[:200]}",
        lieferketten_bestaetigt=True,
        **werte,
    )
    if v.plan.get("freigeben"):
        dienst.auftrag_freigeben(akteur, Auftrag.objects.get(pk=auftrag.pk))


def _hinweise(v: Aktionsvorschlag, vorher: Auftrag, nachher: Auftrag) -> list[str]:
    hinweise = []
    if v.art == Aktionsvorschlag.WIEDERAUFNAHME:
        still = dienst.verstummt(nachher)
        if still and still["beendet"]:
            hinweise.append(
                f"Der Chat von {nachher.bearbeiter_name} ist beendet. Nach der Bestätigung "
                f"einem neuen Chat sagen: „Mach bei {nachher.nummer} weiter.“"
            )
        else:
            hinweise.append(
                f"{nachher.bearbeiter_name} liest die Antwort beim nächsten Abruf und arbeitet "
                "weiter."
            )
        return hinweise
    if vorher.hat_bearbeiter:
        hinweise.append(
            f"Die Übernahme von {vorher.bearbeiter_name} erlischt; ein Chat übernimmt Revision "
            f"{nachher.revision_nr} neu."
        )
    if nachher.zustand == Auftrag.FREIGEGEBEN:
        hinweise.append("Danach freigegeben: Ein Chat übernimmt ihn mit „weiter“.")
    elif nachher.zustand == Auftrag.ENTWURF:
        hinweise.append("Danach Entwurf: Er braucht noch eine Freigabe.")
    return hinweise


def _ketten_texte(v: Aktionsvorschlag, auftrag: Auftrag) -> list[str]:
    ketten = lieferungen.betroffene_ketten(
        auftrag, als_anbieter=v.art == Aktionsvorschlag.NEUSCHNITT
    )
    texte = []
    for k in ketten:
        if k["verbraucher"] == auftrag.nummer:
            texte.append(
                f"Technischer Lieferbedarf entfällt: {k['anbieter']}/{k['lieferkennung']}; "
                f"eine spätere Lieferung weckt {auftrag.nummer} nicht mehr."
            )
        else:
            texte.append(
                f"Lieferkette getrennt: {k['verbraucher']} wartet auf "
                f"{k['anbieter']}/{k['lieferkennung']}; der Bedarf verwaist und muss neu "
                "gebunden werden."
            )
    return texte


def _pruefcode(v: Aktionsvorschlag, daten: dict) -> str:
    roh = json.dumps(
        {"v": v.pk, "rev": v.erwartete_revision, "meldung": v.meldung_id, **daten},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(roh.encode("utf-8")).hexdigest()[:24]


def _probelauf(v: Aktionsvorschlag, auftrag: Auftrag) -> dict:
    """Führt die Aktion aus, liest das Ergebnis und rollt alles zurück."""
    ergebnis: dict = {}
    try:
        with transaction.atomic():
            _ausfuehren(_PROBE, v, auftrag)
            nachher = _laden(auftrag.pk)
            ergebnis["nachher"] = _stand(nachher)
            ergebnis["hinweise"] = _hinweise(v, auftrag, nachher)
            raise _Zurueckrollen
    except _Zurueckrollen:
        pass
    return ergebnis


def auswirkungen(v: Aktionsvorschlag) -> dict:
    """Was die Bestätigung jetzt tatsächlich bewirken würde: Zustand, Bearbeiter und Revision
    vorher und nachher, betroffene Lieferketten, Hinweise. ``gueltig`` False mit ``grund``, wenn
    der Vorschlag veraltet ist oder die Ausführung scheitern würde. ``pruefcode`` bindet die
    Bestätigung an diese Anzeige."""
    auftrag = _laden(v.auftrag_id)
    daten = {
        "gueltig": False,
        "grund": "",
        "vorher": _stand(auftrag),
        "nachher": None,
        "lieferketten": [],
        "hinweise": [],
        "pruefcode": "",
    }
    if v.zustand != Aktionsvorschlag.OFFEN:
        daten["grund"] = f"{v.nummer} ist {v.get_zustand_display()}."
        return daten
    try:
        _grundpruefung(auftrag)  # dieselben Grundbedingungen wie beim Bestätigen
    except Konflikt as fehler:
        daten["grund"] = str(fehler)
        return daten
    grund = _veraltet_grund(v, auftrag)
    if grund:
        daten["grund"] = grund
        return daten
    daten["lieferketten"] = _ketten_texte(v, auftrag)
    try:
        daten.update(_probelauf(v, auftrag))
    except DienstFehler as fehler:
        daten["grund"] = str(fehler)
        return daten
    daten["gueltig"] = True
    daten["pruefcode"] = _pruefcode(
        v, {k: daten[k] for k in ("vorher", "nachher", "lieferketten", "hinweise")}
    )
    return daten


def offener_vorschlag(auftrag: Auftrag) -> Aktionsvorschlag | None:
    return (
        Aktionsvorschlag.objects.filter(auftrag_id=auftrag.pk, zustand=Aktionsvorschlag.OFFEN)
        .select_related("meldung", "entscheidung")
        .first()
    )


def _art_text(v: Aktionsvorschlag) -> str:
    return v.get_art_display()


def vorschlagen(
    akteur: Akteur,
    auftrag: Auftrag,
    *,
    art: str,
    revision,
    meldung_id,
    text: str,
    entscheidung: Entscheidung | None = None,
    plan: dict | None = None,
) -> Aktionsvorschlag:
    """Planung bereitet eine Aktion vor; am Auftrag ändert sich nichts. Ein offener früherer
    Vorschlag desselben Auftrags wird als veraltet abgelegt. Ein Vorschlag, dessen Ausführung schon
    jetzt scheitern würde (etwa eine Freigabe mit Überschneidung), wird abgewiesen."""
    dienst._planung_erforderlich(akteur, "Aktionen vorschlagen")
    dienst._projekt_sichtbar(akteur, auftrag.projekt)
    if art not in dict(Aktionsvorschlag.ARTEN):
        raise DienstFehler("art muss „wiederaufnahme“ oder „neuschnitt“ sein.")
    if type(revision) is not int:
        raise DienstFehler("revision muss eine ganze Zahl sein.")
    text = (text or "").strip()
    if not text or len(text) > TEXT_LAENGE:
        raise DienstFehler(f"text ist Pflicht (höchstens {TEXT_LAENGE} Zeichen).")
    if art == Aktionsvorschlag.NEUSCHNITT:
        plan = _plan_lesen(plan)
    elif plan:
        raise DienstFehler("Eine Wiederaufnahme ändert keine Planfelder; dafür art=neuschnitt.")
    else:
        plan = {}
    if (
        entscheidung is not None
        and not entscheidung.betroffene_projekte.filter(pk=auftrag.projekt_id).exists()
    ):
        raise KeinZugriff("Nicht gefunden.")
    with transaction.atomic():
        aktuell = _laden(auftrag.pk)
        _grundpruefung(aktuell)
        if aktuell.revision_nr != revision:
            raise Konflikt(
                f"Revision {revision} ist veraltet; aktuell ist Revision {aktuell.revision_nr}."
            )
        if art == Aktionsvorschlag.WIEDERAUFNAHME and aktuell.zustand != Auftrag.BLOCKIERT:
            raise Konflikt(
                f"Wiederaufnahme nur für blockierte Aufträge; {aktuell.nummer} ist "
                f"{aktuell.get_zustand_display()}."
            )
        meldung = None
        if aktuell.zustand == Auftrag.BLOCKIERT:
            massgeblich = lieferungen._blocker(aktuell)
            if (
                massgeblich is None
                or massgeblich.art not in Meldung.BLOCKIERENDE
                or meldung_id != massgeblich.pk
            ):
                raise Konflikt(
                    "meldung_id muss die maßgebliche blockierende Meldung sein"
                    + (f" (aktuell {massgeblich.pk})" if massgeblich is not None else "")
                    + "; vorher kipm_lesen."
                )
            meldung = massgeblich
        elif meldung_id is not None:
            raise Konflikt(f"{aktuell.nummer} ist nicht blockiert; meldung_id weglassen.")
        v = Aktionsvorschlag.objects.create(
            auftrag=aktuell,
            art=art,
            erwartete_revision=revision,
            meldung=meldung,
            entscheidung=entscheidung,
            text=text,
            plan=plan,
            vorgeschlagen_von_name=akteur.name,
            bindung=akteur.bindung,
        )
        grund = _veraltet_grund(v, aktuell)
        if grund:
            raise Konflikt(grund)
        wirkung = auswirkungen(v)
        if not wirkung["gueltig"]:
            raise Konflikt(f"Vorschlag nicht ausführbar: {wirkung['grund']}")
        ersetzt = Aktionsvorschlag.objects.filter(
            auftrag_id=aktuell.pk, zustand=Aktionsvorschlag.OFFEN
        ).exclude(pk=v.pk)
        for alt in ersetzt:
            _ablegen(akteur, alt, aktuell, Aktionsvorschlag.VERALTET, f"ersetzt durch {v.nummer}")
        dienst._ereignis(
            akteur,
            projekt=aktuell.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=aktuell.nummer,
            ereignis=f"Aktion vorgeschlagen: {_art_text(v)} ({v.nummer})",
            anlass="; ".join(
                t
                for t in (
                    f"Meldung {meldung.pk}" if meldung else "",
                    entscheidung.nummer if entscheidung else "",
                    text[:200],
                )
                if t
            ),
            revision=str(revision),
        )
    return v


def _ablegen(akteur: Akteur, v: Aktionsvorschlag, auftrag: Auftrag, zustand: str, grund: str):
    geaendert = Aktionsvorschlag.objects.filter(pk=v.pk, zustand=Aktionsvorschlag.OFFEN).update(
        zustand=zustand, grund=grund, abgeschlossen_am=timezone.now()
    )
    if geaendert != 1:
        return False
    was = "abgelehnt" if zustand == Aktionsvorschlag.ABGELEHNT else "veraltet"
    dienst._ereignis(
        akteur,
        projekt=auftrag.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=auftrag.nummer,
        ereignis=f"Aktionsvorschlag {was} ({v.nummer})",
        anlass=grund,
        revision=str(auftrag.revision_nr),
    )
    return True


def _fred_erforderlich(akteur: Akteur, handlung: str) -> None:
    """Bestätigen und Ablehnen nur durch den angemeldeten Owner selbst, nie über einen Zugang."""
    if akteur.bindung is not None or not akteur.ist_mensch or not akteur.ist_owner:
        raise KeinZugriff(f"{handlung} darf nur Fred selbst in der Oberfläche.")


def _offen_oder_konflikt(v: Aktionsvorschlag) -> Aktionsvorschlag:
    v = Aktionsvorschlag.objects.select_related("auftrag").get(pk=v.pk)
    if v.zustand != Aktionsvorschlag.OFFEN:
        raise Konflikt(f"{v.nummer} ist bereits {v.get_zustand_display()}.")
    return v


def bestaetigen(akteur: Akteur, v: Aktionsvorschlag, *, pruefcode: str) -> Aktionsvorschlag:
    """Freds Bestätigung: prüft Zustand, Revision, Hindernis, Berechtigung und die angezeigten
    Auswirkungen erneut und führt dann in derselben Transaktion aus. Ein veralteter Vorschlag wird
    als veraltet abgelegt; geänderte Auswirkungen oder eine jetzt scheiternde Ausführung lassen ihn
    offen, damit Fred die neue Anzeige sieht."""
    _fred_erforderlich(akteur, "Aktionen bestätigen")
    veraltet = ""
    with transaction.atomic():
        v = _offen_oder_konflikt(v)
        auftrag = _laden(v.auftrag_id)
        _grundpruefung(auftrag)
        veraltet = _veraltet_grund(v, auftrag)
        if veraltet:
            _ablegen(akteur, v, auftrag, Aktionsvorschlag.VERALTET, veraltet)
        else:
            wirkung = auswirkungen(v)
            if not wirkung["gueltig"]:
                raise Konflikt(f"{v.nummer} ist so nicht ausführbar: {wirkung['grund']}")
            if wirkung["pruefcode"] != (pruefcode or ""):
                raise Konflikt(
                    "Die Auswirkungen haben sich seit der Anzeige geändert. Bitte neu ansehen und "
                    "dann bestätigen; es wurde nichts ausgeführt."
                )
            jetzt = timezone.now()
            geaendert = Aktionsvorschlag.objects.filter(
                pk=v.pk, zustand=Aktionsvorschlag.OFFEN
            ).update(bestaetigt_von=akteur.benutzer, bestaetigt_am=jetzt)
            if geaendert != 1:
                raise Konflikt(f"{v.nummer} wurde gerade anders entschieden.")
            dienst._ereignis(
                akteur,
                projekt=auftrag.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=auftrag.nummer,
                ereignis=f"Aktion bestätigt ({v.nummer})",
                anlass=f"{_art_text(v)}: "
                + "; ".join(wirkung["hinweise"] + wirkung["lieferketten"]),
                revision=str(auftrag.revision_nr),
            )
            _ausfuehren(akteur, v, auftrag)
            nachher = _laden(auftrag.pk)
            Aktionsvorschlag.objects.filter(pk=v.pk).update(
                zustand=Aktionsvorschlag.AUSGEFUEHRT,
                ausgefuehrt_am=timezone.now(),
                abgeschlossen_am=timezone.now(),
                ergebnis_revision=nachher.revision_nr,
            )
            dienst._ereignis(
                akteur,
                projekt=auftrag.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=auftrag.nummer,
                ereignis=f"Aktion ausgeführt ({v.nummer})",
                anlass=f"{nachher.get_zustand_display()}, Bearbeiter {_name(nachher)}, "
                f"Revision {nachher.revision_nr}",
                revision=str(nachher.revision_nr),
            )
    if veraltet:
        raise Konflikt(f"{v.nummer} ist veraltet und wurde nicht ausgeführt: {veraltet}")
    v.refresh_from_db()
    return v


def ablehnen(akteur: Akteur, v: Aktionsvorschlag, grund: str = "") -> Aktionsvorschlag:
    _fred_erforderlich(akteur, "Aktionen ablehnen")
    with transaction.atomic():
        v = _offen_oder_konflikt(v)
        auftrag = _laden(v.auftrag_id)
        if not _ablegen(akteur, v, auftrag, Aktionsvorschlag.ABGELEHNT, (grund or "").strip()):
            raise Konflikt(f"{v.nummer} wurde gerade anders entschieden.")
    v.refresh_from_db()
    return v


def vorschlag_json(v: Aktionsvorschlag, *, mit_auswirkungen: bool = False) -> dict:
    daten = {
        "nummer": v.nummer,
        "art": v.art,
        "zustand": v.zustand,
        "revision": v.erwartete_revision,
        "meldung_id": v.meldung_id,
        "entscheidung": v.entscheidung.nummer if v.entscheidung_id else None,
        "text": v.text,
        "plan": v.plan,
        "von": v.vorgeschlagen_von_name,
        "vorgeschlagen_am": v.vorgeschlagen_am.isoformat(timespec="minutes"),
        "bestaetigt_am": v.bestaetigt_am.isoformat(timespec="minutes") if v.bestaetigt_am else None,
        "ausgefuehrt_am": (
            v.ausgefuehrt_am.isoformat(timespec="minutes") if v.ausgefuehrt_am else None
        ),
        "ergebnis_revision": v.ergebnis_revision,
        "grund": v.grund,
        "vollmacht": f"VM-{v.vollmacht_id}" if v.vollmacht_id else None,
        "nachweis": v.nachweis or None,
        "bearbeiter_danach": bearbeiter_danach(v),
    }
    if v.zustand == Aktionsvorschlag.OFFEN:
        daten["veraltet_grund"] = _veraltet_grund(v, _laden(v.auftrag_id))
    if mit_auswirkungen:
        daten["auswirkungen"] = auswirkungen(v)
    return daten


# --- Vollmacht: technische Standardfälle ohne erneute Einzelbestätigung ----------------------

VOLLMACHT_ARTEN = (Aktionsvorschlag.WIEDERAUFNAHME,)
VOLLMACHT_MELDUNGSARTEN = (Meldung.FEHLER, Meldung.HINDERNIS, Meldung.RUECKFRAGE)
VOLLMACHT_HOECHSTENS_TAGE = 90
NICHT_AUSGEFUEHRT = "Aktion mit Vollmacht nicht ausgeführt"


def _vollmacht_ereignis(akteur, vm: Aktionsvollmacht, ereignis: str, anlass: str) -> None:
    dienst._ereignis(
        akteur,
        projekt=vm.projekt,
        objekt_typ="Vollmacht",
        objekt_kennung=vm.nummer,
        ereignis=ereignis,
        anlass=anlass,
    )


def vollmacht_erteilen(
    akteur: Akteur,
    projekt,
    *,
    aktion: str,
    ausfuehrende_bindung: ChatBindung,
    bearbeiter_bindung: ChatBindung,
    meldungsarten,
    gueltig_bis,
    auftrag: Auftrag | None = None,
    entscheidung: Entscheidung | None = None,
    lieferung: Lieferung | None = None,
) -> Aktionsvollmacht:
    """Nur Fred selbst in der Oberfläche. Die Vollmacht nennt Projekt, Aktion, ausführenden
    Zugang (PC-Zugang mit Planungsrecht), Bearbeiterzugang, erlaubte Blockadearten, Frist und
    optional einen einzelnen Auftrag sowie die Entscheidung als Bezug."""
    _fred_erforderlich(akteur, "Vollmachten erteilen")
    if aktion not in VOLLMACHT_ARTEN:
        raise DienstFehler("Eine Vollmacht gibt es bisher nur für die Wiederaufnahme.")
    if ausfuehrende_bindung.art != ChatBindung.ART_PC or not dienst._chatbindung_im_projekt_gilt(
        ausfuehrende_bindung, projekt
    ):
        raise DienstFehler("Ausführen darf nur ein aktiver PC-Zugang (er hat das Planungsrecht).")
    if not dienst._chatbindung_im_projekt_gilt(bearbeiter_bindung, projekt):
        raise DienstFehler("Der Bearbeiterzugang gilt für dieses Projekt nicht.")
    arten = sorted(set(meldungsarten or []))
    if not arten or set(arten) - set(VOLLMACHT_MELDUNGSARTEN):
        raise DienstFehler("Mindestens eine Blockadeart: Fehler, Hindernis oder Rückfrage.")
    jetzt = timezone.now()
    if gueltig_bis is None or gueltig_bis <= jetzt:
        raise DienstFehler("Gültig bis muss in der Zukunft liegen.")
    if gueltig_bis > jetzt + timedelta(days=VOLLMACHT_HOECHSTENS_TAGE):
        raise DienstFehler(f"Eine Vollmacht gilt höchstens {VOLLMACHT_HOECHSTENS_TAGE} Tage.")
    if auftrag is not None and auftrag.projekt_id != projekt.pk:
        raise KeinZugriff("Nicht gefunden.")
    if lieferung is not None and lieferung.auftrag.projekt_id != projekt.pk:
        raise KeinZugriff("Nicht gefunden.")
    if (
        entscheidung is not None
        and not entscheidung.betroffene_projekte.filter(pk=projekt.pk).exists()
    ):
        raise KeinZugriff("Nicht gefunden.")
    with transaction.atomic():
        vm = Aktionsvollmacht.objects.create(
            projekt=projekt,
            aktion=aktion,
            ausfuehrende_bindung=ausfuehrende_bindung,
            bearbeiter_bindung=bearbeiter_bindung,
            meldungsarten=arten,
            auftrag=auftrag,
            entscheidung=entscheidung,
            lieferung=lieferung,
            gueltig_bis=gueltig_bis,
            erteilt_von=akteur.benutzer,
        )
        _vollmacht_ereignis(
            akteur,
            vm,
            "Vollmacht erteilt",
            f"{vm.get_aktion_display()} durch {ausfuehrende_bindung.name} bei Bearbeiter "
            f"{bearbeiter_bindung.name}; Blockaden {', '.join(arten)}; bis "
            f"{timezone.localtime(gueltig_bis):%d.%m.%Y %H:%M}"
            + (f"; nur {auftrag.nummer}" if auftrag else "")
            + (f"; nur mit Nachweis L-{lieferung.pk}" if lieferung else "")
            + (f"; Bezug {entscheidung.nummer}" if entscheidung else ""),
        )
    return vm


def vollmacht_widerrufen(akteur: Akteur, vm: Aktionsvollmacht) -> Aktionsvollmacht:
    _fred_erforderlich(akteur, "Vollmachten widerrufen")
    with transaction.atomic():
        geaendert = Aktionsvollmacht.objects.filter(pk=vm.pk, widerrufen_am__isnull=True).update(
            widerrufen_am=timezone.now()
        )
        if geaendert == 1:
            _vollmacht_ereignis(akteur, vm, "Vollmacht widerrufen", vm.get_aktion_display())
    vm.refresh_from_db()
    return vm


def vollmacht_json(vm: Aktionsvollmacht) -> dict:
    return {
        "nummer": vm.nummer,
        "aktion": vm.aktion,
        "ausfuehrender_zugang": vm.ausfuehrende_bindung.name,
        "bearbeiter": vm.bearbeiter_bindung.name,
        "meldungsarten": vm.meldungsarten,
        "auftrag": vm.auftrag.nummer if vm.auftrag_id else None,
        "entscheidung": vm.entscheidung.nummer if vm.entscheidung_id else None,
        "lieferung": f"L-{vm.lieferung_id}" if vm.lieferung_id else None,
        "gueltig_bis": vm.gueltig_bis.isoformat(timespec="minutes"),
    }


def _vollmacht_laden(akteur: Akteur, projekt, nummer) -> Aktionsvollmacht:
    roh = str(nummer or "").strip().upper().removeprefix("VM-")
    vm = None
    if roh.isdigit():
        vm = (
            Aktionsvollmacht.objects.select_related(
                "projekt", "ausfuehrende_bindung", "bearbeiter_bindung", "auftrag__projekt"
            )
            .filter(pk=int(roh), projekt=projekt)
            .first()
        )
    if vm is None:
        raise KeinZugriff("Nicht gefunden.")
    if akteur.bindung is None or akteur.bindung.pk != vm.ausfuehrende_bindung_id:
        raise KeinZugriff(f"{vm.nummer} gilt nicht für diesen Zugang.")
    return vm


NACHWEIS_BEISPIEL = '{"auftrag": "NEX-69", "revision": 5, "lieferung": "L-1"}'


def _nachweis_lesen(nachweis) -> dict:
    if not isinstance(nachweis, dict) or not {"auftrag", "revision", "lieferung"} <= set(nachweis):
        raise DienstFehler(
            "Nachweis fehlt: auftrag, revision und lieferung der belegten Korrektur, z. B. "
            + NACHWEIS_BEISPIEL
        )
    roh = str(nachweis["lieferung"]).strip().upper().removeprefix("L-")
    if type(nachweis["revision"]) is not int or not roh.isdigit():
        raise DienstFehler("Nachweis: revision ist eine Zahl, lieferung eine Liefernummer (L-n).")
    return {
        "auftrag": str(nachweis["auftrag"]).strip(),
        "revision": nachweis["revision"],
        "lieferung": int(roh),
    }


def _nachweis_pruefen(projekt, n: dict) -> dict:
    """Der Nachweis ist eine tatsächlich gemeldete, belegte Lieferung im selben Projekt."""
    lieferung = (
        Lieferung.objects.select_related("auftrag__projekt")
        .filter(pk=n["lieferung"], auftrag__projekt=projekt)
        .first()
    )
    if lieferung is None:
        raise Konflikt(f"Lieferung L-{n['lieferung']} gibt es in diesem Projekt nicht.")
    try:
        auftrag_id = dienst.auftrag_id_aus_nummer(n["auftrag"])
    except DienstFehler as fehler:
        raise Konflikt(f"Nachweis: {fehler}") from fehler
    if lieferung.auftrag_id != auftrag_id:
        raise Konflikt(
            f"Lieferung L-{lieferung.pk} gehört zu {lieferung.auftrag.nummer}, nicht zu "
            f"{n['auftrag']}."
        )
    if lieferung.revision_nr != n["revision"]:
        raise Konflikt(
            f"Der Nachweis nennt Revision {n['revision']}; Lieferung L-{lieferung.pk} gehört zu "
            f"Revision {lieferung.revision_nr} von {lieferung.auftrag.nummer}."
        )
    return {
        "auftrag": lieferung.auftrag.nummer,
        "revision": lieferung.revision_nr,
        "lieferung": lieferung.pk,
    }


def _vollmacht_grund(vm: Aktionsvollmacht, auftrag: Auftrag, aktion, revision, meldung_id) -> str:
    """Leer, wenn die Vollmacht diesen Fall jetzt deckt; sonst der konkrete Wartegrund."""
    if vm.widerrufen_am is not None:
        return f"{vm.nummer} ist widerrufen."
    if vm.gueltig_bis <= timezone.now():
        return f"{vm.nummer} ist abgelaufen."
    if aktion != vm.aktion:
        return f"{vm.nummer} deckt die Aktion „{vm.get_aktion_display()}“, nicht „{aktion}“."
    if vm.auftrag_id is not None and vm.auftrag_id != auftrag.pk:
        return f"{vm.nummer} gilt nur für {vm.auftrag.nummer}."
    if auftrag.revision_nr != revision:
        return f"Revision {revision} ist veraltet; aktuell ist Revision {auftrag.revision_nr}."
    if auftrag.zustand != Auftrag.BLOCKIERT:
        return f"{auftrag.nummer} ist {auftrag.get_zustand_display()}, nicht blockiert."
    massgeblich = lieferungen._blocker(auftrag)
    if massgeblich is None or massgeblich.pk != meldung_id:
        return (
            f"Meldung {meldung_id} ist nicht die maßgebliche Blockademeldung"
            + (f" (aktuell {massgeblich.pk})" if massgeblich is not None else "")
            + "; vorher kipm_lesen."
        )
    if massgeblich.lieferbedarf.exists():
        return (
            "Die Blockade ist ein technischer Lieferbedarf; sie läuft über die Lieferautomatik "
            "mit ihren exakten Zuordnungen, nicht über eine Vollmacht."
        )
    if massgeblich.art not in vm.meldungsarten:
        return (
            f"Die maßgebliche Meldung ist eine {massgeblich.get_art_display()}; {vm.nummer} "
            f"deckt nur {', '.join(vm.meldungsarten)}."
        )
    if auftrag.bearbeiter_bindung_id != vm.bearbeiter_bindung_id:
        return (
            f"Bearbeiter ist {auftrag.bearbeiter_name or 'niemand'}; {vm.nummer} gilt für "
            f"{vm.bearbeiter_bindung.name}."
        )
    if not vm.bearbeiter_bindung.aktiv or auftrag.uebernommene_revision != auftrag.revision_nr:
        return "Der Bearbeiter hat keine gültige Übernahme dieser Revision."
    return ""


def _schon_ausgefuehrt(vm, auftrag, meldung_id):
    return (
        Aktionsvorschlag.objects.filter(
            vollmacht=vm,
            auftrag_id=auftrag.pk,
            meldung_id=meldung_id,
            zustand=Aktionsvorschlag.AUSGEFUEHRT,
        )
        .select_related("auftrag__projekt", "entscheidung")
        .first()
    )


def _wartegrund_vermerken(akteur, auftrag: Auftrag, vm: Aktionsvollmacht, grund: str) -> None:
    """Abgewiesene Ausführung bleibt im Verlauf sichtbar; derselbe Grund nur einmal
    hintereinander."""
    name = f"{NICHT_AUSGEFUEHRT} ({vm.nummer})"
    with transaction.atomic():
        letzter = (
            Ereignis.objects.filter(objekt_typ="Auftrag", objekt_kennung=auftrag.nummer)
            .order_by("-id")
            .first()
        )
        if letzter is not None and letzter.ereignis == name and letzter.anlass == grund:
            return
        dienst._ereignis(
            akteur,
            projekt=auftrag.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=auftrag.nummer,
            ereignis=name,
            anlass=grund,
            revision=str(auftrag.revision_nr),
        )


def wartegrund(auftrag: Auftrag) -> str | None:
    """Letzter Grund, aus dem eine Ausführung mit Vollmacht abgewiesen wurde, solange danach
    keine Aktion ausgeführt wurde."""
    if auftrag.zustand != Auftrag.BLOCKIERT:
        return None
    ereignisse = Ereignis.objects.filter(objekt_typ="Auftrag", objekt_kennung=auftrag.nummer)
    abgewiesen = ereignisse.filter(ereignis__startswith=NICHT_AUSGEFUEHRT).order_by("-id").first()
    if abgewiesen is None:
        return None
    massgeblich = lieferungen._blocker(auftrag)
    if massgeblich is not None and abgewiesen.zeitpunkt < massgeblich.zeitpunkt:
        return None  # gehört zu einer früheren Blockade
    danach = ereignisse.filter(ereignis__startswith="Aktion ausgeführt", id__gt=abgewiesen.pk)
    return None if danach.exists() else abgewiesen.anlass


def mit_vollmacht_ausfuehren(
    akteur: Akteur,
    auftrag: Auftrag,
    *,
    vollmacht,
    aktion: str,
    revision,
    meldung_id,
    nachweis,
    text: str,
) -> tuple[Aktionsvorschlag, bool]:
    """Ausführung durch den in der Vollmacht genannten Zugang. Prüft in einer Transaktion
    Vollmacht, Revision, maßgebliche Blockademeldung, Bearbeiter und Nachweis, danach über die
    bestehende Auftragslogik Ordnersperre und Festfahren. Je Blockademeldung wirkt sie höchstens
    einmal; eine Wiederholung liefert die frühere Ausführung (zweiter Wert True). Abweisungen nach
    geprüfter Vollmacht bleiben als Wartegrund im Verlauf."""
    dienst._projekt_sichtbar(akteur, auftrag.projekt)
    vm = _vollmacht_laden(akteur, auftrag.projekt, vollmacht)
    if type(revision) is not int or type(meldung_id) is not int:
        raise DienstFehler("revision und meldung_id müssen ganze Zahlen sein.")
    text = (text or "").strip()
    if not text or len(text) > TEXT_LAENGE:
        raise DienstFehler(f"text ist Pflicht (höchstens {TEXT_LAENGE} Zeichen).")
    n = _nachweis_lesen(nachweis)
    try:
        with transaction.atomic():
            vorhanden = _schon_ausgefuehrt(vm, auftrag, meldung_id)
            if vorhanden is not None:
                return vorhanden, True
            # Unter der Sperre neu lesen: ein Widerruf kurz vorher gilt.
            vm = Aktionsvollmacht.objects.select_related(
                "projekt", "ausfuehrende_bindung", "bearbeiter_bindung", "auftrag__projekt"
            ).get(pk=vm.pk)
            aktuell = _laden(auftrag.pk)
            _grundpruefung(aktuell)
            grund = _vollmacht_grund(vm, aktuell, aktion, revision, meldung_id)
            if grund:
                raise Konflikt(grund)
            belegt = _nachweis_pruefen(aktuell.projekt, n)
            if vm.lieferung_id is not None and belegt["lieferung"] != vm.lieferung_id:
                raise Konflikt(f"{vm.nummer} gilt nur mit Nachweis L-{vm.lieferung_id}.")
            frueher = Aktionsvorschlag.objects.filter(
                auftrag_id=aktuell.pk,
                vollmacht__isnull=False,
                zustand=Aktionsvorschlag.AUSGEFUEHRT,
                nachweis__lieferung=belegt["lieferung"],
            ).first()
            if frueher is not None:
                raise Konflikt(
                    f"Lieferung L-{belegt['lieferung']} hat {aktuell.nummer} schon einmal "
                    f"fortgesetzt ({frueher.nummer}); eine weitere Wiederaufnahme braucht eine "
                    "neue belegte Korrektur."
                )
            v = Aktionsvorschlag.objects.create(
                auftrag=aktuell,
                art=vm.aktion,
                erwartete_revision=revision,
                meldung_id=meldung_id,
                entscheidung=vm.entscheidung,
                text=text,
                vorgeschlagen_von_name=akteur.name,
                bindung=akteur.bindung,
                vollmacht=vm,
                nachweis=belegt,
            )
            _ausfuehren(akteur, v, aktuell)
            nachher = _laden(aktuell.pk)
            jetzt = timezone.now()
            Aktionsvorschlag.objects.filter(pk=v.pk).update(
                zustand=Aktionsvorschlag.AUSGEFUEHRT,
                ausgefuehrt_am=jetzt,
                abgeschlossen_am=jetzt,
                ergebnis_revision=nachher.revision_nr,
            )
            dienst._ereignis(
                akteur,
                projekt=aktuell.projekt,
                objekt_typ="Auftrag",
                objekt_kennung=aktuell.nummer,
                ereignis=f"Aktion ausgeführt ({v.nummer}) mit Vollmacht {vm.nummer}",
                anlass=f"{nachher.get_zustand_display()}, Bearbeiter {_name(nachher)}, Revision "
                f"{nachher.revision_nr}; Meldung {meldung_id}; Nachweis {belegt['auftrag']} "
                f"r{belegt['revision']} L-{belegt['lieferung']}",
                revision=str(nachher.revision_nr),
            )
    except IntegrityError:
        vorhanden = _schon_ausgefuehrt(vm, auftrag, meldung_id)
        if vorhanden is not None:
            return vorhanden, True
        raise
    except KeinZugriff:
        raise
    except DienstFehler as fehler:
        _wartegrund_vermerken(akteur, auftrag, vm, str(fehler))
        raise
    v.refresh_from_db()
    return v, False


def bearbeiter_danach(v: Aktionsvorschlag) -> dict | None:
    """Erste fachliche Meldung oder erstes Ergebnis des bisherigen Bearbeiters nach der
    Ausführung; zeigt, ob die Wiederaufnahme tatsächlich weitergeführt wurde."""
    if v.ausgefuehrt_am is None or v.meldung_id is None:
        return None
    bindung_id = (
        Meldung.objects.filter(pk=v.meldung_id).values_list("bindung_id", flat=True).first()
    )
    if bindung_id is None:
        return None
    meldung = (
        Meldung.objects.filter(
            auftrag_id=v.auftrag_id, bindung_id=bindung_id, zeitpunkt__gt=v.ausgefuehrt_am
        )
        .exclude(art__in=(Meldung.ANTWORT, Meldung.SITZUNGSENDE))
        .order_by("zeitpunkt", "id")
        .first()
    )
    ergebnis = (
        v.auftrag.ergebnisse.filter(bindung_id=bindung_id, eingereicht_am__gt=v.ausgefuehrt_am)
        .order_by("eingereicht_am", "id")
        .first()
    )
    if ergebnis is not None and (meldung is None or ergebnis.eingereicht_am <= meldung.zeitpunkt):
        return {
            "art": "ERGEBNIS",
            "zeit": ergebnis.eingereicht_am.isoformat(timespec="minutes"),
            "text": ergebnis.zusammenfassung[:300],
        }
    if meldung is None:
        return None
    return {
        "art": meldung.art,
        "zeit": meldung.zeitpunkt.isoformat(timespec="minutes"),
        "text": meldung.text[:300],
    }


def aktionen_lesen(akteur: Akteur, projekt) -> dict:
    """Lese- und Aktionsweg der Vorratsautomation: gültige Vollmachten dieses Zugangs und die
    letzten Aktionen im Projekt (vorbereitet, ausgeführt, abgelehnt, veraltet) samt Folgemeldung
    des bisherigen Bearbeiters."""
    dienst._projekt_sichtbar(akteur, projekt)
    vollmachten = Aktionsvollmacht.objects.filter(
        projekt=projekt, widerrufen_am__isnull=True, gueltig_bis__gt=timezone.now()
    ).select_related(
        "ausfuehrende_bindung", "bearbeiter_bindung", "auftrag__projekt", "entscheidung"
    )
    if akteur.bindung is not None:
        vollmachten = vollmachten.filter(ausfuehrende_bindung=akteur.bindung)
    aktionen = Aktionsvorschlag.objects.filter(auftrag__projekt=projekt).select_related(
        "auftrag__projekt", "entscheidung"
    )[:50]
    return {
        "vollmachten": [vollmacht_json(vm) for vm in vollmachten],
        "aktionen": [{"auftrag": v.auftrag.nummer, **vorschlag_json(v)} for v in aktionen],
    }

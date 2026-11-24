"""Stufenregel für die Planung (KIPM-31, ``01-Planung/AKTIONSVOLLMACHT.md``).

Fred speichert je Projekt und PC-Zugang eine befristete, widerrufbare Regel. Sie trägt Änderungen
der Planung ohne ``fred_sagte``; der Verlauf nennt dann die Regel statt Freds Worten, niemand
spricht in seinem Namen.

- Stufe A: einen fertig geplanten Auftrag freigeben, eine fehlende Lieferkante nachtragen.
- Stufe B: eine technische Entscheidung buchen, nur mit wörtlicher Fundstelle, die KI-PM gegen
  Stand und Hash prüft; den davon abhängigen blockierten Auftrag beim bisherigen Bearbeiter
  fortsetzen, je Blockademeldung höchstens einmal.
- Stufe C (Ziel, Kosten, Rechte, Cloud/Microsoft, Sicherheit, Daten, Neuschnitt,
  Bearbeiterwechsel, Löschung) und alles ohne prüfbare Fundstelle bleibt bei Fred.

Die Regel ersetzt die nicht übernommene Klärungsregel des Blockadenabgleichs (Zweig
``sicherung/blockadenabgleich-20261124``); deren Prüfung der Fundstelle ist hier aufgegangen."""

import dataclasses
import hashlib
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import akte, dienst, lieferungen
from .auftragsaktionen import VOLLMACHT_HOECHSTENS_TAGE
from .models import (
    Auftrag,
    ChatBindung,
    Entscheidung,
    Meldung,
    Planungsentscheid,
    Projekt,
    Stufenregel,
    Unterlage,
)

TECHNIK = "technik"
# Bereiche der Stufe C: nur Fred. Wer einen davon nennt, wird mit Grund abgewiesen.
STUFE_C = (
    "ziel",
    "kosten",
    "rechte",
    "cloud",
    "microsoft",
    "sicherheit",
    "daten",
    "neuschnitt",
    "bearbeiterwechsel",
    "loeschung",
)
# Wortprüfung über Entscheidung und Begründung: Ein Treffer schickt den Fall zu Fred. Sie ist
# bewusst grob; ein Fehlalarm kostet nur eine Frage an Fred.
_STUFE_C_WORTE = re.compile(
    r"(ziel|daten|kosten|preis|lizenz|budget|rechnung|\brecht(e|en)?\b|berechtigung|"
    r"zugriffsrecht|passwort|geheimnis|token|microsoft|azure|entra|sharepoint|onedrive|"
    r"\bcloud|sicherheit|datenschutz|personenbezogen|dsgvo|lösch|loesch|neuschnitt|"
    r"bearbeiter)",
    re.IGNORECASE,
)
_SHA = re.compile(r"[0-9a-f]{64}")
MINDESTLAENGE_FUNDSTELLE = 20
KEINE_REGEL = (
    "Für diesen Zugang gibt es in {projekt} keine gültige Stufenregel mit Stufe {stufe}; "
    "frag Fred (fred_sagte) oder bitte ihn, die Stufenregel in den Projekteinstellungen zu "
    "speichern. Es wurde nichts geändert."
)


def _normal(text: str) -> str:
    return " ".join(text.split())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- Regel verwalten (nur Fred in der Oberfläche) --------------------------------------------


def _nur_fred(akteur, handlung):
    dienst._owner_erforderlich(akteur, handlung)
    if akteur.bindung is not None:
        raise dienst.KeinZugriff(f"{handlung} darf nur Fred in der Oberfläche.")


def erteilen(
    akteur,
    projekt: Projekt,
    *,
    zugang: ChatBindung,
    stufe_a: bool,
    stufe_b: bool,
    gueltig_bis,
    masterspec_bezeichnung: str = "",
    masterspec_sha256: str = "",
) -> Stufenregel:
    """Speichert die Regel; eine ältere gültige Regel desselben Zugangs gilt danach nicht mehr."""
    _nur_fred(akteur, "Stufenregeln erteilen")
    if not projekt.aktiv:
        raise dienst.Konflikt("Das Projekt ist archiviert.")
    if zugang.art != ChatBindung.ART_PC or not dienst._chatbindung_im_projekt_gilt(zugang, projekt):
        raise dienst.DienstFehler("Die Stufenregel gilt nur für einen gültigen PC-Zugang.")
    if not (stufe_a or stufe_b):
        raise dienst.DienstFehler("Mindestens Stufe A oder Stufe B wählen.")
    jetzt = timezone.now()
    if not jetzt < gueltig_bis <= jetzt + timedelta(days=VOLLMACHT_HOECHSTENS_TAGE):
        raise dienst.DienstFehler(
            f"Die Regel muss innerhalb der nächsten {VOLLMACHT_HOECHSTENS_TAGE} Tage ablaufen."
        )
    sha = (masterspec_sha256 or "").strip().lower()
    bezeichnung = (masterspec_bezeichnung or "").strip()
    if sha and not _SHA.fullmatch(sha):
        raise dienst.DienstFehler("Der MasterSpec-Hash muss SHA-256 mit 64 Hexzeichen sein.")
    if bool(sha) != bool(bezeichnung):
        raise dienst.DienstFehler("MasterSpec: Bezeichnung und Hash nur zusammen angeben.")
    with transaction.atomic():
        for alt in _gueltige(projekt).filter(zugang=zugang):
            Stufenregel.objects.filter(pk=alt.pk).update(widerrufen_am=jetzt)
            dienst._ereignis(
                akteur,
                projekt=projekt,
                objekt_typ="Stufenregel",
                objekt_kennung=alt.nummer,
                ereignis="Stufenregel ersetzt",
            )
        regel = Stufenregel.objects.create(
            projekt=projekt,
            zugang=zugang,
            stufe_a=stufe_a,
            stufe_b=stufe_b,
            masterspec_bezeichnung=bezeichnung,
            masterspec_sha256=sha,
            gueltig_bis=gueltig_bis,
            erteilt_von=akteur.benutzer,
        )
        dienst._ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="Stufenregel",
            objekt_kennung=regel.nummer,
            ereignis="Stufenregel erteilt",
            anlass=f"{zugang.name} · {_stufen_text(regel)}",
        )
    return regel


def widerrufen(akteur, regel: Stufenregel) -> None:
    _nur_fred(akteur, "Stufenregeln widerrufen")
    with transaction.atomic():
        if Stufenregel.objects.filter(pk=regel.pk, widerrufen_am__isnull=True).update(
            widerrufen_am=timezone.now()
        ):
            dienst._ereignis(
                akteur,
                projekt=regel.projekt,
                objekt_typ="Stufenregel",
                objekt_kennung=regel.nummer,
                ereignis="Stufenregel widerrufen",
            )


def _stufen_text(regel: Stufenregel) -> str:
    teile = []
    if regel.stufe_a:
        teile.append("Stufe A")
    if regel.stufe_b:
        teile.append("Stufe B")
    if regel.masterspec_sha256:
        teile.append(f"MasterSpec {regel.masterspec_bezeichnung}")
    return ", ".join(teile)


def _gueltige(projekt: Projekt):
    return projekt.stufenregeln.filter(
        widerrufen_am__isnull=True, gueltig_bis__gt=timezone.now()
    ).select_related("zugang")


def regel_json(regel: Stufenregel) -> dict:
    return {
        "nummer": regel.nummer,
        "zugang": regel.zugang.name,
        "stufe_a": regel.stufe_a,
        "stufe_b": regel.stufe_b,
        "masterspec": (
            {"bezeichnung": regel.masterspec_bezeichnung, "sha256": regel.masterspec_sha256}
            if regel.masterspec_sha256
            else None
        ),
        "gueltig_bis": regel.gueltig_bis.isoformat(timespec="minutes"),
    }


def lesen(akteur, projekt: Projekt) -> list[dict]:
    """Gültige Regeln des Projekts für kipm_aktionen; PC-Zugänge sehen alle."""
    regeln = _gueltige(projekt)
    if akteur.bindung is not None and not akteur.ist_assistent:
        regeln = regeln.filter(zugang=akteur.bindung)
    return [regel_json(r) for r in regeln]


def _regel(akteur, projekt: Projekt, stufe: str) -> Stufenregel:
    """Die gültige Regel des aufrufenden PC-Zugangs mit dieser Stufe, sonst Ablehnung."""
    dienst._projekt_sichtbar(akteur, projekt)
    bindung = akteur.bindung
    regel = (
        _gueltige(projekt).filter(zugang=bindung).last()
        if bindung is not None and akteur.ist_assistent and projekt.aktiv
        else None
    )
    if regel is None or not dienst._chatbindung_im_projekt_gilt(bindung, projekt):
        raise dienst.KeinZugriff(KEINE_REGEL.format(projekt=projekt.kennung, stufe=stufe))
    if not (regel.stufe_a if stufe == "A" else regel.stufe_b):
        raise dienst.KeinZugriff(KEINE_REGEL.format(projekt=projekt.kennung, stufe=stufe))
    return regel


def _mit_regel(akteur, regel: Stufenregel):
    return dataclasses.replace(akteur, regel=regel.nummer, fred_sagte="")


# --- Stufe A ----------------------------------------------------------------------------------


def _auftrag(akteur, projekt, nummer, revision) -> Auftrag:
    a = dienst.auftrag_holen(akteur, dienst.auftrag_id_aus_nummer(str(nummer or "")))
    if a.projekt_id != projekt.pk:
        raise dienst.KeinZugriff("Nicht gefunden.")
    if type(revision) is not int or revision != a.revision_nr:
        raise dienst.Konflikt(f"{a.nummer} hat Revision {a.revision_nr}; neu lesen.")
    return a


def freigeben(akteur, projekt: Projekt, *, auftrag, revision) -> Auftrag:
    """Stufe A: einen fertig geplanten Entwurf freigeben; die Freigabeprüfung gilt unverändert."""
    with transaction.atomic():  # Regel, Revision und Freigabe in einer Schreibtransaktion
        regel = _regel(akteur, projekt, "A")
        a = _auftrag(akteur, projekt, auftrag, revision)
        dienst.auftrag_freigeben(_mit_regel(akteur, regel), a)
    a.refresh_from_db()
    return a


def lieferkante(
    akteur, projekt: Projekt, *, auftrag, revision, bedarf, nur_lieferungen, hindernis_id
):
    """Stufe A: eine fehlende technische Lieferkante nachtragen, wie kipm_lieferbedarf_festlegen.
    Nur ergänzen: Eine schon geplante oder registrierte Kante ändern, ersetzen oder aufheben darf
    nur Fred (fred_sagte), denn das wäre eine Löschung."""
    with transaction.atomic():
        regel = _regel(akteur, projekt, "A")
        a = _auftrag(akteur, projekt, auftrag, revision)
        if not isinstance(bedarf, list) or not bedarf:
            raise dienst.KeinZugriff(
                "Stufe A trägt nur fehlende Kanten nach; einen Lieferbedarf aufheben darf nur Fred."
            )
        if a.zustand == Auftrag.ENTWURF and hindernis_id is None:
            # Ohne Projektfilter: auch eine Kante auf einen inzwischen verschobenen Anbieter zählt.
            vorhanden = a.meldungen.filter(
                revision_nr=a.revision_nr, art=Meldung.LIEFERPLAN, historisch=False
            ).exists()
        else:
            blocker = lieferungen._blocker(a)
            vorhanden = blocker is not None and blocker.lieferbedarf.exists()
        if vorhanden:
            raise dienst.KeinZugriff(
                f"{a.nummer} hat schon einen Lieferbedarf; ändern oder ersetzen darf ihn nur Fred."
            )
        lieferungen.festlegen(
            _mit_regel(akteur, regel),
            a,
            revision,
            bedarf=bedarf,
            nur_lieferungen=nur_lieferungen,
            hindernis_id=hindernis_id,
        )
    a.refresh_from_db()
    return a


# --- Stufe B ----------------------------------------------------------------------------------


def _stufe_c(bereich, texte) -> None:
    bereich = str(bereich or "").strip().lower()
    if bereich in STUFE_C:
        raise dienst.KeinZugriff(f"Stufe C ({bereich}): das entscheidet nur Fred. Nichts gebucht.")
    if bereich != TECHNIK:
        raise dienst.DienstFehler(
            f"bereich muss „{TECHNIK}“ sein; Stufe C ({', '.join(STUFE_C)}) entscheidet Fred."
        )
    for text in texte:
        treffer = _STUFE_C_WORTE.search(text or "")
        if treffer:
            raise dienst.KeinZugriff(
                f"Stufe C: „{treffer.group(0)}“ betrifft Ziel, Kosten, Rechte, Cloud/Microsoft, "
                "Sicherheit, Daten oder Löschung; das entscheidet nur Fred. Nichts gebucht."
            )


def _fundstelle(akteur, projekt: Projekt, regel: Stufenregel, f) -> dict:
    """Prüft die Fundstelle gegen Stand und Hash; Rückgabe ist der gespeicherte Beleg."""
    if not isinstance(f, dict):
        raise dienst.DienstFehler(
            "fundstelle fehlt: quelle (E-n, U-n oder masterspec), stand, sha256 und der "
            "wörtliche text. Ohne prüfbare Fundstelle entscheidet Fred."
        )
    quelle = str(f.get("quelle", "")).strip()
    text = f.get("text")
    sha = str(f.get("sha256", "")).strip().lower()
    if not isinstance(text, str) or len(_normal(text)) < MINDESTLAENGE_FUNDSTELLE:
        raise dienst.DienstFehler(
            f"Die Fundstelle braucht den wörtlichen Text (mindestens "
            f"{MINDESTLAENGE_FUNDSTELLE} Zeichen)."
        )
    if not _SHA.fullmatch(sha):
        raise dienst.DienstFehler("sha256 der Quelle fehlt (64 Hexzeichen).")
    if quelle == "masterspec":
        if not regel.masterspec_sha256:
            raise dienst.Konflikt(
                f"{regel.nummer} bindet keine MasterSpec; Fred trägt Bezeichnung und Hash in der "
                "Stufenregel ein."
            )
        if sha != regel.masterspec_sha256:
            raise dienst.Konflikt(
                f"Der Hash passt nicht zur in {regel.nummer} gebundenen MasterSpec "
                f"{regel.masterspec_bezeichnung}."
            )
        stelle = str(f.get("stelle", "")).strip()
        if not stelle:
            raise dienst.DienstFehler("Für die MasterSpec die Stelle angeben, z. B. P12313.")
        return {
            "quelle": "masterspec",
            "bezeichnung": regel.masterspec_bezeichnung,
            "sha256": sha,
            "stelle": stelle[:80],
            "text": text.strip(),
            # Die MasterSpec liegt nicht in KI-PM: geprüft sind Bindung und Hash, nicht der
            # Wortlaut. Der Beleg sagt das offen.
            "wortlaut_geprueft": False,
        }
    m = re.fullmatch(r"([EU])-([1-9][0-9]{0,8})", quelle)
    stand = f.get("stand")
    if m is None or type(stand) is not int:
        raise dienst.DienstFehler("quelle ist E-n, U-n oder masterspec; stand ist eine Zahl.")
    nummer = int(m.group(2))
    if m.group(1) == "E":
        e = akte.entscheidung_holen(akteur, projekt, nummer)
        if e.zustand != Entscheidung.ENTSCHIEDEN or e.stand != stand:
            raise dienst.Konflikt(f"{e.nummer} ist nicht entschieden in Stand {stand}.")
        if Planungsentscheid.objects.filter(entscheidung=e).exists():
            raise dienst.Konflikt(
                f"{e.nummer} hat die Planung selbst gebucht; sie trägt keine weitere Entscheidung."
            )
        inhalt, digest = e.text, _sha(e.text)
    else:
        u = Unterlage.objects.filter(projekt=projekt, nummer=nummer).first()
        if u is None:
            raise dienst.KeinZugriff("Nicht gefunden.")
        if u.archiviert or u.art != Unterlage.TEXT or u.aktuelle_nr != stand:
            raise dienst.Konflikt(
                f"{u.kennung} ist kein aktueller Text in Fassung {stand} (aktuell {u.aktuelle_nr})."
            )
        v = u.versionen.get(nummer=u.aktuelle_nr)
        inhalt, digest = v.text, v.sha256 or _sha(v.text)
    if sha != digest:
        raise dienst.Konflikt(f"Der Hash passt nicht zu {quelle} Stand {stand}.")
    if _normal(text) not in _normal(inhalt):
        raise dienst.Konflikt(f"Die Fundstelle steht nicht wörtlich in {quelle} Stand {stand}.")
    return {
        "quelle": quelle,
        "stand": stand,
        "sha256": digest,
        "text": text.strip(),
        "wortlaut_geprueft": True,
    }


def _blocker_pruefen(akteur, projekt, auftrag, revision, meldung_id):
    a = _auftrag(akteur, projekt, auftrag, revision)
    blocker = lieferungen._blocker(a)
    if a.zustand != Auftrag.BLOCKIERT or blocker is None or blocker.pk != meldung_id:
        raise dienst.Konflikt(
            f"{a.nummer} ist nicht mehr durch Meldung {meldung_id} blockiert; neu lesen."
        )
    if blocker.art not in (Meldung.RUECKFRAGE, Meldung.HINDERNIS):
        raise dienst.Konflikt("Nur eine Rückfrage oder ein Hindernis lässt sich so beantworten.")
    if blocker.lieferbedarf.exists():
        raise dienst.Konflikt(
            "Die Blockade ist ein technischer Lieferbedarf; sie läuft über die Lieferautomatik."
        )
    if not a.hat_bearbeiter:
        raise dienst.Konflikt(f"{a.nummer} hat keinen bisherigen Bearbeiter.")
    if Planungsentscheid.objects.filter(meldung=blocker).exists():
        raise dienst.Konflikt(
            f"Auf Meldung {blocker.pk} hat die Planung schon geantwortet; je Blockade nur einmal."
        )
    return a, blocker


def entscheiden(
    akteur,
    projekt: Projekt,
    *,
    entscheidung,
    warum="",
    bereich,
    fundstelle,
    auftrag=None,
    revision=None,
    meldung_id=None,
):
    """Stufe B: technische Entscheidung mit geprüfter Fundstelle buchen und, wenn ein Auftrag
    genannt ist, dessen Blockade beim bisherigen Bearbeiter aufheben. Alles oder nichts."""
    text = str(entscheidung or "").strip()
    begruendung = str(warum or "").strip()
    if not text:
        raise dienst.DienstFehler("Die Entscheidung darf nicht leer sein.")
    _stufe_c(bereich, (text, begruendung))
    try:
        # Eine Schreibtransaktion (IMMEDIATE): Regel, Fundstelle, Blockade und Buchung sehen
        # denselben Stand; eine gleichzeitige Antwort Freds wartet oder kommt vorher.
        with transaction.atomic():
            regel = _regel(akteur, projekt, "B")
            beleg = _fundstelle(akteur, projekt, regel, fundstelle)
            planung = _mit_regel(akteur, regel)
            a = blocker = None
            if auftrag is not None:
                a, blocker = _blocker_pruefen(akteur, projekt, auftrag, revision, meldung_id)
            ort = beleg["quelle"] + (
                f" Stand {beleg['stand']}" if "stand" in beleg else f" {beleg['stelle']}"
            )
            e = akte.entscheidung_eintragen(
                planung,
                projekt,
                entscheidung=text,
                warum=begruendung,
                ereignis=f"Von Planung entschieden ({regel.nummer})",
                anlass=f"Zugang {akteur.bindung.name} · Fundstelle {ort}: „{beleg['text'][:160]}“",
            )
            pe = Planungsentscheid.objects.create(
                regel=regel,
                entscheidung=e,
                zugang=akteur.bindung,
                bereich=TECHNIK,
                fundstelle=beleg,
                auftrag=a,
                meldung=blocker,
            )
            if a is not None:
                dienst.rueckfrage_beantworten(
                    planung,
                    a,
                    f"Von Planung entschieden ({e.nummer}, Stufenregel {regel.nummer}, "
                    f"Fundstelle {ort}): {text}",
                )
                a.refresh_from_db()
    except IntegrityError as fehler:
        raise dienst.Konflikt(
            "Auf diese Blockademeldung hat die Planung schon geantwortet; je Blockade nur einmal."
        ) from fehler
    return pe, a


def planungsentscheid_widerrufen(akteur, projekt: Projekt, e: Entscheidung) -> None:
    """Fred nimmt eine von der Planung gebuchte Entscheidung mit einem Knopf zurück."""
    _nur_fred(akteur, "Planungsentscheidungen widerrufen")
    with transaction.atomic():
        pe = Planungsentscheid.objects.select_related("regel").filter(entscheidung=e).first()
        if pe is None or pe.widerrufen_am is not None:
            raise dienst.Konflikt(f"{e.nummer} ist keine offene Planungsentscheidung.")
        e = akte.entscheidung_holen(akteur, projekt, e.pk)
        if e.zustand != Entscheidung.ZURUECKGEZOGEN:
            akte.entscheidung_aendern(
                akteur, projekt, e, erwarteter_stand=e.stand, zurueckziehen=True
            )
        Planungsentscheid.objects.filter(pk=pe.pk).update(widerrufen_am=timezone.now())
        dienst._ereignis(
            akteur,
            projekt=projekt,
            objekt_typ="Entscheidung",
            objekt_kennung=e.nummer,
            ereignis="Planungsentscheidung widerrufen",
            anlass=f"Stufenregel {pe.regel.nummer}",
        )

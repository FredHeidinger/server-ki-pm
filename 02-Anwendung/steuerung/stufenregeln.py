"""Stufenregel für die Planung (KIPM-31, ``01-Planung/AKTIONSVOLLMACHT.md``).

Fred speichert je Projekt und PC-Zugang eine befristete, widerrufbare Regel. Sie trägt Änderungen
der Planung ohne ``fred_sagte``; der Verlauf nennt dann die Regel statt Freds Worten, niemand
spricht in seinem Namen.

- Stufe A: einen fertig geplanten Auftrag freigeben, eine fehlende Lieferkante nachtragen, einen
  überholten Entwurf, an dem nichts hängt, mit Grund und Verweis schließen (KIPM-34).
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
import unicodedata
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import akte, dienst, lieferungen
from .auftragsaktionen import VOLLMACHT_HOECHSTENS_TAGE
from .models import (
    Auftrag,
    ChatBindung,
    Entscheidung,
    Lieferbedarf,
    Lieferung,
    Meldung,
    Planungsentscheid,
    Planungsfortsetzung,
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
# Breite Wortstämme, damit auch Zusammensetzungen („Zielsetzung“, „Datenbank“, „Kostenrahmen“,
# „Rechtevergabe“, „Bearbeiter-Wechsel“) zu Fred gehen. Ein Fehlalarm kostet nur eine Frage an
# Fred; ausgenommen sind nur die harmlosen Wörter in _STUFE_C_HARMLOS.
_STUFE_C_WORTE = re.compile(
    "|".join(
        (
            r"ziel",  # Ziel, Zielsetzung, Hauptziel, Zielgruppe
            r"daten",  # Daten, Datenbank, Kundendaten, Datenschutz
            r"personenbezogen|dsgvo",
            r"kost|\beur\b|euro|€|preis|lizenz|budget|rechnung|gebühr|\babo",
            r"recht|berechtigung|zugriff|admin|\broot\b|\bsudo\b",
            r"cloud|microsoft|\bms\s*365|\bo365|m365|office\s*365|teams|outlook|azure|"
            r"\bentra\b|sharepoint|onedrive",
            r"sicherheit|firewall|\bports?\b|verschl(ü|ue)ssel|zertifikat|\btls\b|\bvpn\b|"
            r"\bssh\b|passwort|kennwort|geheimnis|token",
            r"l(ö|oe)sch|entfern|\bdrop\b|truncate|purge|vernicht",
            r"neu[\s-]*schn(itt|eid)",
            r"bearbeiter[\s-]*(wechsel|tausch)|wechsel\s+des\s+bearbeiter|"
            r"\b(ander|neu)(er|en|e)?\s+bearbeiter|bearbeiter\s+wird\s+(gewechselt|getauscht)",
        )
    ),
    re.IGNORECASE,
)
_STUFE_C_HARMLOS = re.compile(
    r"\btestziel(e|en|s)?\b|\bmetadaten\b|\bzentral\w*|\bbisherige[nrms]?\s+bearbeiter\w*",
    re.IGNORECASE,
)


def _normal_fuer_stufe_c(text: str) -> str:
    """Schreibvarianten angleichen: NFC (ö statt o plus Trema), weiche Trennstriche und
    Null-Breite-Zeichen weg, Leerraum zu einem Leerzeichen."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub("[\u00ad\u200b\u200c\u200d\u2060\ufeff]", "", text)
    return " ".join(text.split())


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


def _verweis(akteur, projekt: Projekt, a: Auftrag, verweis: str) -> str:
    """Aufnehmender Auftrag oder entschiedene E-n desselben Projekts; sonst Ablehnung."""
    nr = verweis.upper().removeprefix("E-")
    if verweis.upper().startswith("E-") and nr.isdigit():
        e = Entscheidung.objects.filter(pk=int(nr), betroffene_projekte=projekt).first()
        if e is None or e.zustand != Entscheidung.ENTSCHIEDEN:
            raise dienst.DienstFehler(
                f"Verweis {verweis} ist in {projekt.kennung} keine entschiedene Entscheidung. "
                "Es wurde nichts geändert."
            )
        return e.nummer
    try:
        ziel = Auftrag.objects.filter(
            pk=dienst.auftrag_id_aus_nummer(verweis), projekt=projekt
        ).first()
    except dienst.DienstFehler:
        ziel = None
    if ziel is None or ziel.pk == a.pk or ziel.zustand == Auftrag.ABGEBROCHEN:
        raise dienst.DienstFehler(
            f"Verweis {verweis} nennt keinen anderen, nicht abgebrochenen Auftrag in "
            f"{projekt.kennung} und keine entschiedene E-n. Es wurde nichts geändert."
        )
    return ziel.nummer


def _haengt_daran(a: Auftrag) -> str:
    """Grund, warum die Planung diesen Entwurf nicht schließen darf, sonst leer."""
    offen = ~Q(zustand__in=Auftrag.ENDZUSTAENDE)
    if a.zustand != Auftrag.ENTWURF:
        return (
            f"{a.nummer} ist nicht im Entwurf ({a.get_zustand_display()}); freigegebene, "
            "laufende oder blockierte Aufträge schließt nur Fred."
        )
    if a.nur_mensch:
        return f"{a.nummer} ist nur für Menschen markiert; schließen darf ihn nur Fred."
    if a.ergebnisse.exists():
        return f"{a.nummer} hat ein gebuchtes Ergebnis; schließen darf ihn nur Fred."
    if a.lieferungen.exists():
        return f"{a.nummer} hat eine gebuchte Lieferung; schließen darf ihn nur Fred."
    if a.chat_delegation_id is not None or dienst._werkbank_aktiv(a):
        return f"{a.nummer} gehört zu einer Werkbank (W1); schließen darf ihn nur Fred."
    nachfolger = sorted(
        x.nummer for x in Auftrag.objects.filter(offen, abhaengigkeiten__vorgaenger=a).distinct()
    )
    if nachfolger:
        return f"{', '.join(nachfolger)} nennt {a.nummer} als Vorgänger; erst dort lösen."
    verbraucher = sorted(
        {
            b.meldung.auftrag.nummer
            for b in Lieferbedarf.objects.filter(lieferauftrag=a)
            .exclude(meldung__auftrag=a)
            .exclude(meldung__auftrag__zustand__in=Auftrag.ENDZUSTAENDE)
            .select_related("meldung__auftrag")
        }
    )
    if verbraucher:
        return (
            f"{', '.join(verbraucher)} erwartet von {a.nummer} eine Lieferung (Lieferant); "
            "erst dort lösen."
        )
    return ""


def schliessen(akteur, projekt: Projekt, *, auftrag, revision, grund, verweis):
    """Stufe A (KIPM-34): einen Entwurf, an dem nichts hängt, mit Grund und Verweis abbrechen.
    Enger als Freds Abbrechen in der Oberfläche; derselbe Abbruchweg (``dienst.abbruch_buchen``),
    der Verlauf nennt Stufenregel, Grund und Verweis."""
    grund = str(grund or "").strip()
    verweis = str(verweis or "").strip()
    with transaction.atomic():
        regel = _regel(akteur, projekt, "A")
        a = _auftrag(akteur, projekt, auftrag, revision)
        if not grund:
            raise dienst.DienstFehler(
                "grund fehlt: Warum ist der Entwurf überholt? Es wurde nichts geändert."
            )
        if not verweis:
            raise dienst.DienstFehler(
                "verweis fehlt: aufnehmender Auftrag (z. B. NEX-259) oder Entscheidung E-n. "
                "Nichts geändert."
            )
        ziel = _verweis(akteur, projekt, a, verweis)
        hindernis = _haengt_daran(a)
        if hindernis:
            raise dienst.Konflikt(hindernis + " Es wurde nichts geändert.")
        dienst.abbruch_buchen(_mit_regel(akteur, regel), a, f"{grund} · Verweis {ziel}")
    a.refresh_from_db()
    return a, regel, ziel


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
        treffer = _STUFE_C_WORTE.search(_STUFE_C_HARMLOS.sub(" ", _normal_fuer_stufe_c(text)))
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


def _blocker_pruefen(
    akteur, projekt, auftrag, revision, meldung_id, arten=(Meldung.RUECKFRAGE, Meldung.HINDERNIS)
):
    a = _auftrag(akteur, projekt, auftrag, revision)
    blocker = lieferungen._blocker(a)
    if a.zustand != Auftrag.BLOCKIERT or blocker is None or blocker.pk != meldung_id:
        raise dienst.Konflikt(
            f"{a.nummer} ist nicht mehr durch Meldung {meldung_id} blockiert; neu lesen."
        )
    if blocker.art not in arten:
        raise dienst.Konflikt(
            f"Eine Blockade der Art {blocker.get_art_display()} lässt sich so nicht aufheben."
        )
    if blocker.lieferbedarf.exists():
        raise dienst.Konflikt(
            "Die Blockade ist ein technischer Lieferbedarf; sie läuft über die Lieferautomatik."
        )
    if not a.hat_bearbeiter:
        raise dienst.Konflikt(f"{a.nummer} hat keinen bisherigen Bearbeiter.")
    if (
        Planungsentscheid.objects.filter(meldung=blocker).exists()
        or Planungsfortsetzung.objects.filter(meldung=blocker).exists()
    ):
        raise dienst.Konflikt(
            f"Auf Meldung {blocker.pk} hat die Planung schon geantwortet; je Blockade nur einmal."
        )
    return a, blocker


def _nennt(e: Entscheidung, a: Auftrag) -> bool:
    text = f"{e.text}\n{e.warum}"
    return any(
        re.search(rf"(?<![A-Za-z0-9-]){re.escape(n)}(?![0-9])", text)
        for n in {a.nummer, a.alte_nummer}
        if n
    )


def _beleg(projekt: Projekt, a: Auftrag, nachweis) -> dict:
    """Beleg für eine Fehler-Blockade: gebuchte Lieferung (L-n) oder erledigter
    Korrekturauftrag desselben Projekts."""
    if not isinstance(nachweis, dict) or not (nachweis.get("lieferung") or nachweis.get("auftrag")):
        raise dienst.DienstFehler(
            "Eine Fehler-Blockade braucht einen Beleg: nachweis mit lieferung (L-n) oder einem "
            "erledigten Korrekturauftrag (auftrag). Nichts fortgesetzt."
        )
    if nachweis.get("lieferung"):
        nr = str(nachweis["lieferung"]).strip().upper().removeprefix("L-")
        lieferung = (
            Lieferung.objects.select_related("auftrag")
            .filter(pk=int(nr), auftrag__projekt=projekt)
            .first()
            if nr.isdigit()
            else None
        )
        if lieferung is None or lieferung.revision_nr != lieferung.auftrag.revision_nr:
            raise dienst.Konflikt(
                f"Lieferung {nachweis['lieferung']} ist in diesem Projekt nicht als gültige "
                "Lieferung der aktuellen Anbieterrevision gebucht."
            )
        return {"lieferung": f"L-{lieferung.pk}", "auftrag": lieferung.auftrag.nummer}
    k = Auftrag.objects.filter(
        pk=dienst.auftrag_id_aus_nummer(str(nachweis["auftrag"])), projekt=projekt
    ).first()
    if k is None or k.pk == a.pk:
        raise dienst.KeinZugriff("Nicht gefunden.")
    if k.zustand != Auftrag.ERLEDIGT:
        raise dienst.Konflikt(f"Der Korrekturauftrag {k.nummer} ist nicht erledigt.")
    return {"auftrag": k.nummer}


def fortsetzen(
    akteur,
    projekt: Projekt,
    *,
    auftrag,
    revision,
    meldung_id,
    entscheidung,
    stand,
    nachweis=None,
):
    """Stufe A: einen blockierten Auftrag beim bisherigen Bearbeiter fortsetzen, weil Fred mit
    einer entschiedenen E-n darüber entschieden hat, die den Auftrag nennt. Es wird keine zweite
    Entscheidung gebucht. Eine Fehler-Blockade braucht zusätzlich einen Beleg. Je
    Blockademeldung höchstens einmal; alles oder nichts."""
    nr = str(entscheidung or "").strip().upper().removeprefix("E-")
    if not nr.isdigit() or type(stand) is not int:
        raise dienst.DienstFehler("entscheidung (E-n) und stand aus kipm_entscheidungen angeben.")
    try:
        with transaction.atomic():
            regel = _regel(akteur, projekt, "A")
            a, blocker = _blocker_pruefen(
                akteur,
                projekt,
                auftrag,
                revision,
                meldung_id,
                arten=(Meldung.RUECKFRAGE, Meldung.HINDERNIS, Meldung.FEHLER),
            )
            e = akte.entscheidung_holen(akteur, projekt, int(nr))
            if e.zustand != Entscheidung.ENTSCHIEDEN or e.stand != stand:
                raise dienst.Konflikt(f"{e.nummer} ist nicht entschieden in Stand {stand}.")
            if Planungsentscheid.objects.filter(entscheidung=e).exists():
                raise dienst.Konflikt(
                    f"{e.nummer} hat die Planung selbst gebucht; fortsetzen trägt nur eine "
                    "Entscheidung von Fred."
                )
            if e.klaerungsbelege:
                raise dienst.Konflikt(
                    f"{e.nummer} hat der frühere Blockadenabgleich per Klärungsregel gebucht; "
                    "fortsetzen trägt nur eine Entscheidung von Fred."
                )
            if not _nennt(e, a):
                raise dienst.Konflikt(f"{e.nummer} nennt {a.nummer} nicht.")
            if e.entschieden_am is None or e.entschieden_am < blocker.zeitpunkt:
                raise dienst.Konflikt(
                    f"{e.nummer} ist vor der Blockademeldung {blocker.pk} entschieden worden."
                )
            beleg = _beleg(projekt, a, nachweis) if blocker.art == Meldung.FEHLER else {}
            Planungsfortsetzung.objects.create(
                regel=regel,
                zugang=akteur.bindung,
                auftrag=a,
                meldung=blocker,
                entscheidung=e,
                nachweis=beleg,
            )
            planung = _mit_regel(akteur, regel)
            belegtext = ", Beleg " + "/".join(beleg.values()) if beleg else ""
            dienst._ereignis(
                planung,
                projekt=projekt,
                objekt_typ="Auftrag",
                objekt_kennung=a.nummer,
                ereignis=f"Von Planung fortgesetzt ({regel.nummer})",
                anlass=f"Zugang {akteur.bindung.name} · nach {e.nummer} Stand {e.stand}{belegtext}",
                revision=str(a.revision_nr),
            )
            dienst.rueckfrage_beantworten(
                planung,
                a,
                f"Fortgesetzt nach Freds Entscheidung {e.nummer} (Stufenregel {regel.nummer}"
                f"{belegtext}): {e.text[:300]}",
            )
            a.refresh_from_db()
    except IntegrityError as fehler:
        raise dienst.Konflikt(
            "Diese Blockademeldung hat die Planung schon fortgesetzt; je Blockade nur einmal."
        ) from fehler
    return a, e, regel, beleg


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

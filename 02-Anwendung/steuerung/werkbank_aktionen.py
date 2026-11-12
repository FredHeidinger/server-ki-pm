"""Werkbank-Anbindung WP6: menschliche Aktionen, Bearbeitung ändern, Abbrechen, Kontextänderung
und Halt (Vertrag kipm-werkbank/1, Auftrag 4.7 und 7.3 bis 7.6). Teil von ``werkbank.py``.

Nur Fred (Owner) erzeugt Aktionen; Werkbank-Schlüssel und ``fred_sagte`` ersetzen das nicht. Eine
Aktion wirkt höchstens einmal und bleibt „ausstehend“, bis die Werkbank ihre Anwendung bestätigt.
Abbruch und Bearbeitung ändern gelten erst nach bestätigtem Stillstand (``halt_bestaetigt`` oder
``abbruch_bestaetigt``); bis dahin ist der Halt nur angefordert.
"""

from django.db import transaction
from django.utils import timezone

from . import dienst, werkbank, werkbank_lauf, werkbank_vertrag
from .models import (
    AUSFUEHRUNG_AKTIV,
    Auftrag,
    WerkbankAktion,
    WerkbankAusfuehrung,
    WerkbankZuweisung,
)

OFFEN = ("ausstehend", "angenommen")
#: Hat die Werkbank länger als so viele Sekunden nach einer Aktion noch abgeglichen, die Aktion aber
#: weder angewendet noch abgewiesen, gilt sie als unbeantwortet (Testlauf KTL-1, B7).
UNBEANTWORTET_S = 60
HALT_ARTEN = {"bearbeitung_beenden": "bearbeitung_aendern", "abbrechen": "abbrechen"}


def _bedarf_offen(ausf: WerkbankAusfuehrung, typ: str, schluessel: str, wert: str) -> bool:
    return any(
        b.get("typ") == typ and b.get(schluessel) == wert and b.get("offen") for b in ausf.bedarf
    )


def _offene_aktion(ausf: WerkbankAusfuehrung, art: str, **ziel) -> bool:
    return any(
        all(a.ziel.get(k) == v for k, v in ziel.items())
        for a in WerkbankAktion.objects.filter(ausfuehrung=ausf, art=art, zustand__in=OFFEN)
    )


def halt_setzen(akteur, ausf: WerkbankAusfuehrung, zweck: str, grund: str = "") -> bool:
    """Einen Halt anfordern. Ein schon angeforderter Halt bleibt bestehen (False)."""
    if ausf.halt_zweck or ausf.zustand not in AUSFUEHRUNG_AKTIV:
        return False
    ausf.halt_zweck = zweck
    ausf.halt_angefordert_am = timezone.now()
    ausf.zustand = "abbruch_angefordert" if zweck == "abbrechen" else "halt_angefordert"
    if zweck == "kontext_veraltet":
        ausf.bedarf = [
            *ausf.bedarf,
            {
                "typ": "kontext_veraltet",
                "grund": grund or "Der Kontext hat sich geändert.",
                "offen": True,
            },
        ]
    ausf.save()
    a = Auftrag.objects.select_related("projekt").get(pk=ausf.auftrag_id)
    dienst._ereignis(
        akteur,
        projekt=a.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=a.nummer,
        ereignis={
            "abbrechen": "Abbruch angefordert",
            "bearbeitung_aendern": "Halt angefordert (Bearbeitung ändern)",
            "kontext_veraltet": "Halt angefordert (Kontext veraltet)",
        }[zweck],
        anlass=grund,
        revision=str(ausf.revision),
    )
    return True


def aktion_erstellen(
    akteur: dienst.Akteur,
    ausfuehrung: WerkbankAusfuehrung,
    art: str,
    *,
    erwartet_zustand: str | None = None,
    frage_id: str = "",
    text: str = "",
    freigabe_id: str = "",
    paket: str = "",
) -> WerkbankAktion:
    """Eine menschliche Aktion auslösen (nur der Owner). Prüft den Stand unter Sperre; stimmt der
    ``erwartet_zustand`` der Oberfläche nicht mehr, wird sie abgewiesen."""
    dienst._owner_erforderlich(akteur, "Aktionen für eine Werkbank-Ausführung auslösen")
    with transaction.atomic():
        ausf = (
            WerkbankAusfuehrung.objects.select_for_update()
            .select_related("auftrag__projekt")
            .get(pk=ausfuehrung.pk)
        )
        a = ausf.auftrag
        if ausf.zustand not in AUSFUEHRUNG_AKTIV:
            raise dienst.Konflikt(f"Die Ausführung von {a.nummer} ist beendet.")
        if erwartet_zustand and ausf.zustand != erwartet_zustand:
            raise dienst.Konflikt(
                f"Der Stand hat sich geändert (jetzt: {ausf.zustand}). Seite neu laden und prüfen."
            )
        if art in HALT_ARTEN and ausf.zustand == "abschluss_gemeldet":
            raise dienst.Konflikt(
                f"Der Abschluss von {a.nummer} ist schon gespeichert; ein Halt ist nicht mehr "
                "möglich."
            )
        ziel: dict = {}
        if art == "antworten":
            if not text.strip():
                raise dienst.DienstFehler("Die Antwort darf nicht leer sein.")
            if not _bedarf_offen(ausf, "frage", "frage_id", frage_id):
                raise dienst.DienstFehler("Diese Rückfrage ist nicht (mehr) offen.")
            if _offene_aktion(ausf, "antworten", frage_id=frage_id):
                raise dienst.Konflikt("Diese Rückfrage wird schon beantwortet.")
            ziel = {"frage_id": frage_id, "text": text.strip()[:20000]}
        elif art in ("start_freigeben", "uebernahme_freigeben"):
            gesucht = "start" if art == "start_freigeben" else "uebernahme"
            offen = next(
                (
                    b
                    for b in ausf.bedarf
                    if b.get("typ") == "freigabe"
                    and b.get("freigabe_id") == freigabe_id
                    and b.get("offen")
                ),
                None,
            )
            if offen is None or offen.get("art") != gesucht:
                raise dienst.DienstFehler(
                    f"Es gibt keine offene Freigabe „{freigabe_id}“ dieser Art."
                )
            if _offene_aktion(ausf, art, freigabe_id=freigabe_id):
                raise dienst.Konflikt("Diese Freigabe wird schon erteilt.")
            ziel = {"freigabe_id": freigabe_id}
        elif art == "weiterbauen":
            # Ein zweiter Klick (Doppelklick, Neuladen) liefert die schon gesendete Aktion zurück,
            # statt eine zweite anzulegen oder einen Fehler zu melden (Testlauf KTL-1, P19).
            vorhanden = WerkbankAktion.objects.filter(
                ausfuehrung=ausf, art="weiterbauen", zustand__in=OFFEN
            ).first()
            if vorhanden is not None:
                vorhanden.schon_gesendet = True
                return vorhanden
            if not any(
                b.get("offen") and b.get("typ") in ("fehler", "unklar") for b in ausf.bedarf
            ):
                raise dienst.DienstFehler("Es gibt weder einen Fehler noch einen unklaren Ausgang.")
            ziel = {"paket": paket} if paket else {}
        elif art in HALT_ARTEN:
            zweck = HALT_ARTEN[art]
            if ausf.halt_zweck and ausf.halt_zweck != zweck:
                raise dienst.Konflikt(f"Es ist schon ein Halt angefordert ({ausf.halt_zweck}).")
            vorhanden = WerkbankAktion.objects.filter(
                ausfuehrung=ausf, art=art, zustand__in=OFFEN
            ).first()
            if vorhanden is not None:
                return vorhanden
        else:
            raise dienst.DienstFehler(f"Unbekannte Aktion „{art}“.")
        aktion = WerkbankAktion.objects.create(
            ausfuehrung=ausf,
            art=art,
            revision=ausf.revision,
            zuweisung_version=ausf.zuweisung_version,
            projektbindung_version=ausf.projektbindung_version,
            erwarteter_zustand=ausf.zustand,
            ziel=ziel,
            owner=akteur.benutzer,
        )
        dienst._ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=a.nummer,
            ereignis=f"Aktion ausgelöst: {art}",
            revision=str(ausf.revision),
        )
        if art in HALT_ARTEN:
            halt_setzen(akteur, ausf, HALT_ARTEN[art])
    return aktion


def unbeantwortet(aktion: WerkbankAktion, instanz, jetzt=None) -> bool:
    """Steht die Aktion noch als „ausstehend“, obwohl die Werkbank seit mehr als
    ``UNBEANTWORTET_S`` Sekunden nach ihr abgeglichen hat? Ohne Abgleich seitdem warnt schon die
    Erreichbarkeit (``werkbank.nicht_erreichbar``)."""
    if aktion.zustand != "ausstehend" or instanz.letzter_kontakt is None:
        return False
    jetzt = jetzt or timezone.now()
    return (jetzt - aktion.erstellt_am).total_seconds() > UNBEANTWORTET_S and (
        instanz.letzter_kontakt - aktion.erstellt_am
    ).total_seconds() > UNBEANTWORTET_S


def aktionen_veralten(ausf: WerkbankAusfuehrung, ausser: WerkbankAktion | None = None) -> None:
    """Alle noch offenen Aktionen verlieren ihre Wirkung (neue Zuweisungsfassung)."""
    offene = WerkbankAktion.objects.filter(ausfuehrung=ausf, zustand__in=OFFEN)
    if ausser is not None:
        offene = offene.exclude(pk=ausser.pk)
    offene.update(zustand="veraltet")


def kontext_geaendert(akteur: dienst.Akteur, projekt) -> None:
    """Hat sich der fachliche Kontext einer laufenden Werkbank-Ausführung geändert (Hash des
    Snapshots weicht ab), werden neue Schritte gesperrt und ein Halt „kontext_veraltet“
    angefordert. Fortschritt, Termine, Meilensteine und Git-Fortschreibung zählen nicht."""
    laufende = WerkbankAusfuehrung.objects.filter(
        auftrag__projekt=projekt, zustand__in=AUSFUEHRUNG_AKTIV, halt_zweck=""
    ).select_related("auftrag")
    for ausf in laufende:
        if ausf.zustand in werkbank_lauf.GESPERRT:
            continue
        aktuell = werkbank.kontext_hash(werkbank.snapshot_bauen(ausf.auftrag))
        if aktuell != werkbank.kontext_hash(ausf.snapshot):
            halt_setzen(
                akteur, ausf, "kontext_veraltet", "Der fachliche Kontext hat sich geändert."
            )


# --- Wirkung der Werkbank-Meldungen ----------------------------------------------------------


def _aktion_suchen(ausf, daten) -> WerkbankAktion | None:
    aktion = WerkbankAktion.objects.filter(aktion_id=daten["aktion_id"], ausfuehrung=ausf).first()
    return aktion if aktion is not None and aktion.zustand in OFFEN else None


def _nachklaeren(ausf: WerkbankAusfuehrung) -> None:
    """Nach einer bestätigten Anwendung entscheidet der aktuelle Gesamtstatus."""
    if any(b.get("offen") for b in ausf.bedarf):
        offen_pakete = {b.get("paket") for b in ausf.bedarf if b.get("offen")}
    else:
        offen_pakete = set()
    for p in ausf.pakete:
        if p.get("handlungsbedarf") and p["paket"] not in offen_pakete:
            p["handlungsbedarf"] = False
    if not offen_pakete and not any(
        b.get("offen") and b.get("typ") != "kontext_veraltet" for b in ausf.bedarf
    ):
        if ausf.zustand in werkbank_lauf.WARTEND:
            ausf.zustand = "laeuft"


def _antwort_angewendet(instanz, ausf, daten, op):
    aktion = _aktion_suchen(ausf, daten)
    if aktion is None or aktion.art != "antworten":
        return "historisch"
    aktion.zustand = "ausgefuehrt"
    aktion.ergebnis = {"angewendet": True}
    aktion.save(update_fields=["zustand", "ergebnis"])
    for b in ausf.bedarf:
        if b.get("typ") == "frage" and b.get("frage_id") == aktion.ziel.get("frage_id"):
            b["offen"] = False
    _nachklaeren(ausf)
    return None


def _aktion_angewendet(instanz, ausf, daten, op):
    aktion = _aktion_suchen(ausf, daten)
    if aktion is None:
        return "historisch"
    if daten["ergebnis"] != "angewendet":
        aktion.zustand = "veraltet"
        aktion.ergebnis = {"verworfen": True, "grund": daten.get("grund", "")}
        aktion.save(update_fields=["zustand", "ergebnis"])
        return None
    aktion.zustand = "ausgefuehrt"
    aktion.ergebnis = {"angewendet": True}
    aktion.save(update_fields=["zustand", "ergebnis"])
    if aktion.art in ("start_freigeben", "uebernahme_freigeben"):
        for b in ausf.bedarf:
            if b.get("typ") == "freigabe" and b.get("freigabe_id") == aktion.ziel.get(
                "freigabe_id"
            ):
                b["offen"] = False
    elif aktion.art == "weiterbauen":
        paket = aktion.ziel.get("paket")
        for b in ausf.bedarf:
            if b.get("typ") in ("fehler", "unklar") and (not paket or b.get("paket") == paket):
                b["offen"] = False
        if paket:
            werkbank_lauf._klaeren(ausf, paket)
        else:
            ausf.operationen.filter(art="schritt", klaerung="ungeklaert").update(
                klaerung="geklaert", ausgang_art="fehlschlag"
            )
    elif aktion.art == "antworten":
        for b in ausf.bedarf:
            if b.get("typ") == "frage" and b.get("frage_id") == aktion.ziel.get("frage_id"):
                b["offen"] = False
    _nachklaeren(ausf)
    return None


def _kontext_veraltet(instanz, ausf, daten, op):
    halt_setzen(
        dienst.Akteur.aus_bindung(instanz.bindung), ausf, "kontext_veraltet", daten["grund"]
    )
    return None


def _zurueck_in_den_entwurf(instanz, ausf: WerkbankAusfuehrung, zweck: str) -> None:
    """Nach bestätigtem Stillstand: ENTWURF, Freigabe weg, Bearbeiterbindung gelöst, höhere
    Zuweisungsfassung, offene Aktionen veraltet. Eine neue Ausführung braucht neue Freigabe."""
    a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=ausf.auftrag_id)
    akteur = dienst.Akteur.aus_bindung(instanz.bindung)
    dienst._bearbeiter_abloesen(a)
    a.zustand = Auftrag.ENTWURF
    a.freigegeben_am = None
    a.save()
    zuweisung = WerkbankZuweisung.objects.get(pk=ausf.zuweisung_id)
    zuweisung.version += 1
    zuweisung.kontext_sha256 = ""
    zuweisung.save(update_fields=["version", "kontext_sha256", "geaendert_am"])
    halt_aktionen = WerkbankAktion.objects.filter(
        ausfuehrung=ausf,
        art__in=[k for k, v in HALT_ARTEN.items() if v == zweck],
        zustand__in=OFFEN,
    )
    halt_aktionen.update(zustand="ausgefuehrt")
    aktionen_veralten(ausf)
    ausf.zustand = "beendet"
    ausf.halt_bestaetigt_am = timezone.now()
    dienst._ereignis(
        akteur,
        projekt=a.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=a.nummer,
        ereignis="Ausführung angehalten, zurück in Entwurf",
        anlass={
            "bearbeitung_aendern": "Bearbeitung ändern",
            "kontext_veraltet": "Kontext veraltet: neue Freigabe nötig",
        }[zweck],
        revision=str(ausf.revision),
    )


def _abbruch_anwenden(instanz, ausf: WerkbankAusfuehrung, daten) -> None:
    a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=ausf.auftrag_id)
    dienst._bearbeiter_abloesen(a)
    a.zustand = Auftrag.ABGEBROCHEN
    a.save()
    WerkbankAktion.objects.filter(ausfuehrung=ausf, art="abbrechen", zustand__in=OFFEN).update(
        zustand="ausgefuehrt"
    )
    aktionen_veralten(ausf)
    ausf.zustand = "abgebrochen"
    ausf.halt_bestaetigt_am = timezone.now()
    dienst._ereignis(
        dienst.Akteur.aus_bindung(instanz.bindung),
        projekt=a.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=a.nummer,
        ereignis="Abbruch bestätigt",
        anlass="Integrierte Commits bleiben: "
        + (", ".join(daten.get("commits_bleiben", [])) or "keine"),
        revision=str(ausf.revision),
    )


def _halt_bestaetigt(instanz, ausf, daten, op):
    zweck = daten["zweck"]
    if ausf.halt_zweck != zweck:
        raise werkbank.WerkbankFehler(
            409,
            "ZUSTANDSKONFLIKT",
            "Für diese Ausführung ist kein Halt dieses Zwecks angefordert.",
            "Abgleich abrufen und nur den angeforderten Halt bestätigen.",
        )
    if zweck == "abbrechen":
        _abbruch_anwenden(instanz, ausf, {})
    else:
        _zurueck_in_den_entwurf(instanz, ausf, zweck)
    return None


def _abbruch_bestaetigt(instanz, ausf, daten, op):
    if ausf.halt_zweck != "abbrechen":
        raise werkbank.WerkbankFehler(
            409,
            "ZUSTANDSKONFLIKT",
            "Für diese Ausführung ist kein Abbruch angefordert.",
            "Abgleich abrufen; nur ein angeforderter Abbruch wird bestätigt.",
        )
    _abbruch_anwenden(instanz, ausf, daten)
    return None


def aktionen_fuer_abgleich(instanz, ausfuehrungen) -> list[dict]:
    """Offene Aktionen im Vertragsformat (``abgleich_antwort.aktionen``)."""
    liste = []
    for aktion in (
        WerkbankAktion.objects.filter(ausfuehrung__in=ausfuehrungen, zustand__in=OFFEN)
        .select_related("ausfuehrung", "owner")
        .order_by("pk")[: werkbank.SEITE_MAX]
    ):
        bezug = {}
        if "frage_id" in aktion.ziel:
            bezug["frage_id"] = aktion.ziel["frage_id"]
        if "freigabe_id" in aktion.ziel:
            bezug["freigabe_id"] = aktion.ziel["freigabe_id"]
        if aktion.ziel.get("paket"):
            bezug["pakete"] = [aktion.ziel["paket"]]
        eintrag = {
            "aktion_id": str(aktion.aktion_id),
            "art": aktion.art,
            "zustand": aktion.zustand,
            "quelle": werkbank._quelle_der_ausfuehrung(aktion.ausfuehrung, instanz),
            "erwarteter_zustand": werkbank._vertragszustand(aktion.erwarteter_zustand),
            "bezug": bezug,
            "owner": aktion.owner.get_username()[:500] or "Owner",
            "erstellt_am": werkbank._zeit(aktion.erstellt_am),
        }
        if aktion.ziel.get("text"):
            eintrag["antworttext"] = aktion.ziel["text"]
        eintrag["inhaltshash"] = werkbank_vertrag.sha256_hex(
            {k: v for k, v in eintrag.items() if k not in ("zustand", "inhaltshash")}
        )
        liste.append(eintrag)
    return liste


werkbank_lauf.EFFEKTE["antwort_angewendet"] = _antwort_angewendet
werkbank_lauf.EFFEKTE["aktion_angewendet"] = _aktion_angewendet
werkbank_lauf.EFFEKTE["kontext_veraltet"] = _kontext_veraltet
werkbank_lauf.EFFEKTE["halt_bestaetigt"] = _halt_bestaetigt
werkbank_lauf.EFFEKTE["abbruch_bestaetigt"] = _abbruch_bestaetigt
for _art in (
    "antwort_angewendet",
    "aktion_angewendet",
    "kontext_veraltet",
    "halt_bestaetigt",
    "abbruch_bestaetigt",
):
    werkbank.HANDLER[_art] = werkbank_lauf._handler(_art)

"""Werkbank-Anbindung WP5: ``schritt`` und ``ereignisse`` (Vertrag kipm-werkbank/1, Auftrag 4.5,
7.1 und 7.7). Teil von ``werkbank.py``: dieselben Regeln, nur in eigener Datei; die Wirkungen
tragen sich in ``werkbank.HANDLER`` ein.

Eine Meldung wird gegen ihre Quellbindung geprüft (``werkbank.quelle_pruefen``). Verspätete
Meldungen zu überholten Fassungen oder zu einer beendeten Ausführung werden gespeichert und
historisch quittiert, ohne etwas Aktuelles zu ändern. Neue Schritte verweigert der Server in der
Antwort (``erlaubt: false`` mit Grundcode), nicht mit einem HTTP-Fehler.
"""

import uuid
from typing import Any

from . import dienst, werkbank, werkbank_vertrag
from .models import (
    Auftrag,
    Meldung,
    WerkbankAktion,
    WerkbankAusfuehrung,
    WerkbankEreignis,
    WerkbankOperation,
)

#: Zustände, die eine Meldung der Werkbank nicht überschreiben darf (Halt, Abbruch, Abschluss).
GESPERRT = (
    "halt_angefordert",
    "angehalten",
    "abbruch_angefordert",
    "abgebrochen",
    "abschluss_gemeldet",
)
#: Wie GESPERRT, dazu „unklar“: Ein unklarer Ausgang überlebt jede weitere Meldung und löst sich
#: nur durch Freds „Weiterbauen“ (Auftrag 4.5: keine neue Ausführung bei UNKLAR).
FEST = (*GESPERRT, "unklar")
#: Aktionsarten, die eine offene Freigabe erteilen, und die Art der Freigabe dazu.
FREIGABE_ARTEN = {"start_freigeben": "start", "uebernahme_freigeben": "uebernahme"}
#: Zustände, in denen die Ausführung auf Fred oder eine Klärung wartet.
WARTEND = ("wartet_antwort", "wartet_freigabe", "fehler", "unklar")
#: Paketzustände, nach denen ein ungeklärter Versuch des Pakets als geklärt gilt.
ABGESCHLOSSEN = ("erledigt", "ersetzt", "zerlegt", "verworfen", "fehler")


def _zustand_setzen(ausf: WerkbankAusfuehrung, neu: str) -> None:
    if ausf.zustand not in FEST:
        ausf.zustand = neu


def _fortsetzbar(ausf: WerkbankAusfuehrung) -> bool:
    """Läuft oder wartet noch ein Paket, das ohne Fred weiterkommt? Dann blockiert ein einzelner
    Handlungsbedarf nicht den ganzen Auftrag (Auftrag 7.1)."""
    return any(
        p.get("zustand") in ("laeuft", "offen") and not p.get("handlungsbedarf")
        for p in ausf.pakete
    )


def _bedarf_offen(ausf: WerkbankAusfuehrung, *typen: str) -> list[dict]:
    return [b for b in ausf.bedarf if b.get("offen") and b.get("typ") in typen]


def zustand_aus_bedarf(ausf: WerkbankAusfuehrung) -> str:
    """Der technische Zustand, der zum offenen Bedarf passt (nach Offline-Phase oder Antwort):
    unklar vor allem, sonst läuft es, wenn ein Paket ohne Fred weiterkommt, sonst wartet es."""
    if _bedarf_offen(ausf, "unklar"):
        return "unklar"
    if _fortsetzbar(ausf):
        return "laeuft"
    for typ, zustand in (
        ("freigabe", "wartet_freigabe"),
        ("frage", "wartet_antwort"),
        ("fehler", "fehler"),
    ):
        if _bedarf_offen(ausf, typ):
            return zustand
    return "laeuft" if ausf.planfassung != "vorplanung" or ausf.pakete else "plant"


def _blockade(ausf: WerkbankAusfuehrung, ziel: str) -> None:
    if ausf.zustand in FEST:
        return
    if _fortsetzbar(ausf):
        if ausf.zustand in ("plant", "offline"):
            ausf.zustand = "laeuft"
    else:
        ausf.zustand = ziel


def auftrag_abgleichen(ausf: WerkbankAusfuehrung) -> None:
    """Der Auftragszustand folgt dem technischen Zustand: BLOCKIERT, solange nichts automatisch
    weiterläuft und Frage, Freigabe, Fehler oder ein unklarer Ausgang wartet; sonst IN_ARBEIT."""
    a = Auftrag.objects.get(pk=ausf.auftrag_id)
    if a.zustand not in (Auftrag.IN_ARBEIT, Auftrag.BLOCKIERT):
        return
    ziel = Auftrag.BLOCKIERT if ausf.zustand in WARTEND else Auftrag.IN_ARBEIT
    if a.zustand != ziel:
        Auftrag.objects.filter(pk=a.pk).update(zustand=ziel)


def _verlauf(instanz, ausf: WerkbankAusfuehrung, art: str, text: str, ereignis: str) -> None:
    """Meldung (damit „Wartet auf dich“ sie zeigt) und Eintrag im Verlauf."""
    a = Auftrag.objects.select_related("projekt").get(pk=ausf.auftrag_id)
    Meldung.objects.create(
        auftrag=a,
        revision_nr=ausf.revision,
        art=art,
        text=text[:5000],
        akteur_name=f"{instanz.name} (Werkbank)"[:120],
        bindung=instanz.bindung,
    )
    dienst._ereignis(
        dienst.Akteur.aus_bindung(instanz.bindung),
        projekt=a.projekt,
        objekt_typ="Auftrag",
        objekt_kennung=a.nummer,
        ereignis=ereignis,
        anlass=text[:300],
        revision=str(ausf.revision),
    )


def _paket(ausf: WerkbankAusfuehrung, name: str, **felder: Any) -> dict:
    for p in ausf.pakete:
        if p.get("paket") == name:
            p.update(felder)
            return p
    neu = {"paket": name, **felder}
    ausf.pakete = [*ausf.pakete, neu]
    return neu


def _klaeren(ausf: WerkbankAusfuehrung, paket: str, ausgang: str = "fehlschlag") -> None:
    """Der Ausgang der Versuche dieses Pakets ist bekannt: ``erfolg`` (danach kein neuer Versuch)
    oder ``fehlschlag`` (neuer Versuch erlaubt)."""
    WerkbankOperation.objects.filter(
        ausfuehrung=ausf, art="schritt", paket=paket, klaerung="ungeklaert"
    ).update(klaerung="geklaert", ausgang_art=ausgang)


# --- Bauplan zur Anzeige ----------------------------------------------------------------------

#: Paketstände als Wort; „blockiert“ und „abgelöst“ leitet ``bauplan`` ab, die Werkbank meldet sie
#: nicht.
PAKET_WORT = {
    "offen": "offen",
    "laeuft": "läuft",
    "erledigt": "erledigt",
    "ersetzt": "ersetzt",
    "zerlegt": "zerlegt",
    "verworfen": "verworfen",
    "fehler": "Fehler",
    "blockiert": "blockiert",
    "abgeloest": "abgelöst",
}
_ENDGUELTIG = ("erledigt", "ersetzt", "zerlegt", "verworfen")


def _versuch_von(name: str, haupt: list[str]) -> str | None:
    """Das Planpaket, dessen Versuch ``name`` ist: der längste Paketname, mit dem ``name`` beginnt,
    gefolgt von einem Trennzeichen (die Werkbank hängt Auftrag und Versuch an, z. B. ``-v2``)."""
    passend = [
        h for h in haupt if len(name) > len(h) and name.startswith(h) and name[len(h)] in "-._"
    ]
    return max(passend, key=len) if passend else None


def bauplan(ausf: WerkbankAusfuehrung) -> list[dict]:
    """Planpakete mit ihren Versuchen, abgeleitetem Stand und letztem Fehlergrund (Testlauf KTL-1).

    Ein Paket oder Versuch, der noch „läuft“ oder „offen“ heißt, ohne dass ein Schritt ungeklärt
    ist, gilt als blockiert, wenn für ihn oder den ganzen Auftrag ein Fehler offen ist, sonst als
    abgelöst, wenn ein späterer Versuch folgt. Das Planpaket zeigt den Stand seines neuesten
    Versuchs und den letzten Fehlergrund seiner Versuche.
    """
    pakete = [dict(p) for p in ausf.pakete]
    namen = [p["paket"] for p in pakete]
    if any(p.get("im_plan") for p in pakete):
        haupt = [p for p in pakete if p.get("im_plan")]
    else:  # ältere Ausführung ohne Markierung: nur am Namen erkennbar
        haupt = [p for p in pakete if _versuch_von(p["paket"], namen) is None]
    zeilen = {p["paket"]: {**p, "versuche_liste": []} for p in haupt}
    for p in pakete:
        if p["paket"] in zeilen:
            continue
        ziel = _versuch_von(p["paket"], list(zeilen))
        if ziel is None:
            zeilen[p["paket"]] = {**p, "versuche_liste": []}
        else:
            zeilen[ziel]["versuche_liste"].append(p)
    schritte = WerkbankOperation.objects.filter(ausfuehrung=ausf, art="schritt")
    ungeklaert = set(schritte.filter(klaerung="ungeklaert").values_list("paket", flat=True))
    offen = {
        b.get("paket") or "root"
        for b in ausf.bedarf
        if b.get("offen") and b.get("typ") in ("fehler", "unklar")
    }
    grund: dict[str, dict] = {}
    for b in ausf.bedarf:  # in Meldungsreihenfolge: der letzte Eintrag gewinnt
        if b.get("typ") in ("fehler", "unklar"):
            grund[b.get("paket") or "root"] = {"text": b.get("text", ""), "code": b.get("code", "")}

    def stand(p: dict, abgeloest: bool) -> str:
        z = p.get("zustand") or "offen"
        if z in ("laeuft", "offen") and p["paket"] not in ungeklaert:
            if p["paket"] in offen or "root" in offen:
                return "blockiert"
            if abgeloest:
                return "abgeloest"
        return z

    ergebnis = []
    for z in zeilen.values():
        versuche = z["versuche_liste"]
        for i, v in enumerate(versuche):
            v["stand"] = stand(v, i < len(versuche) - 1)
            v["grund"] = grund.get(v["paket"])
            v["schritte"] = schritte.filter(paket=v["paket"]).count()
        z["stand"] = stand(z, False)
        if versuche and z["stand"] not in _ENDGUELTIG:
            z["stand"] = versuche[-1]["stand"]
        z["grund"] = next(
            (g for g in (grund.get(v["paket"]) for v in reversed(versuche)) if g),
            grund.get(z["paket"]),
        )
        eigene = [z["paket"], *(v["paket"] for v in versuche)]
        z["schritte"] = schritte.filter(paket__in=eigene).count()
        z["versuche_anzahl"] = len(versuche) or z.get("versuche")
        for p in (z, *versuche):
            p["stand_wort"] = PAKET_WORT.get(p["stand"], p["stand"])
        ergebnis.append(z)
    return ergebnis


# --- schritt ---------------------------------------------------------------------------------


def _abgelehnt(n: dict, ausf: WerkbankAusfuehrung, code: str, text: str, handlung: str):
    return werkbank.Ausgang(
        200,
        {
            "vertrag": werkbank_vertrag.VERTRAG,
            "operation_id": n["operation_id"],
            "erlaubt": False,
            "grund_code": code,
            "text": text,
            "naechste_handlung": handlung,
            "autorisierung_id": None,
            "statusversion": ausf.statusversion,
        },
    )


def _aktion_pruefen(ausf: WerkbankAusfuehrung, aktion_id: str) -> tuple[WerkbankAktion | None, str]:
    aktion = WerkbankAktion.objects.filter(aktion_id=aktion_id, ausfuehrung=ausf).first()
    if (
        aktion is None
        or aktion.zustand not in ("ausstehend", "angenommen")
        or aktion.revision != ausf.revision
        or aktion.zuweisung_version != ausf.zuweisung_version
        or aktion.projektbindung_version != ausf.projektbindung_version
    ):
        return None, "AKTION_UNGUELTIG"
    if aktion.operation_id is not None:
        return None, "AKTION_VERBRAUCHT"
    return aktion, ""


def _noetige_freigaben(ausf: WerkbankAusfuehrung, paket: str) -> list[dict]:
    """Offene Freigaben, die für einen Schritt in diesem Paket noch fehlen: nicht von einer an einen
    Schritt gebundenen Aktion erteilt, und für alle Pakete oder genau dieses verlangt."""
    erteilt = {
        a.ziel.get("freigabe_id")
        for a in WerkbankAktion.objects.filter(
            ausfuehrung=ausf,
            art__in=list(FREIGABE_ARTEN),
            zustand__in=("angenommen", "ausgefuehrt"),
            operation__isnull=False,
        )
    }
    return [
        b
        for b in _bedarf_offen(ausf, "freigabe")
        if b["freigabe_id"] not in erteilt and (not b.get("pakete") or paket in b["pakete"])
    ]


def wiederholung_pruefen(n: dict, ausf: WerkbankAusfuehrung, bekannt: WerkbankOperation):
    """Wiederholt die Werkbank einen erlaubten Schritt, gilt der aktuelle Stand: Ist der Ausgang
    inzwischen unklar, ist der alte Erfolg kein Startschein mehr (Vertrag 4.6)."""
    if bekannt.ausgang.get("erlaubt") and (
        ausf.zustand == "unklar" or _bedarf_offen(ausf, "unklar")
    ):
        return _abgelehnt(
            n, ausf, "UNKLAR", "Der Ausgang eines früheren Schritts ist inzwischen unklar.",
            "Nichts Neues starten; Fred klärt den Ausgang in KI-PM.",
        )  # fmt: skip
    return None


def schritt_vorab(instanz, n: dict, q: werkbank.QuellePruefung):
    """Alle Prüfungen vor der Autorisierung. Gibt eine Ablehnung zurück (wird nicht als Operation
    gespeichert, der Versuch bleibt frei) oder None, wenn der Schritt erlaubt werden darf."""
    ausf = q.ausfuehrung
    nl = n["nutzlast"]
    s = nl["schritt"]
    if s["ausfuehrung_id"] != str(ausf.ausfuehrung_id):
        raise werkbank._ungueltig(
            "Der Schrittschlüssel nennt eine andere Ausführung als die Quelle."
        )
    if ausf.zustand == "unklar" or _bedarf_offen(ausf, "unklar"):
        return _abgelehnt(
            n, ausf, "UNKLAR", "Der Ausgang eines früheren Schritts ist unklar.",
            "Nichts Neues starten; Fred klärt den Ausgang in KI-PM.",
        )  # fmt: skip
    if s["planfassung"] != ausf.planfassung:
        return _abgelehnt(
            n, ausf, "PLANFASSUNG_UNBEKANNT",
            f"Die Planfassung „{s['planfassung'][:12]}“ gilt nicht; erwartet ist "
            f"„{ausf.planfassung[:12]}“.",
            "Mit der bestätigten Planfassung neu anfragen (Abgleich und Ereignisse prüfen).",
        )  # fmt: skip
    gleich = WerkbankOperation.objects.filter(
        ausfuehrung=ausf, art="schritt", planfassung=s["planfassung"], paket=s["paket"],
        phase=s["phase"], position=s["position"],
    )  # fmt: skip
    if gleich.filter(versuch_nr=nl["versuch_nr"]).exists():
        return _abgelehnt(
            n, ausf, "VERSUCH_BEREITS_AUTORISIERT",
            "Dieser Versuch des Schritts ist schon autorisiert; eine andere operation_id "
            "ändert daran nichts.",
            "Die erste Antwort verwenden oder mit der Wiederholung derselben operation_id fragen.",
        )  # fmt: skip
    if gleich.filter(klaerung="ungeklaert").exists():
        return _abgelehnt(
            n, ausf, "VORHERIGER_VERSUCH_UNGEKLAERT",
            "Ein früherer Versuch dieses Schritts ist noch nicht geklärt.",
            "Erst den Ausgang melden (paket_status oder fehler), dann den nächsten Versuch.",
        )  # fmt: skip
    letzter = gleich.order_by("-versuch_nr").first()
    if letzter is not None and letzter.ausgang_art == "erfolg":
        return _abgelehnt(
            n, ausf, "SCHRITT_BEREITS_ERFOLGREICH",
            "Der Schritt ist schon erfolgreich; ein neuer Versuch folgt nur auf einen "
            "bekannten Fehlschlag.",
            "Mit dem nächsten Schritt weitermachen.",
        )  # fmt: skip
    erwartet = letzter.versuch_nr + 1 if letzter is not None else 1
    if nl["versuch_nr"] != erwartet:
        return _abgelehnt(
            n, ausf, "VERSUCHSNUMMER_UNGUELTIG",
            f"Der nächste Versuch dieses Schritts trägt die Nummer {erwartet}.",
            "Versuche lückenlos zählen.",
        )  # fmt: skip
    noetig = _noetige_freigaben(ausf, s["paket"])
    aktion_id = nl["owner_aktion_id"]
    if aktion_id:
        aktion, code = _aktion_pruefen(ausf, aktion_id)
        if not code and noetig:
            passend = {
                b["freigabe_id"] for b in noetig if FREIGABE_ARTEN.get(aktion.art) == b.get("art")
            }
            if aktion.ziel.get("freigabe_id") not in passend:
                code = "AKTION_UNGUELTIG"
        if code:
            return _abgelehnt(
                n, ausf, code, "Die Owner-Aktion gilt nicht, passt nicht zur Freigabe oder wurde "
                "schon verwendet.",
                "Im Abgleich die aktuelle Aktion lesen; eine Aktion wirkt höchstens einmal.",
            )  # fmt: skip
    elif noetig:
        return _abgelehnt(
            n, ausf, "FREIGABE_AUSSTEHEND", "Die erforderliche Freigabe von Fred fehlt noch.",
            "Auf die Aktion von Fred im Abgleich warten und sie als owner_aktion_id mitschicken.",
        )  # fmt: skip
    return None


def schritt(instanz, n: dict, q: werkbank.QuellePruefung, op: WerkbankOperation):
    """Den Schritt erlauben: genau eine Autorisierung je Versuch, eine Owner-Aktion wird an diese
    Wirkung gebunden."""
    ausf = q.ausfuehrung
    nl = n["nutzlast"]
    if nl["owner_aktion_id"]:
        aktion, _ = _aktion_pruefen(ausf, nl["owner_aktion_id"])
        aktion.operation = op
        aktion.zustand = "angenommen"
        aktion.save(update_fields=["operation", "zustand"])
    paket = nl["schritt"]["paket"]
    for b in ausf.bedarf:  # ein neuer Versuch erledigt nur den bekannten Fehler dieses Pakets
        if b.get("typ") == "fehler" and b.get("paket") == paket:
            b["offen"] = False
    if ausf.zustand == "plant":
        ausf.zustand = "laeuft"
    elif ausf.zustand in ("fehler", "offline", "wartet_freigabe"):
        ausf.zustand = zustand_aus_bedarf(ausf)
        if ausf.zustand == "wartet_freigabe" and not _noetige_freigaben(ausf, paket):
            ausf.zustand = "laeuft"
    ausf.save(update_fields=["zustand", "bedarf"])
    auftrag_abgleichen(ausf)
    return werkbank.Ausgang(
        200,
        {
            "vertrag": werkbank_vertrag.VERTRAG,
            "operation_id": n["operation_id"],
            "erlaubt": True,
            "grund_code": None,
            "text": "Schritt freigegeben.",
            "naechste_handlung": "Schritt ausführen und Ereignis melden.",
            "autorisierung_id": str(uuid.uuid4()),
            "statusversion": ausf.statusversion,
        },
    )


# --- ereignisse ------------------------------------------------------------------------------


def _plan_bestaetigt(instanz, ausf, daten, op) -> None:
    ausf.planfassung = daten["planfassung"]
    alt = {p["paket"]: p for p in ausf.pakete}
    # ``im_plan`` trennt Planpakete von Versuchen, die die Werkbank unter eigenem Namen meldet.
    ausf.pakete = [{**alt.get(p["paket"], {}), **p, "im_plan": True} for p in daten["pakete"]]


def _paket_status(instanz, ausf, daten, op) -> None:
    felder = {k: daten[k] for k in ("zustand", "versuche", "handlungsbedarf", "text") if k in daten}
    felder.setdefault("handlungsbedarf", False)
    _paket(ausf, daten["paket"], **felder)
    if daten["zustand"] in ABGESCHLOSSEN:
        _klaeren(ausf, daten["paket"], "fehlschlag" if daten["zustand"] == "fehler" else "erfolg")


def _rueckfrage(instanz, ausf, daten, op) -> None:
    if any(b.get("frage_id") == daten["frage_id"] for b in ausf.bedarf):
        return
    ausf.bedarf = [
        *ausf.bedarf,
        {
            "typ": "frage",
            "frage_id": daten["frage_id"],
            "paket": daten["paket"],
            "text": daten["text"],
            "offen": True,
        },
    ]
    _paket(ausf, daten["paket"], handlungsbedarf=True)
    _blockade(ausf, "wartet_antwort")
    _verlauf(instanz, ausf, Meldung.RUECKFRAGE, daten["text"], "Rückfrage der Werkbank")


def _abgedeckt(alt: list[str], neu: list[str]) -> bool:
    """Deckt eine Bitte für die Pakete ``neu`` die ältere für ``alt`` ab? Jedes alte Paket ist eines
    der neuen oder ein Versuch davon (Versuchsname, ``_versuch_von``). Eine Bitte ohne Pakete deckt
    nur eine ebenso paketlose ab."""
    if not alt or not neu:
        return not alt and not neu
    return all(p in neu or _versuch_von(p, neu) is not None for p in alt)


def ueberholte_freigaben_schliessen(ausf: WerkbankAusfuehrung) -> bool:
    """SKP-01: Eine offene Freigabe-Bitte ist überholt, sobald eine jüngere Bitte gleicher Art
    (``start`` bzw. ``uebernahme``) ihre Pakete abdeckt; der Vertrag kennt kein Zurückziehen. Sie
    wird mit Vermerk ``ersetzt_durch`` geschlossen. Erteilte Freigaben auf sie bleiben unberührt
    (die Werkbank weist sie mit Grund ab). Gibt zurück, ob sich etwas geändert hat."""
    bitten = [b for b in ausf.bedarf if b.get("typ") == "freigabe"]
    geaendert = False
    for i, alt in enumerate(bitten):
        if not alt.get("offen"):
            continue
        neuere = next(
            (
                b
                for b in reversed(bitten[i + 1 :])
                if b.get("art") == alt.get("art")
                and _abgedeckt(alt.get("pakete", []), b.get("pakete", []))
            ),
            None,
        )
        if neuere is not None:
            alt["offen"] = False
            alt["ersetzt_durch"] = neuere["freigabe_id"]
            geaendert = True
    if not geaendert:
        return False
    offen = {b.get("paket") for b in ausf.bedarf if b.get("offen") and b.get("paket")}
    for b in ausf.bedarf:
        if b.get("offen") and b.get("typ") == "freigabe":
            offen.update(b.get("pakete", []))
    for p in ausf.pakete:
        if p.get("handlungsbedarf") and p["paket"] not in offen:
            p["handlungsbedarf"] = False
    ausf.bedarf = list(ausf.bedarf)
    if ausf.zustand == "wartet_freigabe":
        ausf.zustand = zustand_aus_bedarf(ausf)
    return True


def _freigabe_noetig(instanz, ausf, daten, op) -> None:
    if any(b.get("freigabe_id") == daten["freigabe_id"] for b in ausf.bedarf):
        return
    ausf.bedarf = [
        *ausf.bedarf,
        {
            "typ": "freigabe",
            "freigabe_id": daten["freigabe_id"],
            "art": daten["art"],
            "pakete": daten.get("pakete", []),
            "kandidaten": daten.get("kandidaten", []),
            "pruefstand": daten.get("pruefstand", ""),
            "grund": daten["grund"],
            "offen": True,
        },
    ]
    ueberholte_freigaben_schliessen(ausf)
    for name in daten.get("pakete", []):
        _paket(ausf, name, handlungsbedarf=True)
    _blockade(ausf, "wartet_freigabe")
    _verlauf(
        instanz, ausf, Meldung.HINDERNIS, f"Freigabe nötig: {daten['grund']}", "Freigabe nötig"
    )


def _fehler(instanz, ausf, daten, op) -> None:
    paket = daten.get("paket", "root")
    unklar = bool(daten.get("ausgang_unklar"))
    ausf.bedarf = [
        *ausf.bedarf,
        {
            "typ": "unklar" if unklar else "fehler",
            "paket": paket,
            "code": daten["code"],
            "text": daten["text"],
            "offen": True,
        },
    ]
    if paket != "root" or any(p["paket"] == paket for p in ausf.pakete):
        _paket(ausf, paket, zustand="fehler", handlungsbedarf=True)
    if unklar:
        _zustand_setzen(ausf, "unklar")  # die ungeklärten Versuche bleiben ungeklärt
    else:
        _klaeren(ausf, paket)
        _blockade(ausf, "fehler")
    _verlauf(
        instanz, ausf, Meldung.FEHLER,
        ("Ausgang unklar: " if unklar else "Fehler: ") + daten["text"],
        "Ausgang unklar" if unklar else "Fehler der Werkbank",
    )  # fmt: skip


def _offline(instanz, ausf, daten, op) -> None:
    _zustand_setzen(ausf, "offline")


def _lebenszeichen(instanz, ausf, daten, op) -> None:
    return None


EFFEKTE = {
    "lebenszeichen": _lebenszeichen,
    "plan_bestaetigt": _plan_bestaetigt,
    "paket_status": _paket_status,
    "rueckfrage": _rueckfrage,
    "freigabe_noetig": _freigabe_noetig,
    "fehler": _fehler,
    "offline_phase": _offline,
}


def ereignis_verarbeiten(art: str, instanz, n: dict, q: werkbank.QuellePruefung, op):
    """Eine Ereignismeldung speichern (original, nach Inhaltshash wiederholbar) und, wenn sie zur
    aktuellen Fassung gehört, auf die Projektion wirken lassen."""
    nutzlast = n["nutzlast"]
    ausf: WerkbankAusfuehrung = q.ausfuehrung
    inhalt = werkbank_vertrag.sha256_hex(nutzlast)
    vorhanden = WerkbankEreignis.objects.filter(
        instanz=instanz, ereignis_id=nutzlast["ereignis_id"]
    ).first()
    if vorhanden is not None:
        if vorhanden.inhalt_sha256 != inhalt:
            raise werkbank.WerkbankFehler(
                409,
                "WIDERSPRUCH_WIEDERHOLUNG",
                "Diese ereignis_id kam schon mit anderem Inhalt.",
                "Neue ereignis_id und die nächste Sequenznummer verwenden.",
            )
        ergebnis = "wiederholt"
    else:
        if WerkbankEreignis.objects.filter(ausfuehrung=ausf, sequenz=nutzlast["sequenz"]).exists():
            raise werkbank.WerkbankFehler(
                409,
                "ZUSTANDSKONFLIKT",
                "Diese Sequenznummer ist in der Ausführung schon vergeben.",
                "Abgleich abrufen und mit der nächsten freien Sequenznummer fortfahren.",
            )
        version = nutzlast["statusversion"]
        historisch = (
            bool(q.veraltet)
            or not q.aktiv
            or (version is not None and version < ausf.statusversion)
        )
        WerkbankEreignis.objects.create(
            instanz=instanz,
            ereignis_id=nutzlast["ereignis_id"],
            art=art,
            ausfuehrung=ausf,
            projektbindung=q.projektbindung,
            sequenz=nutzlast["sequenz"],
            statusversion=version,
            inhalt_sha256=inhalt,
            rueckmeldung=nutzlast,
            operation=op,
            verarbeitung="historisch" if historisch else "angewendet",
        )
        ergebnis = "historisch_quittiert" if historisch else "verarbeitet"
        if not historisch:
            rueck = EFFEKTE[art](instanz, ausf, nutzlast["ereignis"]["daten"], op)
            if rueck == "historisch":
                WerkbankEreignis.objects.filter(
                    instanz=instanz, ereignis_id=nutzlast["ereignis_id"]
                ).update(verarbeitung="historisch")
                ergebnis = "historisch_quittiert"
            else:
                if ausf.zustand == "offline" and art != "offline_phase":
                    ausf.zustand = zustand_aus_bedarf(ausf)  # der Bedarf von vorher gilt weiter
                if version is not None:
                    ausf.statusversion = max(ausf.statusversion, version)
                ausf.save()
                auftrag_abgleichen(ausf)
    ausf.refresh_from_db()
    return werkbank.Ausgang(
        200,
        {
            "vertrag": werkbank_vertrag.VERTRAG,
            "operation_id": n["operation_id"],
            "ereignis_id": nutzlast["ereignis_id"],
            "ergebnis": ergebnis,
            "statusversion": ausf.statusversion,
            "technischer_zustand": werkbank._vertragszustand(ausf.zustand),
            "haltanforderung": ausf.halt_zweck or None,
            "naechste_handlung": (
                "Keine; Meldung gehört zu einer älteren Fassung."
                if ergebnis == "historisch_quittiert"
                else "Weiterarbeiten wie bisher."
            ),
        },
    )


def _handler(art: str):
    def handler(instanz, n, q, op):
        return ereignis_verarbeiten(art, instanz, n, q, op)

    return handler


werkbank.HANDLER["schritt"] = schritt
for _art in EFFEKTE:
    werkbank.HANDLER[_art] = _handler(_art)

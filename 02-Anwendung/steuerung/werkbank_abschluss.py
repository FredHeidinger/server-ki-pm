"""Werkbank-Anbindung WP7: Abschlussregel (Auftrag 8, 7.6) und Wiederherstellung (Auftrag 7.7).
Teil von ``werkbank.py``.

Der Server prüft Vollständigkeit und Herkunft des ``abschluss_bericht``, nicht die Wahrheit der
Tests; er behauptet nie, Tests selbst ausgeführt zu haben. Ein Halt, der zuerst gespeichert wurde,
geht vor: Die Fakten werden protokolliert, der Auftrag wird nicht erledigt. Der Auftrag wird über
dieselbe Abschlussstelle wie bei Chats erledigt (``dienst.ergebnis_einreichen``), nur nicht über
deren gesperrten alten Weg.
"""

import re

from django.utils import timezone

from . import dienst, werkbank, werkbank_lauf
from .models import WerkbankAusfuehrung

ENDGUELTIG = ("erledigt", "ersetzt", "zerlegt", "verworfen")
OFFENER_BEDARF = ("frage", "freigabe", "fehler", "unklar")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _maengel(ausf: WerkbankAusfuehrung, d: dict) -> list[str]:
    maengel: list[str] = []
    if d["planfassung"] != ausf.planfassung:
        maengel.append("Die Planfassung des Berichts ist nicht die bestätigte.")
    # Kriterien: alle ursprünglichen, einzeln behandelt.
    erwartet = [k["id"] for k in ausf.snapshot["abnahmekriterien"]]
    ids = [k["kriterium_id"] for k in d["kriterien"]]
    for kid in sorted({i for i in ids if ids.count(i) > 1}):
        maengel.append(f"Das Kriterium {kid} steht mehrfach im Bericht.")
    behandelt = {k["kriterium_id"]: k for k in d["kriterien"]}
    for kid in erwartet:
        k = behandelt.get(kid)
        if k is None:
            maengel.append(f"Das Kriterium {kid} wurde nicht behandelt.")
        elif k["behandlung"] == "nicht_erfuellt":
            maengel.append(f"Das Kriterium {kid} ist nicht erfüllt.")
        elif k["behandlung"] == "bereits_erfuellt" and not k.get("commit"):
            maengel.append(f"Das Kriterium {kid} ist „bereits erfüllt“ ohne Nachweis-Commit.")
    for kid in behandelt:
        if kid not in erwartet:
            maengel.append(f"Das Kriterium {kid} gehört nicht zum Auftrag.")
    nur_bestand = all(k["behandlung"] in ("bereits_erfuellt", "entfaellt") for k in d["kriterien"])
    # Paketplan: endgültig und mit allen dem Server bekannten Paketen.
    namen = [p["paket"] for p in d["paketplan"]]
    for name in sorted({i for i in namen if namen.count(i) > 1}):
        maengel.append(f"Das Paket {name} steht mehrfach im Paketplan.")
    plan = {p["paket"]: p for p in d["paketplan"]}
    for name, p in plan.items():
        if p["zustand"] == "zerlegt" and not any(
            q.get("zerlegt_aus") == name for q in plan.values()
        ):
            maengel.append(f"Das zerlegte Paket {name} hat keine Teilpakete im Plan.")
        if p["zustand"] == "ersetzt" and not any(
            name in q.get("ersetzt", []) for q in plan.values()
        ):
            maengel.append(f"Das ersetzte Paket {name} hat kein Ersatzpaket im Plan.")
    unbekannt = {
        o.paket
        for o in ausf.operationen.filter(art="schritt", klaerung="ungeklaert")
        if o.paket != "root" and o.paket not in plan
    }
    for name in sorted(unbekannt):
        maengel.append(f"Ein Schritt in {name} wurde gestartet, das Paket fehlt im Paketplan.")
    for p in ausf.pakete:
        if p["paket"] not in plan:
            maengel.append(f"Das Paket {p['paket']} fehlt im endgültigen Paketplan.")
    for name, p in plan.items():
        if p["zustand"] not in ENDGUELTIG:
            maengel.append(f"Das Paket {name} ist nicht abgeschlossen ({p['zustand']}).")
    # Integrationen und Prüfungen der erledigten Pakete.
    integriert = {i["paket"] for i in d["integrationen"]}
    letzte: dict[tuple, str] = {}
    bestanden: set[str] = set()
    for pr in d["pruefungen"]:
        letzte[(pr["paket"], pr["art"])] = pr["ergebnis"]
    for (paket, art), ergebnis in letzte.items():
        if ergebnis == "bestanden":
            bestanden.add(paket)
        else:
            maengel.append(f"Die Prüfung „{art}“ von {paket} ist fehlgeschlagen.")
    if not nur_bestand:
        for name, p in plan.items():
            if p["zustand"] != "erledigt":
                continue
            if name not in integriert:
                maengel.append(f"Für das erledigte Paket {name} fehlt die Integration.")
            if name not in bestanden:
                maengel.append(f"Für das erledigte Paket {name} fehlt eine bestandene Prüfung.")
    gesamt = d["gesamtpruefung"]
    if gesamt is not None and gesamt["ergebnis"] != "bestanden":
        maengel.append("Die Gesamtprüfung ist fehlgeschlagen.")
    if not nur_bestand and not d["integrationen"]:
        maengel.append("Es fehlt eine tatsächliche Integration.")
    if not nur_bestand and gesamt is None:
        maengel.append("Es fehlt die Gesamtprüfung.")
    maengel += [f"Offene Arbeit: {t}" for t in d["offene_arbeiten"]]
    maengel += [f"Ungeklärte Wirkung: {t}" for t in d["ungeklaerte_wirkungen"]]
    if ausf.zustand in werkbank_lauf.WARTEND or any(
        b.get("offen") and b.get("typ") in OFFENER_BEDARF for b in ausf.bedarf
    ):
        maengel.append(
            "Es wartet noch eine Rückfrage, Freigabe, ein Fehler oder ein unklarer Ausgang."
        )
    return maengel


def _abschluss(instanz, ausf, d, op):
    if ausf.zustand == "abschluss_gemeldet":
        raise werkbank.WerkbankFehler(
            409,
            "ABSCHLUSS_GESPEICHERT",
            "Der Abschluss dieser Ausführung ist schon gespeichert.",
            "Nichts weiter melden; die Ausführung ist abgeschlossen.",
        )
    if ausf.halt_zweck:
        return None  # Halt geht vor: Fakten sind protokolliert, aber kein ERLEDIGT
    maengel = _maengel(ausf, d)
    if maengel:
        raise werkbank.WerkbankFehler(
            409,
            "ABSCHLUSS_UNVOLLSTAENDIG",
            "Der Abschlussbericht ist unvollständig oder widersprüchlich.",
            "Die genannten Mängel beheben und den Bericht neu senden.",
            {"maengel": [m[:300] for m in maengel[:20]]},
        )
    ausf.zustand = "abschluss_gemeldet"
    ausf.abschluss_bestaetigt_am = timezone.now()
    dateien = [f"{i['paket']}@{i['commit']}" for i in d["integrationen"]] or [
        f"{k['kriterium_id']}@{k['commit']}" for k in d["kriterien"] if k.get("commit")
    ]
    pruefungen = [
        f"{p['paket']} {p['art']}: {p['beschreibung'][:200]}"
        for p in d["pruefungen"]
        if p["ergebnis"] == "bestanden"
    ]
    if d["gesamtpruefung"]:
        pruefungen.append("Gesamtprüfung: " + d["gesamtpruefung"]["beschreibung"][:200])
    dienst.ergebnis_einreichen.__wrapped__(
        dienst.Akteur.aus_bindung(instanz.bindung, ausf.auftrag.projekt),
        ausf.auftrag,
        ausf.revision,
        zusammenfassung=(
            f"Abschluss der Werkbank: {len(d['kriterien'])} Kriterien behandelt, "
            f"{len(d['integrationen'])} Integration(en), Gesamtprüfung "
            + ("bestanden." if d["gesamtpruefung"] else "nicht nötig.")
        ),
        dateien="\n".join(dateien) or "keine Änderung nötig@vorhandener Stand",
        pruefungen="\n".join(pruefungen),
    )
    return None


def unbekannte_ausfuehrungen(instanz, bekannt: str | None) -> list[dict]:
    """Nennt die Werkbank dem Server Ausführungen, die er nicht kennt (etwa nach einer
    Serverwiederherstellung), steht ein Abgleichkonflikt in der Antwort. Der Server leitet daraus
    nichts ab und baut nichts neu."""
    if not bekannt:
        return []
    kennungen = [k.strip() for k in bekannt.split(",") if k.strip()]
    if len(kennungen) > werkbank.SEITE_MAX or not all(_UUID.fullmatch(k) for k in kennungen):
        raise werkbank._ungueltig("„ausfuehrungen“ braucht höchstens 100 kleingeschriebene UUIDs.")
    vorhanden = {
        str(u)
        for u in WerkbankAusfuehrung.objects.filter(
            ausfuehrung_id__in=kennungen, projektbindung__instanz=instanz
        ).values_list("ausfuehrung_id", flat=True)
    }
    return [
        {
            "ausfuehrung_id": k,
            "code": "AUSFUEHRUNG_UNBEKANNT",
            "text": "Der Server kennt diese Ausführung nicht (Wiederherstellung?). Die Werkbank "
            "startet daraus nichts Neues; Fred klärt den Stand in KI-PM.",
        }
        for k in kennungen
        if k not in vorhanden
    ]


werkbank_lauf.EFFEKTE["abschluss_bericht"] = _abschluss
werkbank.HANDLER["abschluss_bericht"] = werkbank_lauf._handler("abschluss_bericht")

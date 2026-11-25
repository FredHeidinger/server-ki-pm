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

from . import dienst, werkbank, werkbank_lauf, werkbank_vertrag
from .models import WerkbankAusfuehrung, WerkbankEreignis

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
    if ausf.vertrag == werkbank_vertrag.VERTRAG_2:
        maengel += _repo_maengel(ausf, d, plan)
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


def _repo_maengel(ausf: WerkbankAusfuehrung, d: dict, plan: dict) -> list[str]:
    """Fassung 2: jedes erledigte Paket hat ein Repository aus dem bestätigten Plan
    (``plan_bestaetigt``), jede Integration gehört zu diesem Repository und zum Zielverbund.
    Ein Paket, das erst im Bericht auftaucht, meldet die Werkbank vorher mit einem neuen Plan."""
    from . import werkbank_verbund

    bekannt = werkbank_verbund.repo_ids(ausf)
    paket_repo = {p["paket"]: p.get("repo_id") for p in ausf.pakete if p.get("repo_id")}
    maengel = [
        f"Das Paket {name} hat kein Repository aus dem bestätigten Plan."
        for name, p in plan.items()
        if p["zustand"] == "erledigt" and name not in paket_repo
    ]
    for i in d["integrationen"]:
        if i["repo_id"] not in bekannt:
            maengel.append(f"Die Integration von {i['paket']} nennt ein fremdes Repo.")
        elif i["paket"] not in paket_repo:
            if i["paket"] not in plan:
                maengel.append(f"Die Integration von {i['paket']} hat kein Paket im Plan.")
        elif paket_repo[i["paket"]] != i["repo_id"]:
            maengel.append(
                f"Die Integration von {i['paket']} liegt in {i['repo_id']}, das Paket gehört "
                f"zu Repo {paket_repo[i['paket']]}."
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
    # Altbestand vor SKP-01: eine von einer jüngeren Bitte überholte Bitte stand noch offen.
    werkbank_lauf.ueberholte_freigaben_schliessen(ausf)
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
    dateien = [
        f"{i['paket']}{' (' + i['repo_id'] + ')' if i.get('repo_id') else ''}@{i['commit']}"
        for i in d["integrationen"]
    ] or [f"{k['kriterium_id']}@{k['commit']}" for k in d["kriterien"] if k.get("commit")]
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
    from .models import WerkbankKonflikt

    for k in kennungen:
        if k not in vorhanden:
            # Festhalten (KIPM-35, B15): bis Fred klärt, übernimmt diese Werkbank nichts.
            konflikt, neu = WerkbankKonflikt.objects.get_or_create(
                instanz=instanz, ausfuehrung_id=k
            )
            if not neu:
                konflikt.save(update_fields=["zuletzt_am"])
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


# --- Anzeige (KIPM-28, Paket KIPM-04) ---------------------------------------------------------

BEHANDLUNG_TEXT = {
    "erfuellt": ("erfüllt", "ok"),
    "bereits_erfuellt": ("bereits erfüllt", "ok"),
    "entfaellt": ("entfällt", "aus"),
    "nicht_erfuellt": ("nicht erfüllt", "bad"),
}


def abschlussbericht(ausf: WerkbankAusfuehrung) -> dict | None:
    """Die Daten des angenommenen Abschlussberichts dieser Ausführung, sonst None. Ein abgewiesener
    Bericht wird nie gespeichert (die Transaktion fällt zurück); ein historisch quittierter zählt
    nicht."""
    e = (
        WerkbankEreignis.objects.filter(
            ausfuehrung=ausf, art="abschluss_bericht", verarbeitung="angewendet"
        )
        .order_by("-sequenz")
        .first()
    )
    if e is None:
        return None
    return (e.rueckmeldung.get("ereignis") or {}).get("daten") or None


def kriterien_nachweis(ausf: WerkbankAusfuehrung) -> list[dict] | None:
    """„Kriterium · Test · Ergebnis“ für jedes Kriterium des Snapshots, aus dem angenommenen
    Abschlussbericht. Der Test ist der Nachweistext der Werkbank (`kipm-werkbank/1`,
    `kriterien[].nachweis`); KI-PM prüft ihn nicht nach. Ohne Bericht None."""
    daten = abschlussbericht(ausf)
    if daten is None:
        return None
    gemeldet = {k.get("kriterium_id"): k for k in daten.get("kriterien") or []}
    zeilen = []
    for k in ausf.snapshot.get("abnahmekriterien") or []:
        g = gemeldet.get(k["id"])
        nachweis = ((g or {}).get("nachweis") or "").strip()
        text, art = (
            BEHANDLUNG_TEXT.get(g.get("behandlung"), (g.get("behandlung") or "", "aus"))
            if g
            else ("nicht gemeldet", "bad")
        )
        zeilen.append(
            {
                "id": k["id"],
                "text": k.get("text", ""),
                "test": nachweis,
                "zuordnung": bool(nachweis),
                "ergebnis": text,
                "art": art,
                "commit": (g or {}).get("commit") or "",
            }
        )
    return zeilen


def _beobachtung(ausf: WerkbankAusfuehrung | None, gefragt: bool) -> dict:
    if ausf is None:
        return {"stand": "unbekannt", "auftrag": None, "am": None}
    return {
        "stand": "fragt" if gefragt else "selbst",
        "auftrag": ausf.auftrag.nummer,
        "am": ausf.abschluss_bestaetigt_am or ausf.angelegt_am,
    }


def vollmacht(bindung) -> dict:
    """Was die Werkbank an dieser Projektbindung ohne Freds Klick tut, beobachtet am jüngsten Lauf,
    der die Stelle erreicht hat. `kipm-werkbank/1` meldet die Einstellung der Werkbank nicht;
    darum nur Beobachtung, nie eine Zusage.

    Start: beobachtet, sobald ein Schritt an einem geplanten Paket (nicht `root`, also nach der
    Vorplanung) autorisiert wurde oder die Werkbank eine Startfreigabe erbeten hat.
    Übernahme: beobachtet mit einem gespeicherten Abschluss oder einer erbetenen
    Übernahmefreigabe."""
    start = uebernahme = None
    start_gefragt = uebernahme_gefragt = False
    for ausf in (
        WerkbankAusfuehrung.objects.select_related("auftrag")
        .filter(projektbindung=bindung)
        .order_by("-id")
    ):
        arten = {b.get("art") for b in ausf.bedarf if b.get("typ") == "freigabe"}
        if start is None and (
            "start" in arten
            or ausf.operationen.filter(art="schritt").exclude(paket__in=("", "root")).exists()
        ):
            start, start_gefragt = ausf, "start" in arten
        if uebernahme is None and (
            "uebernahme" in arten or ausf.zustand in ("abschluss_gemeldet", "abgeschlossen")
        ):
            uebernahme, uebernahme_gefragt = ausf, "uebernahme" in arten
        if start is not None and uebernahme is not None:
            break
    s = _beobachtung(start, start_gefragt)
    u = _beobachtung(uebernahme, uebernahme_gefragt)
    teile = {
        "unbekannt": ("Start noch nicht beobachtet", "Übernahme noch nicht beobachtet"),
        "fragt": ("Start fragt hier", "Übernahme fragt hier"),
        "selbst": ("startet ohne Frage", "übernimmt ohne Frage"),
    }
    if s["stand"] == u["stand"] == "unbekannt":
        satz = "Ohne deinen Klick: noch nicht beobachtet (kein Lauf in diesem Projekt)."
    else:
        satz = f"Ohne deinen Klick: {teile[s['stand']][0]}; {teile[u['stand']][1]}."
    return {"start": s, "uebernahme": u, "satz": satz}


werkbank_lauf.EFFEKTE["abschluss_bericht"] = _abschluss
werkbank.HANDLER["abschluss_bericht"] = werkbank_lauf._handler("abschluss_bericht")

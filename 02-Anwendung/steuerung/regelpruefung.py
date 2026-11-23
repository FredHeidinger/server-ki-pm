"""Rein lesende Prüfung produktiver Daten und historischer Auftragskopien."""

import re
from collections import defaultdict, deque

from . import auftragsregeln, ordnersperren
from .models import Auftrag, Ergebnis, Meldung

BEZUG = re.compile(r"\b[A-Z][A-Z0-9]{0,7}-[1-9][0-9]*\b")
BEDARF = re.compile(
    r"\b(?:abhängig|abhängigkeit|anschluss|aus|benötig\w*|braucht|durch|fehlt|"
    r"geliefert|liefer\w*|offen|rückgab\w*|über|übernimmt|verdraht\w*|"
    r"voraussetz\w*|von|wartet?)\b",
    re.IGNORECASE,
)


def _implizite_bezuege(text, nach_nummer):
    for zeile in text.splitlines():
        for satz in re.split(r"(?<=[.!?;])\s+", zeile):
            if BEDARF.search(satz):
                for nummer in dict.fromkeys(BEZUG.findall(satz)):
                    if nummer in nach_nummer:
                        yield nach_nummer[nummer], satz.strip()


def _kurzer_weg(graph, start, ziel):
    vorgaenger = {start: None}
    offen = deque([start])
    while offen:
        knoten = offen.popleft()
        if knoten == ziel:
            weg = []
            while knoten is not None:
                weg.append(knoten)
                knoten = vorgaenger[knoten]
            return list(reversed(weg))
        for nachbar in sorted(graph.get(knoten, ())):
            if nachbar not in vorgaenger:
                vorgaenger[nachbar] = knoten
                offen.append(nachbar)
    return []


def _kreiswege(graph):
    # Ein kürzester Rückweg je Kante zeigt auch gegenseitige Einzelabhängigkeiten.
    gesehen = set()
    for auftrag, anbieter in sorted(graph.items()):
        for quelle in sorted(anbieter):
            weg = _kurzer_weg(graph, quelle, auftrag)
            if not weg:
                continue
            ring = [auftrag, *weg[:-1]]
            position = ring.index(min(ring))
            ring = tuple(ring[position:] + ring[:position])
            if ring not in gesehen:
                gesehen.add(ring)
                yield [*ring, ring[0]]


def _alte_kanten(auftrag, revision, graph, nach_nummer, melde):
    hinweise = {}
    if not revision.kriterienregeln:
        for nr, text in enumerate(revision.kriterien, start=1):
            for quelle, satz in _implizite_bezuege(text, nach_nummer):
                if quelle.pk == auftrag.pk or quelle.pk in graph.get(auftrag.pk, ()):
                    continue
                beleg = {"auftrag": auftrag.nummer, "revision": revision.nummer, "kriterium": nr}
                hinweise[quelle.pk] = beleg | {"text": satz}
                melde(
                    "R1",
                    "kriterium_nicht_erfuellbar",
                    [auftrag],
                    "Im Altkriterium steht ein möglicher Lieferauftrag ohne hinterlegten Bezug.",
                    "hinweis_altbestand",
                    kriterium=nr,
                    text=text,
                    anbieter=quelle.nummer,
                )
    blocker = auftrag.meldungen.filter(
        revision_nr=revision.nummer,
        historisch=False,
        art__in=(*Meldung.BLOCKIERENDE, Meldung.ANTWORT),
    ).first()
    if auftrag.zustand == Auftrag.BLOCKIERT and blocker and blocker.art in Meldung.BLOCKIERENDE:
        for quelle, text in _implizite_bezuege(blocker.text, nach_nummer):
            if quelle.pk != auftrag.pk and quelle.pk not in graph.get(auftrag.pk, ()):
                hinweise.setdefault(
                    quelle.pk, {"auftrag": auftrag.nummer, "meldung": blocker.pk, "text": text}
                )
    return hinweise


def _festfahrbefund(auftrag, melde):
    offene_frage = auftragsregeln.offene_festfahrfrage(auftrag)
    if offene_frage:
        melde(
            "R5",
            "festgefahren",
            [auftrag],
            offene_frage.text,
            "wartet_auf_fred",
            meldung=offene_frage.pk,
        )
        return
    alte_sitzungen = list(
        auftrag.meldungen.filter(
            revision_nr=auftrag.revision_nr,
            historisch=False,
            art=Meldung.SITZUNGSENDE,
            kriterien_erfuellt__isnull=True,
        ).values_list("pk", flat=True)[:3]
    )
    if (
        len(alte_sitzungen) == 3
        and not auftrag.meldungen.filter(
            revision_nr=auftrag.revision_nr, historisch=False, kriterien_erfuellt__isnull=False
        ).exists()
    ):
        melde(
            "R5",
            "festgefahren",
            [auftrag],
            "Mindestens drei alte Sitzungsenden ohne Ergebnis; kriterienscharfer Zuwachs ist "
            "für diesen Altbestand nicht belegt.",
            "hinweis_altbestand",
            meldungen=alte_sitzungen,
        )


def projekt_pruefen(projekt):
    """Befunde mit Revisionen, gemeldeten Ist-Daten und kenntlichen Altdatenhinweisen."""
    auftraege = list(projekt.auftraege.select_related("projekt", "modul").order_by("pk"))
    nach_id = {a.pk: a for a in auftraege}
    nach_nummer = {nummer: a for a in auftraege for nummer in (a.nummer, a.alte_nummer)}
    graph = auftragsregeln.abhaengigkeiten(projekt.pk)
    ergaenzt = {pk: set(quellen) for pk, quellen in graph.items()}
    altbelege = {}
    ordnergruppen = defaultdict(list)
    sammelissues = defaultdict(list)
    menschenschritte = []
    befunde = []

    def melde(regel, code, betroffene, grund, status="verstoss", **beleg):
        befunde.append(
            {
                "regel": regel,
                "code": code,
                "status": status,
                "grund": grund,
                "auftraege": [a.nummer for a in betroffene],
                **beleg,
            }
        )

    for auftrag in auftraege:
        if auftrag.ist_endzustand:
            continue
        rev = auftrag.aktuelle_revision
        for text in auftragsregeln.kriterien_fehler(auftrag, rev):
            regel = "R3" if "Art Integration" in text else "R1"
            code = "integration_erforderlich" if regel == "R3" else "kriterium_nicht_erfuellbar"
            melde(regel, code, [auftrag], text, revision=rev.nummer)
        for anbieter, beleg in _alte_kanten(auftrag, rev, graph, nach_nummer, melde).items():
            ergaenzt.setdefault(auftrag.pk, set()).add(anbieter)
            altbelege[auftrag.pk, anbieter] = beleg
        if auftrag.typ == Auftrag.TYP_INTEGRATION and auftrag.zustand == Auftrag.IN_ARBEIT:
            offen = list(
                auftrag.abhaengigkeiten.select_related("vorgaenger__projekt").exclude(
                    vorgaenger__zustand=Auftrag.ERLEDIGT
                )
            )
            if offen:
                melde(
                    "R3",
                    "integration_vor_vorgaengern",
                    [auftrag],
                    "Integration läuft, obwohl Vorgänger noch nicht erledigt sind.",
                    vorgaenger=[eintrag.vorgaenger.nummer for eintrag in offen],
                )
        pfad = ordnersperren.pfadschluessel(rev.arbeitsort)
        if (
            pfad
            and auftrag.zustand == Auftrag.IN_ARBEIT
            and auftrag.hat_bearbeiter
            and auftrag.ausfuehrungsweg == Auftrag.WEG_CHAT
        ):
            letzte_art = (
                auftrag.meldungen.filter(
                    revision_nr=rev.nummer,
                    historisch=False,
                    art__in=(Meldung.SITZUNGSENDE, Meldung.FORTSCHRITT),
                )
                .values_list("art", flat=True)
                .first()
            )
            if letzte_art != Meldung.SITZUNGSENDE:
                ordnergruppen[pfad].append(auftrag)
        if auftrag.zustand in Auftrag.BEARBEITER_ZUSTAENDE:
            _festfahrbefund(auftrag, melde)
        if auftrag.fremdbefund_sammelissue and auftrag.modul_id:
            sammelissues[auftrag.modul.kennung].append(auftrag)
        if auftragsregeln.menschenschritt(auftrag, rev):
            menschenschritte.append(auftrag.nummer)

    formal = {tuple(weg) for weg in _kreiswege(graph)}
    kombinierte = {tuple(weg) for weg in _kreiswege(ergaenzt)}
    for weg in sorted(formal | kombinierte):
        alt = [altbelege[kante] for kante in zip(weg, weg[1:], strict=False) if kante in altbelege]
        melde(
            "R2",
            "abhaengigkeitskreis",
            [nach_id[pk] for pk in weg[:-1]],
            "Geschlossener Abhängigkeitsweg.",
            "hinweis_altbestand" if alt else "verstoss",
            weg=[nach_id[pk].nummer for pk in weg],
            altbelege=alt,
        )
    for schluessel, gruppe in sorted(ordnergruppen.items()):
        if len(gruppe) > 1:
            sperre = ordnersperren.belegung(schluessel)
            melde(
                "R4",
                "ordner_doppelt_belegt",
                gruppe,
                "Mehrere laufende Chat-Aufträge sind demselben Pfad zugeordnet.",
                "hinweis_altbestand",
                pfad=schluessel.split(":", 1)[1],
                aktiver_halter=(
                    sperre.auftrag.nummer if sperre and sperre.auftrag_id in nach_id else None
                ),
            )
    for modul, gruppe in sorted(sammelissues.items()):
        if len(gruppe) > 1:
            melde(
                "R6",
                "sammelissue_doppelt",
                gruppe,
                "Das Modul hat mehrere offene Sammelissues für fremde Prüfbefunde.",
                modul=modul,
            )
    for ergebnis in (
        Ergebnis.objects.filter(auftrag__projekt=projekt)
        .exclude(fremde_befunde=[])
        .select_related("auftrag__projekt")
    ):
        for befund in ergebnis.fremde_befunde:
            issue = nach_nummer.get(befund.get("issue"))
            if (
                not issue
                or not issue.fremdbefund_sammelissue
                or (not issue.modul or issue.modul.kennung != befund.get("modul"))
            ):
                melde(
                    "R6",
                    "fremdbefund_ohne_eigentuemerissue",
                    [ergebnis.auftrag],
                    "Das gemeldete Issue ist dem Eigentümermodul nicht mehr zugeordnet.",
                    ergebnis=ergebnis.pk,
                    modul=befund.get("modul"),
                    issue=befund.get("issue"),
                )
    return {
        "ok": True,
        "projekt": projekt.kennung,
        "gepruefte_regeln": [f"R{nr}" for nr in range(1, 8)],
        "auftraege_geprueft": len(auftraege),
        "befunde": befunde,
        "wartet_auf_fred": menschenschritte,
    }

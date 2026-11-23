"""Erfüllbarkeit der Abnahmekriterien und gemeinsamer Abhängigkeitsgraph."""

from collections import defaultdict

from django.db.models import F, OuterRef, Q, Subquery

from .models import Abhaengigkeit, Auftrag, Entscheidung, Lieferbedarf, Meldung, Modul, Revision


def kriterien(revision):
    return revision.kriterienregeln or [
        {"text": text, "erfuellbar_durch": "eigen", "nur_mensch": False}
        for text in revision.kriterien
    ]


def kriterien_anzeigen(revision):
    interne_felder = {"auftrag_id", "entscheidung_id", "modul_id"}
    return [
        {"nummer": nr, **{k: v for k, v in kriterium.items() if k not in interne_felder}}
        for nr, kriterium in enumerate(kriterien(revision), start=1)
    ]


def menschenschritt(auftrag, revision=None):
    return auftrag.nur_mensch or any(
        regel["nur_mensch"] or regel["erfuellbar_durch"] == "fred"
        for regel in kriterien(revision or auftrag.aktuelle_revision)
    )


def mehrere_module(kriteriensatz, projekt_id, modul_id):
    module = {modul_id} if modul_id is not None else set()
    anbieter_ids = set()
    for regel in kriteriensatz:
        if regel.get("modul_id") is not None:
            module.add(regel["modul_id"])
        elif regel.get("auftrag_id") is not None:
            anbieter_ids.add(regel["auftrag_id"])
    if anbieter_ids:
        module.update(
            Auftrag.objects.filter(pk__in=anbieter_ids, projekt_id=projekt_id)
            .exclude(modul_id=None)
            .values_list("modul_id", flat=True)
        )
    return len(module) > 1


def kriterien_fehler(auftrag, revision):
    if not revision.kriterienregeln:
        return []
    regeln = revision.kriterienregeln
    fehler = []
    if auftrag.typ != Auftrag.TYP_INTEGRATION and mehrere_module(
        regeln, auftrag.projekt_id, auftrag.modul_id
    ):
        fehler.append("Abnahmekriterien über mehrere Module brauchen die Art Integration.")
    vorgaenger = set(auftrag.abhaengigkeiten.values_list("vorgaenger_id", flat=True))
    lieferanten = set(
        Lieferbedarf.objects.filter(
            meldung__auftrag=auftrag,
            meldung__revision_nr=revision.nummer,
            meldung__historisch=False,
            lieferauftrag__projekt_id=auftrag.projekt_id,
            lieferrevision=F("lieferauftrag__revision_nr"),
        ).values_list("lieferauftrag_id", flat=True)
    )
    for nr, regel in enumerate(regeln, start=1):
        bezeichnung = f"Kriterium {nr} „{regel['text'][:100]}“"
        if (
            regel.get("modul_id") is not None
            and not Modul.objects.filter(
                pk=regel["modul_id"], projekt_id=auftrag.projekt_id, aktiv=True
            ).exists()
        ):
            fehler.append(f"{bezeichnung} hat kein aktives Modul dieses Projekts.")
        wer = regel["erfuellbar_durch"]
        if wer.startswith("auftrag:"):
            pk = regel.get("auftrag_id")
            if (
                pk not in vorgaenger | lieferanten
                or not Auftrag.objects.filter(pk=pk, projekt_id=auftrag.projekt_id)
                .exclude(zustand=Auftrag.ABGEBROCHEN)
                .exists()
            ):
                fehler.append(
                    f"{bezeichnung} braucht {wer.split(':', 1)[1]} als Vorgänger oder Lieferbedarf."
                )
        elif wer == "fred":
            erlaubte_stati = [Entscheidung.VORSCHLAG]
            if auftrag.zustand != Auftrag.ENTWURF:
                erlaubte_stati.append(Entscheidung.ENTSCHIEDEN)
            if not Entscheidung.objects.filter(
                pk=regel.get("entscheidung_id"),
                betroffene_projekte=auftrag.projekt_id,
                zustand__in=erlaubte_stati,
            ).exists():
                fehler.append(f"{bezeichnung} braucht eine verknüpfte offene Entscheidung.")
    return fehler


def abhaengigkeiten(projekt_id, *, ohne_kriterien_von=None):
    """Ein aktueller Lieferbedarf gehört zur neuesten gültigen Blockermeldung."""
    graph = defaultdict(set)
    vorgaenger = Abhaengigkeit.objects.filter(
        auftrag__projekt_id=projekt_id, vorgaenger__projekt_id=projekt_id
    ).values_list("auftrag_id", "vorgaenger_id")
    for verbraucher, anbieter in vorgaenger:
        graph[verbraucher].add(anbieter)

    blocker = Meldung.objects.filter(
        auftrag_id=OuterRef("meldung__auftrag_id"),
        revision_nr=OuterRef("meldung__auftrag__revision_nr"),
        historisch=False,
        art__in=(*Meldung.BLOCKIERENDE, Meldung.ANTWORT),
    ).order_by("-zeitpunkt", "-id")
    lieferungen = Lieferbedarf.objects.filter(
        Q(meldung__art=Meldung.LIEFERPLAN)
        | Q(
            meldung__auftrag__zustand=Auftrag.BLOCKIERT,
            meldung_id=Subquery(blocker.values("pk")[:1]),
        ),
        meldung__auftrag__projekt_id=projekt_id,
        meldung__revision_nr=F("meldung__auftrag__revision_nr"),
        meldung__historisch=False,
        lieferauftrag__projekt_id=projekt_id,
        lieferrevision=F("lieferauftrag__revision_nr"),
        erfuellt_durch__isnull=True,
    ).values_list("meldung__auftrag_id", "lieferauftrag_id")
    for verbraucher, anbieter in lieferungen:
        graph[verbraucher].add(anbieter)
    revisionen = Revision.objects.filter(
        auftrag__projekt_id=projekt_id, nummer=F("auftrag__revision_nr")
    ).exclude(kriterienregeln=[])
    if ohne_kriterien_von is not None:
        revisionen = revisionen.exclude(auftrag_id=ohne_kriterien_von)
    auftraege = set(Auftrag.objects.filter(projekt_id=projekt_id).values_list("pk", flat=True))
    for pk, regeln in revisionen.values_list("auftrag_id", "kriterienregeln").iterator():
        graph[pk].update(
            regel["auftrag_id"] for regel in regeln if regel.get("auftrag_id") in auftraege
        )
    return graph


def kreis_ab(graph, start):
    """Geschlossenen Weg ab dem ersten wiederbesuchten Knoten liefern."""
    pfad = [start]
    position = {start: 0}
    beendet = set()
    stapel = [iter(sorted(graph.get(start, ())))]
    while stapel:
        kind = next(stapel[-1], None)
        if kind is None:
            fertig = pfad.pop()
            beendet.add(fertig)
            del position[fertig]
            stapel.pop()
        elif kind in position:
            return [*pfad[position[kind] :], kind]
        elif kind not in beendet:
            position[kind] = len(pfad)
            pfad.append(kind)
            stapel.append(iter(sorted(graph.get(kind, ()))))
    return []


def kreisweg(auftrag, weiterer_vorgaenger=None, *, neue_kriterien=None):
    graph = abhaengigkeiten(
        auftrag.projekt_id, ohne_kriterien_von=auftrag.pk if neue_kriterien is not None else None
    )
    if neue_kriterien is not None:
        graph[auftrag.pk].update(
            regel["auftrag_id"] for regel in neue_kriterien if regel.get("auftrag_id") is not None
        )
    if weiterer_vorgaenger is not None:
        graph[auftrag.pk].add(weiterer_vorgaenger.pk)
    kette = kreis_ab(graph, auftrag.pk)
    if not kette:
        return ""
    nummern = {
        a.pk: a.nummer for a in Auftrag.objects.filter(pk__in=set(kette)).select_related("projekt")
    }
    return " -> ".join(nummern[pk] for pk in kette)


def kriterienstand(auftrag):
    return (
        auftrag.meldungen.filter(
            revision_nr=auftrag.revision_nr,
            art=Meldung.FORTSCHRITT,
            historisch=False,
            kriterien_erfuellt__isnull=False,
        )
        .values_list("kriterien_erfuellt", flat=True)
        .first()
        or []
    )


def sitzungsserie(auftrag):
    """Anzahl abgeschlossener Sitzungen ohne neuen Höchststand derselben Revision."""
    ohne_zuwachs = hoechststand = 0
    meldungen = auftrag.meldungen.filter(revision_nr=auftrag.revision_nr, historisch=False)
    relevante = Q(festgefahren=True) | Q(art=Meldung.SITZUNGSENDE, kriterien_erfuellt__isnull=False)
    for stand, festgefahren in (
        meldungen.filter(relevante).order_by("pk").values_list("kriterien_erfuellt", "festgefahren")
    ):
        if festgefahren or len(stand) > hoechststand:
            ohne_zuwachs = 0
        else:
            ohne_zuwachs += 1
        if stand is not None:
            hoechststand = max(hoechststand, len(stand))
    return ohne_zuwachs


def offene_festfahrfrage(auftrag):
    if auftrag.zustand != Auftrag.BLOCKIERT:
        return None
    meldungen = auftrag.meldungen.filter(revision_nr=auftrag.revision_nr, historisch=False)
    aktuell = meldungen.filter(Q(festgefahren=True) | Q(art=Meldung.ANTWORT)).first()
    return aktuell if aktuell and aktuell.festgefahren else None

"""Zielverbund und Fassung 2 des Werkbank-Vertrags (KIPM-36, ``vertrag/kipm-werkbank-2``).

Ein Auftrag umfasst neben dem Ziel seiner Zuweisung weitere Projektbindungen derselben Werkbank
(``WerkbankVerbundmitglied``). Jedes Mitglied ist ein eigenes Repository mit eigener Bindung;
integriert wird je Repository, eine Transaktion über mehrere gibt es nicht. Fred legt den Verbund
fest; jede Änderung erhöht die Zuweisungsfassung und hebt die Freigabe auf. Ohne Mitglieder ist
die Freigabebasis genau die bisherige, laufende Freigaben bleiben gültig.
"""

from django.db import transaction

from . import dienst, werkbank, werkbank_vertrag
from .models import (
    AUSFUEHRUNG_AKTIV,
    Auftrag,
    WerkbankAusfuehrung,
    WerkbankProjektbindung,
    WerkbankVerbundmitglied,
    WerkbankZuweisung,
)

#: Höchstzahl der Repositories eines Verbunds (Ziel plus Mitglieder; Schema ``zielverbund``).
MAX_REPOS = 20


def repo_id(pb_id: int) -> str:
    return f"repo-{pb_id}"


def mitglieder(zuweisung: WerkbankZuweisung | None) -> list[WerkbankProjektbindung]:
    """Weitere Mitglieder ohne das Ziel der Zuweisung, in fester Reihenfolge."""
    if zuweisung is None or zuweisung.pk is None:
        return []
    return [
        m.projektbindung
        for m in WerkbankVerbundmitglied.objects.filter(zuweisung=zuweisung).select_related(
            "projektbindung__projekt", "projektbindung__arbeitsort", "projektbindung__instanz"
        )
    ]


def braucht_fassung_2(auftrag: Auftrag) -> bool:
    """Delegierte Aufträge und Aufträge mit Verbund laufen nur über Fassung 2."""
    return (
        auftrag.chat_delegation_id is not None
        or WerkbankVerbundmitglied.objects.filter(zuweisung__auftrag_id=auftrag.pk).exists()
    )


def _pfad(text: str) -> str:
    return text.strip().replace("\\", "/").rstrip("/").lower()


def _repo(text: str) -> str:
    t = text.strip().rstrip("/").lower()
    return t.removesuffix(".git")


def _ueberschneidung(a: WerkbankProjektbindung, b: WerkbankProjektbindung) -> bool:
    """Gleicher oder verschachtelter Ordner, gleiche Repository-Adresse (Alias)."""
    oa, ob = a.arbeitsort, b.arbeitsort
    pa, pb = _pfad(oa.pfad or ""), _pfad(ob.pfad or "")
    if pa and pb and (pa == pb or pa.startswith(pb + "/") or pb.startswith(pa + "/")):
        return True
    ra, rb = _repo(oa.repository or ""), _repo(ob.repository or "")
    return bool(ra and ra == rb) or oa.pk == ob.pk


def verbund_setzen(akteur, auftrag: Auftrag, bindungen: list[WerkbankProjektbindung]) -> None:
    """Die weiteren Repositories eines zugewiesenen Auftrags festlegen (nur Fred). Eine leere Liste
    entfernt den Verbund. Alles oder nichts; eine Änderung hebt die Freigabe auf."""
    dienst._owner_erforderlich(akteur, "Den Zielverbund festlegen")
    with transaction.atomic():
        a = Auftrag.objects.select_for_update().select_related("projekt").get(pk=auftrag.pk)
        z = (
            WerkbankZuweisung.objects.select_related("projektbindung__arbeitsort", "instanz")
            .filter(auftrag=a)
            .first()
        )
        if z is None:
            raise dienst.DienstFehler(
                f"{a.nummer} ist keiner Werkbank zugewiesen; erst das Ziel festlegen."
            )
        if (
            a.zustand not in (Auftrag.ENTWURF, Auftrag.FREIGEGEBEN)
            or a.hat_bearbeiter
            or WerkbankAusfuehrung.objects.filter(auftrag=a, zustand__in=AUSFUEHRUNG_AKTIV).exists()
        ):
            raise dienst.Konflikt(
                f"{a.nummer} ist schon in Arbeit; der Verbund ändert sich erst nach "
                "„Bearbeitung ändern“."
            )
        ids = [b.pk for b in bindungen]
        if len(set(ids)) != len(ids):
            raise dienst.DienstFehler("Ein Repository steht doppelt im Verbund.")
        if len(ids) + 1 > MAX_REPOS:
            raise dienst.DienstFehler(f"Ein Verbund hat höchstens {MAX_REPOS} Repositories.")
        neu = list(
            WerkbankProjektbindung.objects.select_related("projekt", "arbeitsort", "instanz")
            .filter(pk__in=ids)
            .order_by("pk")
        )
        neu.sort(key=lambda b: ids.index(b.pk))
        if len(neu) != len(ids):
            raise dienst.DienstFehler("Ein Mitglied gibt es nicht.")
        alle = [z.projektbindung]
        for b in neu:
            if b.pk == z.projektbindung_id:
                raise dienst.DienstFehler("Das Ziel der Zuweisung ist schon Teil des Verbunds.")
            if b.instanz_id != z.instanz_id:
                raise dienst.DienstFehler(
                    f"„{b.projekt.name}“ gehört zu einer anderen Werkbank; ein Verbund nutzt "
                    "nur Bindungen derselben Werkbank."
                )
            if not b.projekt.aktiv or not werkbank.projekt_erlaubt(b.instanz, b.projekt):
                raise dienst.DienstFehler(
                    f"„{b.projekt.name}“ ist archiviert oder für diese Werkbank nicht erlaubt."
                )
            ort = b.arbeitsort
            if ort is None or not ort.aktiv or not ort.bestaetigt:
                raise dienst.DienstFehler(
                    f"„{b.projekt.name}“ hat keinen bestätigten, aktiven Arbeitsort."
                )
            for vorhanden in alle:
                if vorhanden.arbeitsort is not None and _ueberschneidung(b, vorhanden):
                    raise dienst.DienstFehler(
                        f"„{ort.name}“ und „{vorhanden.arbeitsort.name}“ sind dasselbe oder ein "
                        "verschachteltes Repository."
                    )
            alle.append(b)
        bisher = [m.pk for m in mitglieder(z)]
        if bisher == ids:
            return
        WerkbankVerbundmitglied.objects.filter(zuweisung=z).delete()
        for i, b in enumerate(neu, start=1):
            WerkbankVerbundmitglied.objects.create(zuweisung=z, projektbindung=b, reihenfolge=i)
        z.version += 1
        z.kontext_sha256 = ""
        z.geaendert_von = akteur.name
        if a.zustand == Auftrag.FREIGEGEBEN:
            a.zustand = Auftrag.ENTWURF
            a.freigegeben_am = None
            a.save()
        namen = ", ".join(b.arbeitsort.name for b in neu) or "keine weiteren"
        z.ereignis = dienst._ereignis(
            akteur,
            projekt=a.projekt,
            objekt_typ="Auftrag",
            objekt_kennung=a.nummer,
            ereignis="Zielverbund geändert",
            anlass=f"Weitere Repositories: {namen}; Zuweisungsfassung {z.version}",
            revision=str(a.revision_nr),
        )
        z.save()


def _ort(pb: WerkbankProjektbindung) -> dict:
    from . import akte

    ort = pb.arbeitsort
    return {
        "id": ort.pk if ort else 1,
        "name": werkbank._kurz(ort.name if ort else "") or "–",
        "beschreibung": akte._bereinigt(ort.pfad or ort.repository)[:2000] if ort else "",
    }


def _mitgliedsstand(pb: WerkbankProjektbindung) -> dict:
    ort = pb.arbeitsort
    return {
        "projektbindung_id": pb.pk,
        "projektbindung_version": pb.version,
        "instanz_id": pb.instanz_id,
        "arbeitsort": None
        if ort is None
        else [ort.pk, ort.revision, ort.pfad, ort.repository, ort.aktiv, ort.bestaetigt],
    }


def freigabebasis(auftrag: Auftrag) -> str:
    """Was die Freigabe bindet: die bisherige Basis (delegiert: Projektquellen, sonst Kontext) und,
    nur wenn es Mitglieder gibt, deren Bindungsstand. Ändert sich ein Mitglied, passt der Hash
    nicht mehr (X12)."""
    if auftrag.chat_delegation_id is not None:
        basis = werkbank.delegierter_quellenhash(auftrag.projekt_id)
    else:
        basis = werkbank.kontext_hash(werkbank.snapshot_bauen(auftrag))
    weitere = mitglieder(WerkbankZuweisung.objects.filter(auftrag=auftrag).first())
    if not weitere:
        return basis
    return werkbank_vertrag.sha256_hex(
        {"basis": basis, "verbund": [_mitgliedsstand(pb) for pb in weitere]}
    )


def snapshot_v2(auftrag: Auftrag, zuweisung: WerkbankZuweisung) -> dict:
    """Snapshot nach Fassung 2: alles aus Fassung 1, dazu Zuständigkeit und Zielverbund."""
    snap = werkbank.snapshot_bauen(auftrag)
    erlaubnis = None
    if auftrag.chat_delegation_id is not None:
        from .models import WerkbankChatDelegationsfassung

        stand = WerkbankChatDelegationsfassung.objects.filter(
            delegation_id=auftrag.chat_delegation_id, revision=auftrag.chat_delegation_revision
        ).first()
        erlaubnis = {
            "id": auftrag.chat_delegation_id,
            "revision": auftrag.chat_delegation_revision,
            "aktionen": sorted(stand.aktionen) if stand else [],
        }
    haupt = zuweisung.projektbindung
    verbund = [haupt, *mitglieder(zuweisung)]
    snap |= {
        "snapshot_version": 2,
        "zustaendigkeit": {
            "weg": "W1",
            "fuehrendes_projekt": auftrag.projekt.kennung,
            "erlaubnis": erlaubnis,
        },
        "zielverbund": [
            {
                "repo_id": repo_id(pb.pk),
                "fuehrend": pb.pk == haupt.pk,
                "projekt": pb.projekt.kennung,
                "projektbindung_id": pb.pk,
                "projektbindung_version": pb.version,
                "arbeitsort": _ort(pb),
                "basiscommit": None,
            }
            for pb in verbund
        ],
        "integrationseinheiten": [
            {"einheit": repo_id(pb.pk), "repos": [repo_id(pb.pk)]} for pb in verbund
        ],
    }
    return snap


def repos_pruefen(snapshot: dict, repos: list[dict]) -> None:
    """Die Übernahme nennt genau die Repositories des Verbunds, jedes einmal."""
    erwartet = [m["repo_id"] for m in snapshot["zielverbund"]]
    gemeldet = [r["repo_id"] for r in repos]
    if sorted(gemeldet) != sorted(erwartet) or len(set(gemeldet)) != len(gemeldet):
        raise werkbank.WerkbankFehler(
            409,
            "ZUSTANDSKONFLIKT",
            "Die gemeldeten Repositories passen nicht zum Zielverbund des Auftrags.",
            "Je Mitglied des Zielverbunds genau einen Eintrag in repos melden.",
            {"erwartet": erwartet[:20], "gemeldet": gemeldet[:20]},
        )


def repo_ids(ausf: WerkbankAusfuehrung) -> set[str]:
    return {m["repo_id"] for m in ausf.snapshot.get("zielverbund", [])}


def mitglied_geaendert(ausf: WerkbankAusfuehrung) -> str:
    """Name eines Mitglieds, dessen Bindung sich seit dem Snapshot geändert hat, sonst leer."""
    for m in ausf.snapshot.get("zielverbund", []):
        if m["fuehrend"]:
            continue  # das Ziel prüft schon die Quellbindung
        pb = WerkbankProjektbindung.objects.filter(pk=m["projektbindung_id"]).first()
        if pb is None or pb.version != m["projektbindung_version"]:
            return m["arbeitsort"]["name"]
    return ""

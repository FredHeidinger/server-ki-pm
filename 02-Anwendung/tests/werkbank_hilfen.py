"""Gemeinsame Bausteine der Werkbank-Tests (Nachrichten bauen, senden, übernommene Ausführung)."""

import json
import uuid

from steuerung import werkbank, werkbank_vertrag

KOPF = {"HTTP_X_KIPM_VERTRAG": "kipm-werkbank/1"}
BASIS = "/api/werkbank/v1/"
HASH = "a" * 64


def kopf(instanz):
    return {"HTTP_AUTHORIZATION": f"Bearer {instanz.klartext}"} | KOPF


def vor_quelle(instanz, pb, a):
    z = a.zuweisung
    return {
        "server_id": str(werkbank.server_id()),
        "werkbank_id": str(instanz.werkbank_id),
        "projektbindung_id": pb.pk,
        "projektbindung_version": pb.version,
        "auftrag_id": a.pk,
        "revision": a.revision_nr,
        "zuweisung_version": z.version,
    }


def nachricht(art, q, nutzlast, operation_id=None):
    n = {
        "vertrag": "kipm-werkbank/1",
        "operation_id": str(operation_id or uuid.uuid4()),
        "art": art,
        "quelle": q,
        "nutzlast": nutzlast,
    }
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    return n


def uebernahme_nachricht(q, operation_id=None, repo="repo-1"):
    return nachricht(
        "uebernehmen",
        q,
        {
            "repo_fingerabdruck": repo,
            "konfig_fingerabdruecke": {"pruefprofil": HASH},
            "faehigkeiten": ["git", "pytest"],
            "werkbank_version": "1.0.0",
        },
        operation_id,
    )


def quelle_von(ausf, instanz):
    return werkbank._quelle_der_ausfuehrung(ausf, instanz)


def schritt_nachricht(
    ausf,
    instanz,
    *,
    paket="root",
    phase="bauen",
    position=0,
    versuch=1,
    planfassung="vorplanung",
    wirkung="schreiben",
    owner_aktion=None,
    eingabe=HASH,
    operation_id=None,
    ausfuehrung_id=None,
):
    return nachricht(
        "schritt",
        quelle_von(ausf, instanz),
        {
            "schritt": {
                "ausfuehrung_id": str(ausfuehrung_id or ausf.ausfuehrung_id),
                "planfassung": planfassung,
                "paket": paket,
                "phase": phase,
                "position": position,
            },
            "versuch_nr": versuch,
            "eingabehash": eingabe,
            "wirkung": wirkung,
            "owner_aktion_id": str(owner_aktion) if owner_aktion else None,
        },
        operation_id,
    )


def ereignis_nachricht(
    ausf,
    instanz,
    art,
    daten=None,
    *,
    sequenz,
    statusversion=None,
    ereignis_id=None,
    operation_id=None,
):
    return nachricht(
        "ereignisse",
        quelle_von(ausf, instanz),
        {
            "ereignis_id": str(ereignis_id or uuid.uuid4()),
            "sequenz": sequenz,
            "statusversion": statusversion,
            "ereignis": {"art": art, "daten": daten or {}},
        },
        operation_id,
    )


def senden(client, instanz, pfad, n):
    return client.post(
        BASIS + pfad, data=json.dumps(n), content_type="application/json", **kopf(instanz)
    )

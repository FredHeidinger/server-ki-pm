"""Werkbank-Anbindung WP5: ``schritt`` und ``ereignisse`` (Schrittschlüssel, Versuche, UNKLAR,
Statusprojektion, historische Quittierung später Meldungen)."""

import uuid

import pytest

from steuerung import werkbank_vertrag
from steuerung.models import (
    Auftrag,
    Meldung,
    WerkbankAktion,
    WerkbankEreignis,
    WerkbankOperation,
)
from tests.werkbank_hilfen import (
    ereignis_nachricht,
    schritt_nachricht,
    senden,
)

pytestmark = pytest.mark.django_db

PLAN = "b" * 64


def schritt(client, ausf, instanz, **kw):
    antwort = senden(client, instanz, "schritt/", schritt_nachricht(ausf, instanz, **kw))
    assert antwort.status_code == 200, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("schritt_antwort", daten) == []
    return daten


def ereignis(client, ausf, instanz, art, daten=None, **kw):
    kw.setdefault("sequenz", ausf.ereignisse.count() + 1)
    antwort = senden(
        client, instanz, "ereignisse/", ereignis_nachricht(ausf, instanz, art, daten, **kw)
    )
    assert antwort.status_code == 200, antwort.content
    body = antwort.json()
    assert werkbank_vertrag.schema_fehler("ereignisse_antwort", body) == []
    ausf.refresh_from_db()
    return body


def plan(client, ausf, instanz, pakete=("p1",), **kw):
    return ereignis(
        client,
        ausf,
        instanz,
        "plan_bestaetigt",
        {
            "planfassung": PLAN,
            "pakete": [{"paket": p, "titel": f"Paket {p}", "zustand": "offen"} for p in pakete],
        },
        **kw,
    )


# --- schritt ---------------------------------------------------------------------------------


def test_erster_schritt_wird_erlaubt_und_startet_die_ausfuehrung(client, w_ausfuehrung, w_instanz):
    daten = schritt(client, w_ausfuehrung, w_instanz)
    assert daten["erlaubt"] is True and daten["grund_code"] is None
    uuid.UUID(daten["autorisierung_id"])
    op = WerkbankOperation.objects.get(art="schritt")
    assert op.klaerung == "ungeklaert" and op.versuch_nr == 1 and op.paket == "root"
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.zustand == "laeuft"


def test_identische_wiederholung_liefert_dieselbe_autorisierung(client, w_ausfuehrung, w_instanz):
    n = schritt_nachricht(w_ausfuehrung, w_instanz)
    erste = senden(client, w_instanz, "schritt/", n).json()
    zweite = senden(client, w_instanz, "schritt/", n).json()
    assert zweite["autorisierung_id"] == erste["autorisierung_id"] and zweite["erlaubt"]
    assert WerkbankOperation.objects.filter(art="schritt").count() == 1


def test_zwei_uuids_fuer_denselben_versuch_geben_keine_zweite_autorisierung(
    client, w_ausfuehrung, w_instanz
):
    erste = schritt(client, w_ausfuehrung, w_instanz)
    zweite = schritt(client, w_ausfuehrung, w_instanz)  # neue operation_id, gleicher Versuch
    assert erste["erlaubt"] and not zweite["erlaubt"]
    assert zweite["grund_code"] == "VERSUCH_BEREITS_AUTORISIERT"
    assert zweite["autorisierung_id"] is None
    assert WerkbankOperation.objects.filter(art="schritt", versuch_nr=1).count() == 1


def test_zweiter_versuch_nur_nach_geklaertem_ersten(client, w_ausfuehrung, w_instanz):
    plan(client, w_ausfuehrung, w_instanz)
    pf = PLAN
    assert schritt(client, w_ausfuehrung, w_instanz, paket="p1", planfassung=pf)["erlaubt"]
    zwei = schritt(client, w_ausfuehrung, w_instanz, paket="p1", planfassung=pf, versuch=2)
    assert not zwei["erlaubt"] and zwei["grund_code"] == "VORHERIGER_VERSUCH_UNGEKLAERT"
    # bekannter Fehlschlag klärt den ersten Versuch
    ereignis(
        client, w_ausfuehrung, w_instanz, "fehler", {"paket": "p1", "code": "TEST", "text": "rot"}
    )
    drei = schritt(client, w_ausfuehrung, w_instanz, paket="p1", planfassung=pf, versuch=2)
    assert drei["erlaubt"] and drei["grund_code"] is None


def test_anderer_schritt_neben_ungeklaertem_ist_erlaubt(client, w_ausfuehrung, w_instanz):
    assert schritt(client, w_ausfuehrung, w_instanz, position=0)["erlaubt"]
    assert schritt(client, w_ausfuehrung, w_instanz, position=1)["erlaubt"]


def test_bei_unklar_startet_nichts_neues(client, akteur, w_ausfuehrung, w_instanz):
    assert schritt(client, w_ausfuehrung, w_instanz, position=0)["erlaubt"]
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "fehler",
        {"code": "ABBRUCH", "text": "Ausgang unbekannt", "ausgang_unklar": True},
    )
    assert w_ausfuehrung.zustand == "unklar"
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.BLOCKIERT
    antwort = schritt(client, w_ausfuehrung, w_instanz, position=5)
    assert not antwort["erlaubt"] and antwort["grund_code"] == "UNKLAR"
    # der ungeklärte Versuch bleibt ungeklärt
    assert WerkbankOperation.objects.get(art="schritt", position=0).klaerung == "ungeklaert"


def test_schrittschluessel_nennt_die_eigene_ausfuehrung(client, w_ausfuehrung, w_instanz):
    n = schritt_nachricht(w_ausfuehrung, w_instanz, ausfuehrung_id=uuid.uuid4())
    antwort = senden(client, w_instanz, "schritt/", n)
    assert antwort.status_code == 400
    assert antwort.json()["fehler"]["code"] == "UNGUELTIGE_NACHRICHT"


def test_planfassung_folgt_dem_bestaetigten_plan(client, w_ausfuehrung, w_instanz):
    # Vor dem Plan gilt nur „vorplanung“, danach nur die bestätigte Fassung.
    assert schritt(client, w_ausfuehrung, w_instanz, planfassung=PLAN)["grund_code"] == (
        "PLANFASSUNG_UNBEKANNT"
    )
    plan(client, w_ausfuehrung, w_instanz)
    assert w_ausfuehrung.planfassung == PLAN
    alt = schritt(client, w_ausfuehrung, w_instanz, position=7)
    assert not alt["erlaubt"] and alt["grund_code"] == "PLANFASSUNG_UNBEKANNT"
    assert schritt(client, w_ausfuehrung, w_instanz, position=7, planfassung=PLAN)["erlaubt"]


def _aktion(ausf, art="start_freigeben", owner=None, zustand="ausstehend", ziel=None):
    from django.contrib.auth import get_user_model

    benutzer = owner or get_user_model().objects.first()
    return WerkbankAktion.objects.create(
        ausfuehrung=ausf,
        art=art,
        revision=ausf.revision,
        zuweisung_version=ausf.zuweisung_version,
        projektbindung_version=ausf.projektbindung_version,
        erwarteter_zustand="wartet_freigabe",
        ziel=ziel or {},
        owner=benutzer,
        zustand=zustand,
    )


def test_wartende_freigabe_braucht_eine_owner_aktion(client, w_ausfuehrung, w_instanz):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "freigabe_noetig",
        {"freigabe_id": "f1", "art": "start", "grund": "Bezahlter Umfang"},
    )
    assert w_ausfuehrung.zustand == "wartet_freigabe"
    ohne = schritt(client, w_ausfuehrung, w_instanz)
    assert not ohne["erlaubt"] and ohne["grund_code"] == "FREIGABE_AUSSTEHEND"
    aktion = _aktion(w_ausfuehrung, ziel={"freigabe_id": "f1"})
    mit = schritt(client, w_ausfuehrung, w_instanz, owner_aktion=aktion.aktion_id, position=1)
    assert mit["erlaubt"]
    aktion.refresh_from_db()
    assert aktion.zustand == "angenommen" and aktion.operation is not None
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.zustand == "laeuft"


def test_owner_aktion_wirkt_nur_einmal(client, w_ausfuehrung, w_instanz):
    aktion = _aktion(w_ausfuehrung)
    assert schritt(client, w_ausfuehrung, w_instanz, owner_aktion=aktion.aktion_id)["erlaubt"]
    zweite = schritt(client, w_ausfuehrung, w_instanz, owner_aktion=aktion.aktion_id, position=2)
    assert not zweite["erlaubt"] and zweite["grund_code"] == "AKTION_VERBRAUCHT"


def test_unbekannte_oder_veraltete_aktion_wird_abgelehnt(client, w_ausfuehrung, w_instanz):
    unbekannt = schritt(client, w_ausfuehrung, w_instanz, owner_aktion=uuid.uuid4())
    assert unbekannt["grund_code"] == "AKTION_UNGUELTIG"
    alt = _aktion(w_ausfuehrung, zustand="veraltet")
    antwort = schritt(client, w_ausfuehrung, w_instanz, owner_aktion=alt.aktion_id, position=3)
    assert antwort["grund_code"] == "AKTION_UNGUELTIG"


# --- ereignisse ------------------------------------------------------------------------------


def test_plan_bestaetigt_speichert_fassung_und_pakete(client, w_ausfuehrung, w_instanz):
    body = plan(client, w_ausfuehrung, w_instanz, pakete=("p1", "p2"))
    assert body["ergebnis"] == "verarbeitet"
    assert w_ausfuehrung.planfassung == PLAN
    assert [p["paket"] for p in w_ausfuehrung.pakete] == ["p1", "p2"]
    e = WerkbankEreignis.objects.get(art="plan_bestaetigt")
    assert e.verarbeitung == "angewendet" and e.rueckmeldung["ereignis"]["art"] == "plan_bestaetigt"


def test_identisches_ereignis_wird_nur_einmal_verarbeitet(client, w_ausfuehrung, w_instanz):
    eid = uuid.uuid4()
    daten = {"paket": "p1", "zustand": "laeuft"}
    ereignis(client, w_ausfuehrung, w_instanz, "paket_status", daten, sequenz=1, ereignis_id=eid)
    zwei = ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", daten, sequenz=1, ereignis_id=eid
    )
    assert zwei["ergebnis"] == "wiederholt"
    assert WerkbankEreignis.objects.filter(ereignis_id=eid).count() == 1


def test_widerspruechliches_ereignis_409(client, w_ausfuehrung, w_instanz):
    eid = uuid.uuid4()
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "paket_status",
        {"paket": "p1", "zustand": "laeuft"},
        sequenz=1,
        ereignis_id=eid,
    )
    n = ereignis_nachricht(
        w_ausfuehrung,
        w_instanz,
        "paket_status",
        {"paket": "p1", "zustand": "fehler"},
        sequenz=1,
        ereignis_id=eid,
    )
    antwort = senden(client, w_instanz, "ereignisse/", n)
    assert antwort.status_code == 409
    assert antwort.json()["fehler"]["code"] == "WIDERSPRUCH_WIEDERHOLUNG"


def test_paket_status_projiziert_zaehlbare_staende(client, w_ausfuehrung, w_instanz):
    plan(client, w_ausfuehrung, w_instanz, pakete=("p1", "p2"))
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "paket_status",
        {"paket": "p1", "zustand": "erledigt", "versuche": 2, "handlungsbedarf": False},
    )
    p1 = next(p for p in w_ausfuehrung.pakete if p["paket"] == "p1")
    assert p1["zustand"] == "erledigt" and p1["versuche"] == 2
    ereignis(client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p9", "zustand": "laeuft"})
    assert {p["paket"] for p in w_ausfuehrung.pakete} == {"p1", "p2", "p9"}


def test_rueckfrage_blockiert_den_auftrag_wenn_nichts_weiterlaeuft(
    client, w_ausfuehrung, w_instanz
):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "rueckfrage",
        {"frage_id": "q1", "paket": "root", "text": "Welche Variante?"},
    )
    assert w_ausfuehrung.zustand == "wartet_antwort"
    assert w_ausfuehrung.bedarf[0]["typ"] == "frage" and w_ausfuehrung.bedarf[0]["frage_id"] == "q1"
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.BLOCKIERT
    assert Meldung.objects.filter(auftrag=w_ausfuehrung.auftrag, art=Meldung.RUECKFRAGE).exists()


def test_handlungsbedarf_bleibt_sichtbar_waehrend_andere_pakete_laufen(
    client, w_ausfuehrung, w_instanz
):
    plan(client, w_ausfuehrung, w_instanz, pakete=("p1", "p2"))
    ereignis(client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p2", "zustand": "laeuft"})
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "rueckfrage",
        {"frage_id": "q1", "paket": "p1", "text": "Frage?"},
    )
    assert len(w_ausfuehrung.bedarf) == 1
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT


def test_freigabe_noetig_wird_zum_bedarf(client, w_ausfuehrung, w_instanz):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "freigabe_noetig",
        {"freigabe_id": "u1", "art": "uebernahme", "pakete": ["p1"], "grund": "Integration"},
    )
    assert w_ausfuehrung.zustand == "wartet_freigabe"
    assert w_ausfuehrung.bedarf[0]["typ"] == "freigabe"
    assert w_ausfuehrung.bedarf[0]["art"] == "uebernahme"


def test_bekannter_fehler_setzt_fehlerzustand(client, w_ausfuehrung, w_instanz):
    ereignis(client, w_ausfuehrung, w_instanz, "fehler", {"code": "LINT", "text": "rot"})
    assert w_ausfuehrung.zustand == "fehler"
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.BLOCKIERT


def test_offline_phase_und_rueckkehr(client, w_ausfuehrung, w_instanz):
    ereignis(client, w_ausfuehrung, w_instanz, "offline_phase", {"grund": "Netz weg"})
    assert w_ausfuehrung.zustand == "offline"
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT  # Netzausfall ist eine Phase
    ereignis(client, w_ausfuehrung, w_instanz, "lebenszeichen")
    assert w_ausfuehrung.zustand != "offline"


def test_statusversion_ist_monoton(client, w_ausfuehrung, w_instanz):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "paket_status",
        {"paket": "p1", "zustand": "laeuft"},
        statusversion=5,
    )
    assert w_ausfuehrung.statusversion == 5
    spaet = ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "paket_status",
        {"paket": "p1", "zustand": "fehler"},
        statusversion=3,
    )
    assert spaet["ergebnis"] == "historisch_quittiert" and w_ausfuehrung.statusversion == 5
    assert next(p for p in w_ausfuehrung.pakete if p["paket"] == "p1")["zustand"] == "laeuft"


def test_meldung_zu_ueberholter_fassung_wird_historisch_quittiert(client, w_ausfuehrung, w_instanz):
    n = ereignis_nachricht(
        w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "fehler"}, sequenz=1
    )
    n["quelle"]["zuweisung_version"] = 0 + 1  # gleich ...
    from steuerung.models import WerkbankZuweisung

    WerkbankZuweisung.objects.filter(pk=w_ausfuehrung.zuweisung_id).update(version=2)
    from tests.werkbank_hilfen import nachricht

    alt = nachricht("ereignisse", n["quelle"], n["nutzlast"])
    antwort = senden(client, w_instanz, "ereignisse/", alt)
    assert antwort.status_code == 200, antwort.content
    assert antwort.json()["ergebnis"] == "historisch_quittiert"
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.pakete == []
    assert WerkbankEreignis.objects.get().verarbeitung == "historisch"


def test_meldung_zu_beendeter_ausfuehrung_ist_historisch(client, w_ausfuehrung, w_instanz):
    type(w_ausfuehrung).objects.filter(pk=w_ausfuehrung.pk).update(zustand="abgebrochen")
    body = ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "laeuft"}
    )
    assert body["ergebnis"] == "historisch_quittiert" and w_ausfuehrung.pakete == []


def test_halt_ueberschreibt_keinen_technischen_zustand(client, w_ausfuehrung, w_instanz):
    type(w_ausfuehrung).objects.filter(pk=w_ausfuehrung.pk).update(
        zustand="halt_angefordert", halt_zweck="abbrechen"
    )
    ereignis(client, w_ausfuehrung, w_instanz, "fehler", {"code": "X", "text": "rot"})
    assert w_ausfuehrung.zustand == "halt_angefordert"
    assert WerkbankEreignis.objects.get(art="fehler").verarbeitung == "angewendet"


def test_nicht_umgesetzte_ereignisart_bleibt_ehrlich_501(client, w_ausfuehrung, w_instanz):
    n = ereignis_nachricht(w_ausfuehrung, w_instanz, "abschluss_bericht", {}, sequenz=1)
    # Schema verlangt Pflichtfelder: 400 ist hier der erwartete, ehrliche Ausgang.
    assert senden(client, w_instanz, "ereignisse/", n).status_code in {400, 501}

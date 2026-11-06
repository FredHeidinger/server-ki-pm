"""Befunde der unabhängigen Prüfung (Reviewer) zu WP4 bis WP8: jeder Fall ist am Original
nachgestellt; Tests zuerst, ohne die Korrektur rot."""

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.db import connection

from steuerung import dienst, werkbank
from steuerung.models import Auftrag, WerkbankAktion, WerkbankAusfuehrung, WerkbankOperation
from tests.werkbank_hilfen import ereignis_nachricht, schritt_nachricht, senden

pytestmark = pytest.mark.django_db

PLAN = "c" * 64
COMMIT = "abc1234"


def ereignis(client, ausf, instanz, art, daten=None, **kw):
    kw.setdefault("sequenz", ausf.ereignisse.count() + 1)
    n = ereignis_nachricht(ausf, instanz, art, daten, **kw)
    antwort = senden(client, instanz, "ereignisse/", n)
    assert antwort.status_code == 200, antwort.content
    ausf.refresh_from_db()
    return antwort.json()


def schritt(client, ausf, instanz, **kw):
    antwort = senden(client, instanz, "schritt/", schritt_nachricht(ausf, instanz, **kw))
    assert antwort.status_code == 200, antwort.content
    return antwort.json()


def plan(client, ausf, instanz, pakete=("p1",)):
    ereignis(
        client, ausf, instanz, "plan_bestaetigt",
        {"planfassung": PLAN, "pakete": [{"paket": p, "titel": p, "zustand": "offen"} for p in pakete]},
    )  # fmt: skip


def aktion(ausf, art, **ziel):
    return WerkbankAktion.objects.create(
        ausfuehrung=ausf,
        art=art,
        revision=ausf.revision,
        zuweisung_version=ausf.zuweisung_version,
        projektbindung_version=ausf.projektbindung_version,
        erwarteter_zustand=ausf.zustand,
        ziel=ziel,
        owner=get_user_model().objects.first(),
    )


# --- Alte Wege ---------------------------------------------------------------------------------


def test_verschieben_nur_des_arbeitsorts_ist_bei_aktiver_werkbank_gesperrt(
    client, akteur, projekt, w_ausfuehrung, w_instanz
):
    dienst.arbeitsort_anlegen(akteur, projekt, kennung="zweit", name="Zweiter Ort")
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "rueckfrage",
        {"frage_id": "q", "paket": "root", "text": "?"},
    )
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    vorschau = dienst.verschiebung_pruefen(
        akteur,
        [{"auftrag": a.nummer, "revision": a.revision_nr, "projekt": projekt.kennung, "arbeitsort": "zweit"}],
    )  # fmt: skip
    assert any("Werkbank" in k for e in vorschau["eintraege"] for k in e["konflikte"])
    assert not vorschau["ausfuehrbar"]


@pytest.mark.django_db(transaction=True)
def test_sperrpruefung_der_alten_wege_laeuft_in_der_transaktion(
    akteur, projekt, w_ausfuehrung, monkeypatch
):
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    beobachtet = []
    echt = dienst._werkbank_aktiv

    def merken(auftrag):
        beobachtet.append(connection.in_atomic_block)
        return echt(auftrag)

    monkeypatch.setattr(dienst, "_werkbank_aktiv", merken)
    with pytest.raises(dienst.Konflikt):
        dienst.auftrag_abbrechen(akteur, a, "weg")
    assert beobachtet and all(beobachtet)  # Prüfung und Wirkung teilen eine Transaktion


def test_projekt_mit_aktiver_werkbank_ausfuehrung_wird_nicht_archiviert(
    client, akteur, projekt, w_ausfuehrung, w_instanz
):
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "rueckfrage",
        {"frage_id": "q", "paket": "root", "text": "?"},
    )
    with pytest.raises(dienst.Konflikt):
        dienst.projekt_archivieren(akteur, projekt)


# --- Abschlussregel ----------------------------------------------------------------------------


@pytest.fixture
def fertig(client, w_ausfuehrung, w_instanz):
    plan(client, w_ausfuehrung, w_instanz)
    ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"}
    )
    return w_ausfuehrung


def bericht(ausf, **aenderungen):
    ids = [k["id"] for k in ausf.snapshot["abnahmekriterien"]]
    daten = {
        "planfassung": PLAN,
        "kriterien": [
            {"kriterium_id": i, "behandlung": "erfuellt", "nachweis": "grün", "commit": COMMIT}
            for i in ids
        ],
        "paketplan": [{"paket": "p1", "titel": "p1", "zustand": "erledigt"}],
        "integrationen": [{"paket": "p1", "commit": COMMIT}],
        "pruefungen": [{"paket": "p1", "art": "test", "ergebnis": "bestanden", "beschreibung": "ok"}],
        "gesamtpruefung": {"paket": "root", "art": "gesamt", "ergebnis": "bestanden", "beschreibung": "ok"},
        "offene_arbeiten": [],
        "ungeklaerte_wirkungen": [],
    }  # fmt: skip
    daten.update(aenderungen)
    return daten


def abschlussversuch(client, ausf, instanz, **aenderungen):
    n = ereignis_nachricht(
        ausf,
        instanz,
        "abschluss_bericht",
        bericht(ausf, **aenderungen),
        sequenz=ausf.ereignisse.count() + 1,
    )
    return senden(client, instanz, "ereignisse/", n)


@pytest.mark.parametrize("endzustand", ["verworfen", "zerlegt", "ersetzt"])
def test_abschluss_ohne_jede_integration_wird_abgelehnt(client, fertig, w_instanz, endzustand):
    antwort = abschlussversuch(
        client, fertig, w_instanz,
        paketplan=[{"paket": "p1", "titel": "p1", "zustand": endzustand}],
        integrationen=[], pruefungen=[], gesamtpruefung=None,
        kriterien=[{**k, "commit": None} for k in bericht(fertig)["kriterien"]],
    )  # fmt: skip
    assert antwort.status_code == 409
    assert antwort.json()["fehler"]["code"] == "ABSCHLUSS_UNVOLLSTAENDIG"
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand != Auftrag.ERLEDIGT


def test_zerlegt_und_ersetzt_brauchen_ein_nachfolgepaket(client, fertig, w_instanz):
    plan_ = [
        {"paket": "p1", "titel": "p1", "zustand": "zerlegt"},
        {"paket": "p2", "titel": "p2", "zustand": "erledigt"},
    ]
    antwort = abschlussversuch(
        client, fertig, w_instanz, paketplan=plan_, integrationen=[{"paket": "p2", "commit": COMMIT}],
        pruefungen=[{"paket": "p2", "art": "test", "ergebnis": "bestanden", "beschreibung": "ok"}],
    )  # fmt: skip
    assert antwort.status_code == 409  # p1 zerlegt, aber kein Paket mit zerlegt_aus p1
    plan_[1]["zerlegt_aus"] = "p1"
    antwort = abschlussversuch(
        client, fertig, w_instanz, paketplan=plan_, integrationen=[{"paket": "p2", "commit": COMMIT}],
        pruefungen=[{"paket": "p2", "art": "test", "ergebnis": "bestanden", "beschreibung": "ok"}],
    )  # fmt: skip
    assert antwort.status_code == 200, antwort.content


def test_doppelte_kriterien_und_pakete_widersprechen_sich(client, fertig, w_instanz):
    k = bericht(fertig)["kriterien"]
    doppelt = [{**k[0], "behandlung": "nicht_erfuellt"}, *k]
    assert abschlussversuch(client, fertig, w_instanz, kriterien=doppelt).status_code == 409
    paketplan = [
        {"paket": "p1", "titel": "p1", "zustand": "fehler"},
        {"paket": "p1", "titel": "p1", "zustand": "erledigt"},
    ]
    assert abschlussversuch(client, fertig, w_instanz, paketplan=paketplan).status_code == 409
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand != Auftrag.ERLEDIGT


def test_nicht_gemeldeter_schritt_eines_unbekannten_pakets_verhindert_den_abschluss(
    client, fertig, w_instanz
):
    assert schritt(client, fertig, w_instanz, paket="p9", planfassung=PLAN)["erlaubt"]
    antwort = abschlussversuch(client, fertig, w_instanz)
    assert antwort.status_code == 409
    assert "p9" in str(antwort.json()["fehler"]["details"])


# --- Schritte ----------------------------------------------------------------------------------


def test_unklar_bleibt_trotz_weiterer_ereignisse_bestehen(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    plan(client, ausf, w_instanz, pakete=("p1", "p2"))
    assert schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN)["erlaubt"]
    ereignis(
        client,
        ausf,
        w_instanz,
        "fehler",
        {"paket": "p1", "code": "X", "text": "?", "ausgang_unklar": True},
    )
    ereignis(client, ausf, w_instanz, "rueckfrage", {"frage_id": "q", "paket": "p2", "text": "?"})
    ereignis(client, ausf, w_instanz, "fehler", {"paket": "p2", "code": "Y", "text": "rot"})
    antwort = schritt(client, ausf, w_instanz, paket="p2", planfassung=PLAN, position=3)
    assert not antwort["erlaubt"] and antwort["grund_code"] == "UNKLAR"


def test_unklar_laesst_sich_nicht_durch_offline_und_rueckkehr_loesen(
    client, w_ausfuehrung, w_instanz
):
    ausf = w_ausfuehrung
    ereignis(client, ausf, w_instanz, "fehler", {"code": "X", "text": "?", "ausgang_unklar": True})
    ereignis(client, ausf, w_instanz, "offline_phase", {"grund": "Netz"})
    ereignis(client, ausf, w_instanz, "lebenszeichen")
    assert ausf.zustand == "unklar"
    antwort = schritt(client, ausf, w_instanz, position=9)
    assert not antwort["erlaubt"] and antwort["grund_code"] == "UNKLAR"


def test_wiederholung_eines_erlaubten_schritts_nach_unklar_wird_abgelehnt(
    client, w_ausfuehrung, w_instanz
):
    n = schritt_nachricht(w_ausfuehrung, w_instanz)
    assert senden(client, w_instanz, "schritt/", n).json()["erlaubt"]
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "fehler",
        {"code": "X", "text": "?", "ausgang_unklar": True},
    )
    wieder = senden(client, w_instanz, "schritt/", n).json()
    assert wieder["erlaubt"] is False and wieder["grund_code"] == "UNKLAR"


def test_freigabe_gilt_je_paket_und_ueberlebt_keine_offline_phase(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    plan(client, ausf, w_instanz, pakete=("p1", "p2"))
    ereignis(client, ausf, w_instanz, "paket_status", {"paket": "p1", "zustand": "laeuft"})
    ereignis(
        client, ausf, w_instanz, "freigabe_noetig",
        {"freigabe_id": "f1", "art": "start", "pakete": ["p2"], "grund": "bezahlt"},
    )  # fmt: skip
    ohne = schritt(client, ausf, w_instanz, paket="p2", planfassung=PLAN)
    assert not ohne["erlaubt"] and ohne["grund_code"] == "FREIGABE_AUSSTEHEND"
    assert schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN, position=1)["erlaubt"]
    ereignis(client, ausf, w_instanz, "offline_phase", {"grund": "Netz"})
    ereignis(client, ausf, w_instanz, "lebenszeichen")
    nochmal = schritt(client, ausf, w_instanz, paket="p2", planfassung=PLAN, position=2)
    assert not nochmal["erlaubt"] and nochmal["grund_code"] == "FREIGABE_AUSSTEHEND"


def test_freigabe_braucht_die_passende_aktion(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    ereignis(
        client,
        ausf,
        w_instanz,
        "freigabe_noetig",
        {"freigabe_id": "f1", "art": "start", "grund": "x"},
    )
    falsche_art = aktion(ausf, "antworten", frage_id="q", text="a")
    falsche_id = aktion(ausf, "start_freigeben", freigabe_id="andere")
    for fremd in (falsche_art, falsche_id):
        antwort = schritt(client, ausf, w_instanz, owner_aktion=fremd.aktion_id, position=fremd.pk)
        assert not antwort["erlaubt"] and antwort["grund_code"] == "AKTION_UNGUELTIG"
    passend = aktion(ausf, "start_freigeben", freigabe_id="f1")
    assert schritt(client, ausf, w_instanz, owner_aktion=passend.aktion_id, position=5)["erlaubt"]
    # die erteilte Freigabe gilt für die weiteren Schritte
    assert schritt(client, ausf, w_instanz, position=6)["erlaubt"]


def test_neuer_versuch_nur_nach_bekanntem_fehlschlag(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    plan(client, ausf, w_instanz)
    assert schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN)["erlaubt"]
    ereignis(client, ausf, w_instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"})
    nochmal = schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN, versuch=2)
    assert not nochmal["erlaubt"] and nochmal["grund_code"] == "SCHRITT_BEREITS_ERFOLGREICH"


def test_versuchsnummern_zaehlen_lueckenlos(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    plan(client, ausf, w_instanz)
    assert schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN)["erlaubt"]
    ereignis(client, ausf, w_instanz, "fehler", {"paket": "p1", "code": "X", "text": "rot"})
    sprung = schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN, versuch=3)
    assert not sprung["erlaubt"] and sprung["grund_code"] == "VERSUCHSNUMMER_UNGUELTIG"
    assert schritt(client, ausf, w_instanz, paket="p1", planfassung=PLAN, versuch=2)["erlaubt"]
    assert WerkbankOperation.objects.filter(art="schritt", ausgang_art="fehlschlag").count() == 1


def test_ein_schritt_schliesst_nur_die_fehler_seines_pakets(client, w_ausfuehrung, w_instanz):
    ausf = w_ausfuehrung
    plan(client, ausf, w_instanz)
    ereignis(client, ausf, w_instanz, "fehler", {"paket": "p1", "code": "X", "text": "rot"})
    assert ausf.zustand == "fehler"
    assert schritt(client, ausf, w_instanz, paket="p2", planfassung=PLAN)["erlaubt"]
    ausf.refresh_from_db()
    assert any(b["typ"] == "fehler" and b["paket"] == "p1" and b["offen"] for b in ausf.bedarf)


def test_historische_meldung_aendert_weder_zustand_noch_statusversion(
    client, w_ausfuehrung, w_instanz
):
    ausf = w_ausfuehrung
    ereignis(client, ausf, w_instanz, "offline_phase", {"grund": "Netz"})
    vorher = ausf.statusversion
    body = ereignis(
        client,
        ausf,
        w_instanz,
        "antwort_angewendet",
        {"aktion_id": str(uuid.uuid4())},
        statusversion=vorher + 5,
    )
    assert body["ergebnis"] == "historisch_quittiert"
    assert ausf.zustand == "offline" and ausf.statusversion == vorher
    assert WerkbankAusfuehrung.objects.get(pk=ausf.pk).zustand == "offline"
    assert werkbank  # Modul geladen

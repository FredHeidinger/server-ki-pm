"""Werkbank-Anbindung WP7: Abschlussregel, Sperre der alten Änderungswege, Wiederherstellung."""

import copy
import uuid

import pytest

from steuerung import akte, dienst, werkbank, werkbank_vertrag
from steuerung.models import (
    Auftrag,
    Ereignis,
    Ergebnis,
    WerkbankAusfuehrung,
    WerkbankEreignis,
)
from tests.werkbank_hilfen import (
    BASIS,
    ereignis_nachricht,
    kopf,
    senden,
)

pytestmark = pytest.mark.django_db

PLAN = "c" * 64
COMMIT = "abc1234"


def ereignis(client, ausf, instanz, art, daten=None, status=200, **kw):
    kw.setdefault("sequenz", ausf.ereignisse.count() + 1)
    antwort = senden(
        client, instanz, "ereignisse/", ereignis_nachricht(ausf, instanz, art, daten, **kw)
    )
    assert antwort.status_code == status, antwort.content
    ausf.refresh_from_db()
    return antwort.json()


@pytest.fixture
def fertig(client, w_ausfuehrung, w_instanz):
    """Plan mit einem Paket p1, das erledigt ist."""
    ereignis(
        client,
        w_ausfuehrung,
        w_instanz,
        "plan_bestaetigt",
        {"planfassung": PLAN, "pakete": [{"paket": "p1", "titel": "Paket", "zustand": "offen"}]},
    )
    ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"}
    )
    return w_ausfuehrung


def bericht(ausf, **aenderungen):
    ids = [k["id"] for k in ausf.snapshot["abnahmekriterien"]]
    daten = {
        "planfassung": PLAN,
        "kriterien": [
            {"kriterium_id": i, "behandlung": "erfuellt", "nachweis": "Test grün", "commit": COMMIT}
            for i in ids
        ],
        "paketplan": [{"paket": "p1", "titel": "Paket", "zustand": "erledigt"}],
        "integrationen": [{"paket": "p1", "commit": COMMIT}],
        "pruefungen": [
            {"paket": "p1", "art": "test", "ergebnis": "bestanden", "beschreibung": "pytest grün"}
        ],
        "gesamtpruefung": {
            "paket": "root", "art": "gesamt", "ergebnis": "bestanden", "beschreibung": "alles grün"
        },
        "offene_arbeiten": [],
        "ungeklaerte_wirkungen": [],
    }  # fmt: skip
    daten.update(aenderungen)
    return daten


def abschluss(client, ausf, instanz, status=200, **aenderungen):
    return ereignis(
        client, ausf, instanz, "abschluss_bericht", bericht(ausf, **aenderungen), status=status
    )


def abgelehnt(client, ausf, instanz, **aenderungen):
    body = abschluss(client, ausf, instanz, status=409, **aenderungen)
    assert body["fehler"]["code"] == "ABSCHLUSS_UNVOLLSTAENDIG"
    ausf.auftrag.refresh_from_db()
    assert ausf.auftrag.zustand == Auftrag.IN_ARBEIT and ausf.zustand != "abschluss_gemeldet"
    assert not WerkbankEreignis.objects.filter(art="abschluss_bericht").exists()
    return body["fehler"]


# --- Abschlussregel --------------------------------------------------------------------------


def test_vollstaendiger_abschluss_erledigt_den_auftrag(client, fertig, w_instanz):
    body = abschluss(client, fertig, w_instanz)
    assert werkbank_vertrag.schema_fehler("ereignisse_antwort", body) == []
    assert fertig.zustand == "abschluss_gemeldet" and fertig.abschluss_bestaetigt_am
    a = fertig.auftrag
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT and not a.hat_bearbeiter
    assert a.abgenommene_revision == a.revision_nr
    ergebnis = Ergebnis.objects.get(auftrag=a)
    assert COMMIT in ergebnis.dateien and ergebnis.fehlgeschlagenliste == []
    assert Ereignis.objects.filter(
        objekt_kennung=a.nummer, ereignis__startswith="Ergebnis"
    ).exists()
    assert WerkbankEreignis.objects.get(art="abschluss_bericht").verarbeitung == "angewendet"


def test_fehlendes_kriterium_verhindert_den_abschluss(client, fertig, w_instanz):
    kriterien = bericht(fertig)["kriterien"][:1]
    fehler = abgelehnt(client, fertig, w_instanz, kriterien=kriterien)
    assert "AK-2" in str(fehler["details"])


def test_nicht_erfuelltes_oder_unbekanntes_kriterium_verhindert_den_abschluss(
    client, fertig, w_instanz
):
    k = bericht(fertig)["kriterien"]
    k[0] = {**k[0], "behandlung": "nicht_erfuellt"}
    abgelehnt(client, fertig, w_instanz, kriterien=k)
    k2 = bericht(fertig)["kriterien"] + [
        {"kriterium_id": "AK-99", "behandlung": "erfuellt", "nachweis": "x", "commit": None}
    ]
    abgelehnt(client, fertig, w_instanz, kriterien=k2)


def test_bereits_erfuellt_braucht_einen_commit_als_nachweis(client, fertig, w_instanz):
    k = [
        {**x, "behandlung": "bereits_erfuellt", "commit": None}
        for x in bericht(fertig)["kriterien"]
    ]
    abgelehnt(client, fertig, w_instanz, kriterien=k)


def test_bereits_erfuellt_braucht_keinen_kuenstlichen_commit(client, fertig, w_instanz):
    k = [{**x, "behandlung": "bereits_erfuellt"} for x in bericht(fertig)["kriterien"]]
    abschluss(
        client, fertig, w_instanz, kriterien=k, integrationen=[], pruefungen=[], gesamtpruefung=None
    )
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand == Auftrag.ERLEDIGT


def test_fehlende_integration_verhindert_den_abschluss(client, fertig, w_instanz):
    abgelehnt(client, fertig, w_instanz, integrationen=[])


def test_fehlende_oder_rote_pruefung_verhindert_den_abschluss(client, fertig, w_instanz):
    abgelehnt(client, fertig, w_instanz, pruefungen=[])
    rot = [
        {"paket": "p1", "art": "test", "ergebnis": "bestanden", "beschreibung": "a"},
        {"paket": "p1", "art": "test", "ergebnis": "fehlgeschlagen", "beschreibung": "b"},
    ]
    abgelehnt(client, fertig, w_instanz, pruefungen=rot)


def test_gesamtpruefung_ist_erforderlich_und_muss_bestanden_sein(client, fertig, w_instanz):
    abgelehnt(client, fertig, w_instanz, gesamtpruefung=None)
    rot = {"paket": "root", "art": "gesamt", "ergebnis": "fehlgeschlagen", "beschreibung": "rot"}
    abgelehnt(client, fertig, w_instanz, gesamtpruefung=rot)


def test_offene_arbeiten_und_ungeklaerte_wirkungen_verhindern_den_abschluss(
    client, fertig, w_instanz
):
    abgelehnt(client, fertig, w_instanz, offene_arbeiten=["Doku fehlt"])
    abgelehnt(client, fertig, w_instanz, ungeklaerte_wirkungen=["Push unklar"])


def test_paketplan_muss_endgueltig_und_vollstaendig_sein(client, fertig, w_instanz):
    abgelehnt(
        client, fertig, w_instanz,
        paketplan=[{"paket": "p1", "titel": "Paket", "zustand": "laeuft"}],
    )  # fmt: skip
    abgelehnt(
        client, fertig, w_instanz,
        paketplan=[{"paket": "p7", "titel": "Anderes", "zustand": "erledigt"}],
    )  # fmt: skip


def test_planfassung_muss_die_bestaetigte_sein(client, fertig, w_instanz):
    abgelehnt(client, fertig, w_instanz, planfassung="d" * 64)


def test_offener_bedarf_verhindert_den_abschluss(client, fertig, w_instanz):
    ereignis(
        client, fertig, w_instanz, "rueckfrage", {"frage_id": "q1", "paket": "p1", "text": "?"}
    )
    body = abschluss(client, fertig, w_instanz, status=409)
    assert body["fehler"]["code"] == "ABSCHLUSS_UNVOLLSTAENDIG"
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand == Auftrag.BLOCKIERT and not Ergebnis.objects.exists()


def test_zweiter_abschluss_wird_abgelehnt(client, fertig, w_instanz):
    abschluss(client, fertig, w_instanz)
    body = abschluss(client, fertig, w_instanz, status=409)
    assert body["fehler"]["code"] == "ABSCHLUSS_GESPEICHERT"


def test_halt_zuerst_protokolliert_fakten_aber_erledigt_nicht(client, akteur, fertig, w_instanz):
    werkbank.aktion_erstellen(akteur, fertig, "bearbeitung_beenden")
    body = abschluss(client, fertig, w_instanz)
    assert body["ergebnis"] == "verarbeitet" and body["haltanforderung"] == "bearbeitung_aendern"
    assert fertig.zustand == "halt_angefordert"
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand == Auftrag.IN_ARBEIT
    assert WerkbankEreignis.objects.get(art="abschluss_bericht").verarbeitung == "angewendet"
    assert not Ergebnis.objects.exists()


def test_abschluss_zuerst_lehnt_spaeteren_halt_ab(client, akteur, fertig, w_instanz):
    abschluss(client, fertig, w_instanz)
    with pytest.raises(dienst.Konflikt):
        werkbank.aktion_erstellen(akteur, fertig, "abbrechen")


def test_abschluss_zu_ueberholter_fassung_wird_nur_quittiert(client, fertig, w_instanz):
    from steuerung.models import WerkbankZuweisung

    n = ereignis_nachricht(fertig, w_instanz, "abschluss_bericht", bericht(fertig), sequenz=9)
    WerkbankZuweisung.objects.filter(pk=fertig.zuweisung_id).update(version=2)
    body = senden(client, w_instanz, "ereignisse/", n).json()
    assert body["ergebnis"] == "historisch_quittiert"
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand == Auftrag.IN_ARBEIT


# --- Alte Änderungswege ----------------------------------------------------------------------


def test_alte_wege_koennen_einen_aktiven_werkbank_auftrag_nicht_aendern(
    akteur, projekt, w_ausfuehrung, w_instanz, bindung
):
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    werkbank_akteur = dienst.Akteur.aus_bindung(w_instanz.bindung)
    anderer = Auftrag.objects.create(projekt=projekt, titel="Anderer", revision_nr=1)
    aufrufe = [
        lambda: dienst.auftrag_bearbeiten(
            akteur, a, titel="Neu", beschreibung="x", ziel="y", nicht_ziele="n",
            arbeitsort=a.aktuelle_revision.arbeitsort, schreibumfang="docs/**", quellen="",
            abnahmekriterien="eins", prioritaet="hoch", modul=a.modul,
        ),
        lambda: dienst.auftrag_abbrechen(akteur, a, "weg"),
        lambda: dienst.bearbeitung_unklar(akteur, a, "unklar"),
        lambda: dienst.rueckfrage_beantworten(akteur, a, "Antwort"),
        lambda: dienst.vorgaenger_eintragen(akteur, a, anderer),
        lambda: dienst.ergebnis_einreichen(
            werkbank_akteur, a, a.revision_nr, zusammenfassung="fertig", dateien="x@abc1234"
        ),
    ]  # fmt: skip
    for aufruf in aufrufe:
        with pytest.raises(dienst.Konflikt) as fehler:
            aufruf()
        assert "Werkbank" in str(fehler.value)
    a.refresh_from_db()
    assert (
        a.zustand == Auftrag.IN_ARBEIT
        and a.hat_bearbeiter
        and a.revision_nr == w_ausfuehrung.revision
    )
    assert not Ergebnis.objects.exists()


def test_verschieben_eines_aktiven_werkbank_auftrags_ist_ein_konflikt(
    akteur, projekt, projekt_b, w_ausfuehrung
):
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    vorschau = dienst.verschiebung_pruefen(
        akteur, [{"auftrag": a.nummer, "revision": a.revision_nr, "projekt": projekt_b.kennung}]
    )
    assert any("Werkbank" in k for e in vorschau["eintraege"] for k in e["konflikte"])


def test_termin_und_meilenstein_bleiben_aenderbar(akteur, projekt, w_ausfuehrung):
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    m = akte.meilenstein_anlegen(akteur, projekt, name="M-Z")
    dienst.meilenstein_zuordnen(akteur, a, m)
    dienst.faellig_setzen(akteur, a, "2030-01-01")
    a.refresh_from_db()
    assert a.meilenstein_id == m.pk and a.faellig is not None
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == ""


def test_beendete_ausfuehrung_gibt_den_auftrag_wieder_frei(akteur, projekt, w_ausfuehrung):
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(zustand="beendet")
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    dienst.auftrag_abbrechen(akteur, a, "Entscheidung geändert")  # kein Konflikt mehr
    a.refresh_from_db()
    assert a.zustand == Auftrag.ABGEBROCHEN


def test_gewoehnliche_chat_auftraege_behalten_ihre_regeln(akteur, projekt, bindung):
    from tests.conftest import arbeitspaket

    a = arbeitspaket(akteur, projekt, titel="Chat", umfang="docs/**")
    dienst.auftrag_freigeben(akteur, a)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.ergebnis_einreichen(chat, a, a.revision_nr, zusammenfassung="ok", dateien="x@abc1234")
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT


# --- Wiederherstellung -----------------------------------------------------------------------


def test_abgleich_meldet_eine_dem_server_unbekannte_ausfuehrung(client, w_instanz, w_ausfuehrung):
    fremd = uuid.uuid4()
    bekannt = f"{w_ausfuehrung.ausfuehrung_id},{fremd}"
    antwort = client.get(BASIS + f"abgleich/?ausfuehrungen={bekannt}", **kopf(w_instanz))
    assert antwort.status_code == 200, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("abgleich_antwort", daten) == []
    assert [k["ausfuehrung_id"] for k in daten["konflikte"]] == [str(fremd)]
    assert daten["konflikte"][0]["code"] == "AUSFUEHRUNG_UNBEKANNT"
    assert WerkbankAusfuehrung.objects.count() == 1  # nichts neu abgeleitet


def test_ohne_angabe_gibt_es_keine_konflikte(client, w_instanz, w_ausfuehrung):
    daten = client.get(BASIS + "abgleich/", **kopf(w_instanz)).json()
    assert daten["konflikte"] == []


def test_ungueltige_angabe_wird_abgewiesen(client, w_instanz, w_ausfuehrung):
    antwort = client.get(BASIS + "abgleich/?ausfuehrungen=kaputt", **kopf(w_instanz))
    assert antwort.status_code == 400


def test_meldung_zu_unbekannter_ausfuehrung_ist_404_und_baut_nichts(
    client, w_instanz, w_ausfuehrung
):
    n = copy.deepcopy(ereignis_nachricht(w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=1))
    n["quelle"]["ausfuehrung_id"] = str(uuid.uuid4())
    from steuerung.werkbank_vertrag import inhaltshash

    n["inhaltshash"] = inhaltshash(n)
    assert senden(client, w_instanz, "ereignisse/", n).status_code == 404
    assert WerkbankAusfuehrung.objects.count() == 1


@pytest.mark.parametrize("weg", ["warten", "festlegen", "melden"])
def test_lieferwege_aendern_keine_aktive_werkbank(w_ausfuehrung, akteur, weg):
    from steuerung import lieferungen

    a = w_ausfuehrung.auftrag
    with pytest.raises(dienst.Konflikt, match="Werkbank"):
        if weg == "warten":
            lieferungen.warten(akteur, a, a.revision_nr, text="x", bedarf=[], nur_lieferungen=True)
        elif weg == "festlegen":
            lieferungen.festlegen(akteur, a, a.revision_nr, bedarf=[], nur_lieferungen=True)
        else:
            lieferungen.melden(
                akteur,
                a,
                a.revision_nr,
                lieferkennung="x",
                zusammenfassung="x",
                dateien=[],
                pruefungen=[],
            )


# --- SKP-01: eine neue Bitte schließt die ältere (KTL-1, F-7a405… durch F-3303…) --------------

ALT_PAKET = "p1-A-20261109183356-5d6490-p1"


def _bitte(client, ausf, inst, freigabe_id, pakete, art="uebernahme", **mehr):
    ereignis(
        client, ausf, inst, "freigabe_noetig",
        {"freigabe_id": freigabe_id, "art": art, "pakete": pakete, "grund": "x", **mehr},
    )  # fmt: skip


def _bedarf(ausf, freigabe_id):
    return next(b for b in ausf.bedarf if b.get("freigabe_id") == freigabe_id)


def test_neue_bitte_gleicher_art_schliesst_die_aeltere(client, fertig, w_instanz):
    _bitte(client, fertig, w_instanz, "F-alt", [ALT_PAKET])
    _bitte(client, fertig, w_instanz, "F-neu", ["p1"], kandidaten=["k1"])
    alt, neu = _bedarf(fertig, "F-alt"), _bedarf(fertig, "F-neu")
    assert alt["offen"] is False and alt["ersetzt_durch"] == "F-neu"
    assert neu["offen"] is True


@pytest.mark.parametrize(
    ("pakete", "art"), [(["p2"], "uebernahme"), (["p1"], "start"), ([], "uebernahme")]
)
def test_bitte_fuer_andere_pakete_oder_art_laesst_die_aeltere_offen(
    client, fertig, w_instanz, pakete, art
):
    _bitte(client, fertig, w_instanz, "F-alt", [ALT_PAKET])
    _bitte(client, fertig, w_instanz, "F-neu", pakete, art=art)
    assert _bedarf(fertig, "F-alt")["offen"] is True


def test_erteilte_freigabe_auf_die_aeltere_bleibt_bestehen(client, akteur, fertig, w_instanz):
    _bitte(client, fertig, w_instanz, "F-alt", [ALT_PAKET])
    aktion = werkbank.aktion_erstellen(akteur, fertig, "uebernahme_freigeben", freigabe_id="F-alt")
    _bitte(client, fertig, w_instanz, "F-neu", ["p1"], kandidaten=["k1"])
    aktion.refresh_from_db()
    assert aktion.zustand == "ausstehend"


def _ktl1_bericht():
    return [
        {"paket": "p1", "titel": "Paket", "zustand": "erledigt", "ersetzt": [ALT_PAKET]},
        {"paket": ALT_PAKET, "titel": "Altname", "zustand": "ersetzt"},
    ]


def test_ktl1_altbestand_offene_alte_bitte_hindert_den_abschluss_nicht_mehr(
    client, akteur, fertig, w_instanz
):
    """Stand auf dem Server vor SKP-01: alte Bitte offen, jüngere Bitte gleicher Art für dasselbe
    Paket schon angewendet. Der nächste Abschlussbericht der Werkbank muss durchgehen."""
    _bitte(client, fertig, w_instanz, "F-alt", [ALT_PAKET])
    _bitte(client, fertig, w_instanz, "F-neu", ["p1"], kandidaten=["k1"])
    bedarf = [dict(b) for b in fertig.bedarf]
    for b in bedarf:
        b.pop("ersetzt_durch", None)
        b["offen"] = b.get("freigabe_id") == "F-alt"
    WerkbankAusfuehrung.objects.filter(pk=fertig.pk).update(
        bedarf=bedarf, zustand="wartet_freigabe"
    )
    fertig.refresh_from_db()
    abschluss(client, fertig, w_instanz, paketplan=_ktl1_bericht())
    assert fertig.zustand == "abschluss_gemeldet"
    assert _bedarf(fertig, "F-alt")["ersetzt_durch"] == "F-neu"
    fertig.auftrag.refresh_from_db()
    assert fertig.auftrag.zustand == Auftrag.ERLEDIGT


def test_offene_alte_bitte_ohne_juengere_hindert_den_abschluss_weiter(client, fertig, w_instanz):
    _bitte(client, fertig, w_instanz, "F-alt", [ALT_PAKET])
    body = abschluss(client, fertig, w_instanz, status=409, paketplan=_ktl1_bericht())
    assert "Es wartet noch" in " ".join(body["fehler"]["details"]["maengel"])


def test_weiterbauen_nur_bei_offenem_fehler(client, owner_client, akteur, fertig, w_instanz):
    """Kriterium 3 von SKP-01; von Anfang an grün: `kann_weiterbauen` prüft schon nur offenen
    Bedarf. Der Test hält das fest."""
    from steuerung import views_werkbank

    ereignis(
        client, fertig, w_instanz, "fehler",
        {"paket": "p1", "code": "X", "text": "kaputt", "ausgang_unklar": False},
    )  # fmt: skip
    assert views_werkbank._ausfuehrung_daten(fertig)["kann_weiterbauen"] is True
    aktion = werkbank.aktion_erstellen(
        akteur, fertig, "weiterbauen", erwartet_zustand=fertig.zustand
    )
    ereignis(
        client, fertig, w_instanz, "aktion_angewendet",
        {"aktion_id": str(aktion.aktion_id), "ergebnis": "angewendet"},
    )  # fmt: skip
    assert views_werkbank._ausfuehrung_daten(fertig)["kann_weiterbauen"] is False

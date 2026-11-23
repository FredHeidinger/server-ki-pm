import uuid

import pytest
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Auftrag, Entscheidung, Modul
from tests.conftest import arbeitspaket
from tests.test_api import api, post

pytestmark = pytest.mark.django_db


@pytest.fixture
def planer(akteur):
    _, schluessel = dienst.bindung_anlegen(akteur, None, name="Planungszugang")
    return api(schluessel)


def planen(planer, auftrag, **daten):
    return post(
        planer,
        f"/api/auftraege/{auftrag.pk}/planen/",
        anfrage_kennung=uuid.uuid4().hex,
        **daten,
    )


def test_r1_freigabe_verlangt_eingetragenen_auftrag_je_kriterium(akteur, projekt, planer):
    verbraucher = arbeitspaket(akteur, projekt, "Anschluss nutzen", "verbraucher/**")
    anbieter = arbeitspaket(akteur, projekt, "Anschluss liefern", "anbieter/**")
    stand = verbraucher.revision_nr
    regel = {"text": "Export ist anschließbar", "erfuellbar_durch": f"auftrag:{anbieter.nummer}"}

    abgelehnt = planen(planer, verbraucher, abnahmekriterien=[regel], freigeben=True)
    assert abgelehnt.status_code == 409 and "Kriterium 1" in abgelehnt.json()["fehler"]
    assert anbieter.nummer in abgelehnt.json()["fehler"]
    verbraucher.refresh_from_db()
    assert verbraucher.revision_nr == stand and verbraucher.zustand == Auftrag.ENTWURF

    dienst.vorgaenger_eintragen(akteur, verbraucher, anbieter)
    Auftrag.objects.filter(pk=anbieter.pk).update(zustand=Auftrag.ERLEDIGT)
    angenommen = planen(planer, verbraucher, abnahmekriterien=[regel], freigeben=True)
    assert angenommen.status_code == 200, angenommen.content
    detail = angenommen.json()["auftrag"]
    assert detail["abnahmekriterien"] == [regel["text"]]
    assert detail["kriterien"][0] == {"nummer": 1, **regel, "nur_mensch": False}


def test_r1_freds_kriterium_benoetigt_offene_projektentscheidung(akteur, projekt, planer):
    auftrag = arbeitspaket(akteur, projekt)
    regel = {"text": "Fred entscheidet über Datenexport", "erfuellbar_durch": "fred"}
    abgelehnt = planen(planer, auftrag, abnahmekriterien=[regel], freigeben=True)
    assert abgelehnt.status_code == 409 and "offene Entscheidung" in abgelehnt.json()["fehler"]
    e = Entscheidung.objects.create(
        titel="Export", text="Export freigeben?", vorgeschlagen_von_name="Test"
    )
    e.betroffene_projekte.add(projekt)
    erlaubt = planen(
        planer, auftrag, abnahmekriterien=[regel | {"entscheidung": e.nummer}], freigeben=True
    )
    assert erlaubt.status_code == 200, erlaubt.content
    assert erlaubt.json()["auftrag"]["kriterien"][0]["entscheidung"] == e.nummer


def test_r1_geplanter_lieferbedarf_genuegt_fuer_externes_kriterium(akteur, projekt, planer):
    a = arbeitspaket(akteur, projekt, "Anschluss planen", "anschluss/**")
    b = arbeitspaket(akteur, projekt, "Anbieter planen", "anbieter/**")
    entwurf = planen(
        planer,
        a,
        abnahmekriterien=[{"text": "Anschluss geprüft", "erfuellbar_durch": f"auftrag:{b.nummer}"}],
    )
    assert entwurf.status_code == 200, entwurf.content
    a.refresh_from_db()
    bedarf = {"auftrag": b.nummer, "revision": b.revision_nr, "lieferkennung": "anschluss"}
    antwort = post(
        planer,
        f"/api/auftraege/{a.pk}/lieferbedarf/festlegen/",
        anfrage_kennung=uuid.uuid4().hex,
        revision=a.revision_nr,
        bedarf=[bedarf],
        nur_lieferungen=True,
    )
    assert antwort.status_code == 200, antwort.content
    assert antwort.json()["auftrag"]["geplanter_lieferbedarf"] == [bedarf | {"nach_lieferung": 0}]
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and not a.hat_bearbeiter
    umbenannt = planen(planer, a, titel="Anschluss mit neuem Titel")
    assert umbenannt.status_code == 200, umbenannt.content
    assert umbenannt.json()["auftrag"]["geplanter_lieferbedarf"] == [bedarf | {"nach_lieferung": 0}]
    a.refresh_from_db()
    frei = planen(planer, a, freigeben=True)
    assert frei.status_code == 200, frei.content
    assert frei.json()["auftrag"]["zustand"] == Auftrag.FREIGEGEBEN


def test_r2_kriterium_schliesst_kreis_mit_vorgaenger(akteur, projekt, planer):
    a = arbeitspaket(akteur, projekt, "Kreis eins", "eins/**")
    b = arbeitspaket(akteur, projekt, "Kreis zwei", "zwei/**")
    dienst.vorgaenger_eintragen(akteur, b, a)
    stand = a.revision_nr
    antwort = planen(
        planer,
        a,
        abnahmekriterien=[{"text": "Rückgabe da", "erfuellbar_durch": f"auftrag:{b.nummer}"}],
    )
    assert (
        antwort.status_code == 409
        and f"{a.nummer} -> {b.nummer} -> {a.nummer}" in antwort.json()["fehler"]
    )
    a.refresh_from_db()
    assert a.revision_nr == stand


def test_r3_nur_integration_darf_mehrere_module_in_kriterien_binden(akteur, projekt, planer):
    a = arbeitspaket(akteur, projekt)
    Modul.objects.create(projekt=projekt, kennung="export", name="Export")
    kriterien = [
        {"text": "Kern angebunden", "erfuellbar_durch": "eigen", "modul": "kern"},
        {"text": "Export angebunden", "erfuellbar_durch": "eigen", "modul": "export"},
    ]
    abgelehnt = planen(planer, a, abnahmekriterien=kriterien, freigeben=True)
    assert abgelehnt.status_code == 409 and "Integration" in abgelehnt.json()["fehler"]
    erlaubt = planen(planer, a, art="integration", abnahmekriterien=kriterien, freigeben=True)
    assert erlaubt.status_code == 200, erlaubt.content
    assert erlaubt.json()["auftrag"]["art"] == "integration"


def test_r3_freigegebene_integration_wartet_auf_nachtraeglichen_vorgaenger(
    akteur, projekt, planer, bindung
):
    a = arbeitspaket(akteur, projekt, "Integration", "integration/**")
    b = arbeitspaket(akteur, projekt, "Vorgänger", "vorgaenger/**")
    assert planen(planer, a, art="integration", freigeben=True).status_code == 200
    a.refresh_from_db()
    dienst.vorgaenger_eintragen(akteur, a, b)
    worker = api(bindung.klartext)
    assert not worker.get(f"/api/auftraege/{a.pk}/").json()["auftrag"]["uebernehmbar"]
    pfad = f"/api/auftraege/{a.pk}/uebernehmen/"
    assert (
        post(worker, pfad, revision=a.revision_nr, anfrage_kennung=uuid.uuid4().hex).status_code
        == 409
    )
    Auftrag.objects.filter(pk=b.pk).update(zustand=Auftrag.ERLEDIGT)
    assert (
        post(worker, pfad, revision=a.revision_nr, anfrage_kennung=uuid.uuid4().hex).status_code
        == 200
    )


def test_alte_planung_bewahrt_zuordnung_unveraenderter_kriterien(akteur, projekt, planer):
    a = arbeitspaket(akteur, projekt)
    k = {"text": "Manuelle Prüfung", "erfuellbar_durch": "eigen", "nur_mensch": True}
    assert planen(planer, a, abnahmekriterien=[k]).status_code == 200
    altaufruf = planen(planer, a, titel="Neuer Titel", abnahmekriterien=[k["text"]])
    assert altaufruf.json()["auftrag"]["kriterien"] == [{"nummer": 1, **k}]
    assert planen(planer, a, abnahmekriterien=["Ersatz ohne Zuordnung"]).status_code == 409


@pytest.mark.parametrize("markierung", ["auftrag", "kriterium"])
def test_r7_unbeaufsichtigt_sieht_menschenschritt_aber_uebernimmt_ihn_nicht(
    akteur, projekt, planer, bindung, markierung
):
    a = arbeitspaket(akteur, projekt)
    daten = (
        {"nur_mensch": True}
        if markierung == "auftrag"
        else {
            "abnahmekriterien": [
                {
                    "text": "Fred löscht nach Prüfung",
                    "erfuellbar_durch": "eigen",
                    "nur_mensch": True,
                }
            ]
        }
    )
    vorbereitet = planen(planer, a, freigeben=True, **daten)
    assert vorbereitet.status_code == 200, vorbereitet.content
    a.refresh_from_db()
    c = api(bindung.klartext)
    liste = c.get("/api/auftraege/?nur_meine=1&unbeaufsichtigt=1").json()
    assert [e["nummer"] for e in liste["wartet_auf_fred"]] == [a.nummer]
    assert all(e["nummer"] != a.nummer for e in liste["auftraege"])
    pfad = f"/api/auftraege/{a.pk}/uebernehmen/"
    abgelehnt = post(
        c, pfad, anfrage_kennung=uuid.uuid4().hex, revision=a.revision_nr, unbeaufsichtigt=True
    )
    assert abgelehnt.status_code == 409 and "Wartet auf Fred" in abgelehnt.json()["fehler"]
    a.refresh_from_db()
    assert a.zustand == Auftrag.FREIGEGEBEN and not a.hat_bearbeiter
    begleitet = post(c, pfad, anfrage_kennung=uuid.uuid4().hex, revision=a.revision_nr)
    assert begleitet.status_code == 200


def test_r7_unbeaufsichtigter_eigener_auftrag_bleibt_uebernehmbar(freigegeben, bindung):
    a = freigegeben
    c = api(bindung.klartext)
    liste = c.get("/api/auftraege/?unbeaufsichtigt=true").json()
    assert liste["wartet_auf_fred"] == [] and liste["auftraege"][0]["uebernehmbar"]
    antwort = post(
        c,
        f"/api/auftraege/{a.pk}/uebernehmen/",
        anfrage_kennung=uuid.uuid4().hex,
        revision=a.revision_nr,
        unbeaufsichtigt=True,
    )
    assert antwort.status_code == 200


def test_r8_lesen_und_projektkontext_nennen_dieselbe_regelfassung(akteur, projekt, bindung):
    a = arbeitspaket(akteur, projekt)
    regeln = akte.text_anlegen(akteur, projekt, titel="Auftragsregeln", text="Erste Fassung")
    akte.text_neue_version(akteur, regeln, text="Zweite Fassung", erwartete_nr=regeln.aktuelle_nr)
    c = api(bindung.klartext)
    erwartet = [{"kennung": regeln.kennung, "stand": 2}]
    assert c.get(f"/api/auftraege/{a.pk}/").json()["auftrag"]["auftragsregeln"] == erwartet
    kontext = c.get(reverse("api_projektkontext"))
    assert kontext.status_code == 200, kontext.content
    assert kontext.json()["manifest"]["auftragsregeln"] == erwartet

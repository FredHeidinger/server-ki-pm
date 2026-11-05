"""Nummern je Projekt: NEX-12 statt A-171; die alten A-Nummern bleiben dauerhaft gültig."""

import pytest
from django.test import Client
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Projekt
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def _neu(akteur, projekt, titel):
    return arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel}/**")


# --- Kürzel ------------------------------------------------------------------------------------


def test_kuerzel_wird_aus_dem_namen_vorgeschlagen(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="vehiclelogger", name="VehicleLogger")
    assert p.kuerzel == "VEH"


def test_name_aus_mehreren_woertern_nimmt_die_anfangsbuchstaben(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="ui-werk", name="Ui Werk Statt")
    assert p.kuerzel == "UWS"


def test_vorschlag_weicht_vergebenem_aus(akteur):
    a = dienst.projekt_registrieren(akteur, kennung="nexus", name="Nexus")
    b = dienst.projekt_registrieren(akteur, kennung="nextstep", name="Nextstep")
    assert a.kuerzel == "NEX" and b.kuerzel != "NEX" and b.kuerzel.startswith("NE")


def test_kuerzel_kann_angegeben_werden_und_wird_gross(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="server", name="Server", kuerzel="kipm")
    assert p.kuerzel == "KIPM"


@pytest.mark.parametrize("falsch", ["A", "1AB", "AB-C", "ÄÖ", "ABCDEFGHI", "E F"])
def test_ungueltiges_kuerzel_wird_abgelehnt(akteur, falsch):
    with pytest.raises(dienst.DienstFehler):
        dienst.projekt_registrieren(akteur, kennung="eins", name="Eins", kuerzel=falsch)
    assert not Projekt.objects.filter(kennung="eins").exists()


def test_doppeltes_kuerzel_wird_abgelehnt(akteur):
    dienst.projekt_registrieren(akteur, kennung="eins", name="Eins", kuerzel="ABC")
    with pytest.raises(dienst.DienstFehler):
        dienst.projekt_registrieren(akteur, kennung="zwei", name="Zwei", kuerzel="abc")


def test_projekt_ohne_buchstaben_im_namen_bekommt_trotzdem_ein_kuerzel(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="p-123", name="123")
    assert len(p.kuerzel) >= 2 and p.kuerzel[0].isalpha()


# --- Zählung -----------------------------------------------------------------------------------


def test_jedes_projekt_zaehlt_ohne_luecken_fuer_sich(akteur, projekt, projekt_b):
    a1 = _neu(akteur, projekt, "a1")
    b1 = _neu(akteur, projekt_b, "b1")
    a2 = _neu(akteur, projekt, "a2")
    b2 = _neu(akteur, projekt_b, "b2")
    assert (a1.projekt_nummer, a2.projekt_nummer) == (1, 2)
    assert (b1.projekt_nummer, b2.projekt_nummer) == (1, 2)
    assert a1.nummer == f"{projekt.kuerzel}-1" and b2.nummer == f"{projekt_b.kuerzel}-2"


def test_alte_nummer_bleibt_erhalten(akteur, projekt):
    a = _neu(akteur, projekt, "a")
    assert a.alte_nummer == f"A-{a.pk}"


def test_abgebrochener_eintrag_gibt_seine_nummer_nicht_frei(akteur, projekt):
    a1 = _neu(akteur, projekt, "a1")
    dienst.auftrag_abbrechen(akteur, a1, "Probe")
    a2 = _neu(akteur, projekt, "a2")
    assert a2.projekt_nummer == 2


# --- Suche nach Nummer -------------------------------------------------------------------------


def test_finden_mit_allen_schreibweisen(akteur, projekt):
    _neu(akteur, projekt, "davor")
    a = _neu(akteur, projekt, "gesucht")
    k = projekt.kuerzel
    for text in (f"{k}-2", f"{k.lower()}-2", f" {k}-2 ", f"A-{a.pk}", f"a-{a.pk}", str(a.pk)):
        assert dienst.auftrag_id_aus_nummer(text) == a.pk, text


@pytest.mark.parametrize("falsch", ["", "XX", "NEX-", "-1", "A-", "NEX-1-2", "A-1x"])
def test_unsinnige_nummer_wird_abgelehnt(falsch):
    with pytest.raises(dienst.DienstFehler):
        dienst.auftrag_id_aus_nummer(falsch)


def test_unbekannte_nummer_gilt_als_nicht_gefunden(projekt):
    with pytest.raises(dienst.KeinZugriff):
        dienst.auftrag_id_aus_nummer(f"{projekt.kuerzel}-99")
    with pytest.raises(dienst.KeinZugriff):
        dienst.auftrag_id_aus_nummer("ZZZ-1")


# --- Schnittstelle -----------------------------------------------------------------------------


def test_mcp_liest_mit_neuer_und_alter_nummer(client, akteur, projekt, pc_zugang):
    a = _neu(akteur, projekt, "a")
    for text in (a.nummer, a.alte_nummer):
        fehler, daten = _werkzeug(
            client, pc_zugang.klartext, "kipm_lesen", projekt=projekt.kennung, auftrag=text
        )
        assert not fehler, daten
        assert daten["auftrag"]["id"] == a.pk
        assert daten["auftrag"]["nummer"] == a.nummer
        assert daten["auftrag"]["alte_nummer"] == a.alte_nummer


def test_mcp_arbeitet_mit_neuer_nummer(client, akteur, projekt, pc_zugang):
    a = _neu(akteur, projekt, "a")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_uebernehmen",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
    )
    assert not fehler, daten
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_fortschritt",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
        text="Läuft.",
    )
    assert not fehler, daten
    assert a.meldungen.filter(text="Läuft.").count() == 1


def test_projektschluessel_findet_fremde_nummer_nicht(client, akteur, projekt, projekt_b, bindung):
    fremd = _neu(akteur, projekt_b, "fremd")
    for text in (fremd.nummer, fremd.alte_nummer):
        fehler, daten = _werkzeug(client, bindung.klartext, "kipm_lesen", auftrag=text)
        assert fehler, daten
        assert "fremd" not in str(daten)


def test_mcp_projekt_anlegen_nimmt_kuerzel(client, pc_zugang):
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_projekt_anlegen", name="Neu", kuerzel="NEU"
    )
    assert not fehler, daten
    assert daten["projekt"]["kuerzel"] == "NEU"


def test_sitzungsstart_nennt_die_neue_nummer(akteur, projekt, pc_zugang):
    a = _neu(akteur, projekt, "a")
    dienst.auftrag_freigeben(akteur, a)
    c = Client(
        headers={"Authorization": f"Bearer {pc_zugang.klartext}", "X-KIPM-Projekt": projekt.kennung}
    )
    antwort = c.get("/api/sitzungsstart/").json()
    assert antwort["naechster"]["nummer"] == a.nummer


# --- Oberfläche --------------------------------------------------------------------------------


def test_auftragsseite_nennt_die_fruehere_nummer(owner_client, akteur, projekt):
    a = _neu(akteur, projekt, "a")
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}))
    text = seite.content.decode("utf-8")
    assert a.nummer in text and f"früher {a.alte_nummer}" in text


def test_neues_projekt_im_formular_mit_kuerzel(owner_client):
    antwort = owner_client.post(
        reverse("projekt_neu"), {"name": "Formular", "kuerzel": "frm", "module": ""}
    )
    assert antwort.status_code == 302
    assert Projekt.objects.get(name="Formular").kuerzel == "FRM"


def test_formular_meldet_doppeltes_kuerzel(owner_client, akteur):
    dienst.projekt_registrieren(akteur, kennung="eins", name="Eins", kuerzel="ABC")
    antwort = owner_client.post(reverse("projekt_neu"), {"name": "Zwei", "kuerzel": "ABC"})
    assert antwort.status_code == 200
    assert "ABC" in antwort.content.decode("utf-8")
    assert not Projekt.objects.filter(name="Zwei").exists()


def test_eindeutig_je_projekt_auch_in_der_datenbank(akteur, projekt):
    from django.db import IntegrityError, transaction

    a = _neu(akteur, projekt, "a")
    b = _neu(akteur, projekt, "b")
    with pytest.raises(IntegrityError), transaction.atomic():
        Auftrag.objects.filter(pk=b.pk).update(projekt_nummer=a.projekt_nummer)


# --- Gegenprüfung: Verlauf von früher, keine Auskunft über fremde Nummern ------------------------


def test_auftragsseite_zeigt_den_verlauf_von_vor_der_umstellung(owner_client, akteur, projekt):
    a = _neu(akteur, projekt, "a")
    Ereignis.objects.filter(objekt_typ="Auftrag", objekt_kennung=a.nummer).update(
        objekt_kennung=a.alte_nummer
    )
    alt = Ereignis.objects.filter(objekt_kennung=a.alte_nummer).count()
    assert alt >= 1
    dienst.auftrag_freigeben(akteur, a)
    neu = Ereignis.objects.filter(objekt_kennung=a.nummer).count()
    assert neu >= 1
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}))
    assert f"Änderungen am Auftrag ({alt + neu})" in seite.content.decode("utf-8")


def test_verlauf_fremder_auftraege_bleibt_getrennt(owner_client, akteur, projekt, projekt_b):
    a = _neu(akteur, projekt, "a")
    b = _neu(akteur, projekt_b, "b")
    assert a.nummer != b.nummer and a.projekt_nummer == b.projekt_nummer
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}))
    anzahl = Ereignis.objects.filter(objekt_kennung=a.nummer).count()
    assert f"Änderungen am Auftrag ({anzahl})" in seite.content.decode("utf-8")


@pytest.mark.parametrize(
    "werkzeug, felder",
    [("kipm_fortschritt", {"text": "x"}), ("kipm_planen", {}), ("kipm_antworten", {"text": "x"})],
)
def test_projektschluessel_erkennt_nicht_ob_eine_nummer_existiert(
    client, akteur, projekt_b, bindung, werkzeug, felder
):
    fremd = _neu(akteur, projekt_b, "fremd")
    antworten = [
        _werkzeug(client, bindung.klartext, werkzeug, auftrag=text, fred_sagte=None, **felder)[1]
        for text in (fremd.nummer, f"{projekt_b.kuerzel}-99", "ZZZ-1")
    ]
    assert antworten[0] == antworten[1] == antworten[2]


def test_filter_abgebrochen_zeigt_die_treffer_aufgeklappt(owner_client, akteur, projekt):
    a = _neu(akteur, projekt, "weg")
    dienst.auftrag_abbrechen(akteur, a, "Probe")
    url = reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    assert (
        'id="gruppe-abgebrochen" open'
        in owner_client.get(url + "?zustand=ABGEBROCHEN").content.decode()
    )
    assert 'id="gruppe-abgebrochen" open' not in owner_client.get(url).content.decode()

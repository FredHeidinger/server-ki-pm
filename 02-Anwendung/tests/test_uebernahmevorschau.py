import uuid
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from steuerung import dienst
from steuerung.models import (
    Abhaengigkeit,
    Auftrag,
    ChatBindung,
    Lieferbedarf,
    Lieferung,
    Meldung,
    Ordnersperre,
    UebernahmePruefrecht,
)
from tests.conftest import arbeitspaket
from tests.test_api import api, post
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.fixture
def vorschau_umfeld(akteur, projekt, bindung):
    ort = projekt.arbeitsorte.get()
    ort.pfad = r"C:\Tests\ProjectPulse"
    ort.save(update_fields=["pfad"])
    auftrag = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, auftrag)
    auftrag.refresh_from_db()
    ziel, schluessel = dienst.bindung_anlegen(akteur, projekt, name="Claude Test Desktop")
    ziel.klartext = schluessel
    return auftrag, bindung, ziel


def pruefen(client, umfeld, **werte):
    auftrag, quelle, ziel = umfeld
    parameter = {
        "auftrag": auftrag.nummer,
        "revision": auftrag.revision_nr,
        "zielbindung_id": ziel.pk,
        "unbeaufsichtigt": True,
    } | werte
    with CaptureQueriesContext(connection) as sql:
        fehler, ergebnis = _werkzeug(
            client,
            quelle.klartext,
            "kipm_uebernahme_pruefen",
            projekt=auftrag.projekt.kennung,
            fred_sagte=None,
            **parameter,
        )
    assert all(q["sql"].lstrip().upper().startswith("SELECT ") for q in sql.captured_queries)
    return fehler, ergebnis


def pruefrecht(umfeld, *optionen):
    auftrag, quelle, ziel = umfeld
    call_command("uebernahme_pruefrecht", auftrag.projekt.kennung, quelle.pk, ziel.pk, *optionen)


def uebernehmen(umfeld, **werte):
    auftrag, _, ziel = umfeld
    parameter = {
        "revision": auftrag.revision_nr,
        "unbeaufsichtigt": True,
        "anfrage_kennung": uuid.uuid4().hex,
    } | werte
    return post(api(ziel.klartext), reverse("api_uebernehmen", args=[auftrag.pk]), **parameter)


def test_vorschau_ist_projekt_und_zielgebunden_und_gibt_keinen_zielschluessel_aus(
    client, vorschau_umfeld, bindung_b
):
    auftrag, quelle, ziel = vorschau_umfeld
    fehler, daten = pruefen(client, vorschau_umfeld)
    assert fehler and "fehlt das Prüfrecht" in daten["fehler"]
    with pytest.raises(CommandError, match="gültig"):
        pruefrecht((auftrag, quelle, bindung_b))

    pruefrecht(vorschau_umfeld)
    pruefrecht(vorschau_umfeld)
    assert UebernahmePruefrecht.objects.count() == 1
    fehler, daten = pruefen(client, vorschau_umfeld, zielbindung_id=bindung_b.pk)
    assert fehler and "fehlt das Prüfrecht" in daten["fehler"]
    fehler, daten = pruefen(client, vorschau_umfeld)
    assert not fehler and daten["uebernehmbar"] is True
    assert daten["revision_geprueft"] == daten["revision_angefragt"] == auftrag.revision_nr
    assert timezone.is_aware(timezone.datetime.fromisoformat(daten["pruefzeitpunkt"]))
    assert ziel.klartext not in str(daten) and ziel.schluessel_hash not in str(daten)
    assert not Ordnersperre.objects.exists()
    auftrag.refresh_from_db()
    assert not auftrag.hat_bearbeiter and auftrag.zustand == Auftrag.FREIGEGEBEN
    assert uebernehmen(vorschau_umfeld).status_code == 200
    auftrag.refresh_from_db()
    assert auftrag.bearbeiter_bindung_id == ziel.pk
    pruefrecht(vorschau_umfeld, "--entziehen")
    assert pruefen(client, vorschau_umfeld)[0]


@pytest.mark.parametrize(
    ("sperre", "code"),
    [
        ("revision", "REVISION_VERALTET"),
        ("vorgaenger", "VORGAENGER_OFFEN"),
        ("lieferung", "LIEFERBEDARF_OFFEN"),
        ("mensch", "MENSCHENSCHRITT"),
        ("ordner", "ORDNER_GESPERRT"),
    ],
)
def test_vorschau_und_tatsaechliche_uebernahme_nennen_dieselbe_sperre(
    client, vorschau_umfeld, akteur, sperre, code
):
    auftrag, _, _ = vorschau_umfeld
    pruefrecht(vorschau_umfeld)
    parameter = {}
    if sperre == "revision":
        parameter["revision"] = auftrag.revision_nr + 1
    elif sperre == "mensch":
        Auftrag.objects.filter(pk=auftrag.pk).update(nur_mensch=True)
    else:
        anderer = arbeitspaket(akteur, auftrag.projekt, "Andere Arbeit", "anderes/**")
        if sperre == "vorgaenger":
            Abhaengigkeit.objects.create(auftrag=auftrag, vorgaenger=anderer)
        elif sperre == "ordner":
            dienst.auftrag_freigeben(akteur, anderer)
            anderer.refresh_from_db()
            dienst.auftrag_uebernehmen(akteur, anderer, anderer.revision_nr)
        else:
            meldung = Meldung.objects.create(
                auftrag=auftrag,
                revision_nr=auftrag.revision_nr,
                art=Meldung.LIEFERPLAN,
                text="Planbedarf",
                akteur_name="Test",
            )
            Lieferbedarf.objects.create(
                meldung=meldung,
                lieferauftrag=anderer,
                lieferrevision=anderer.revision_nr,
                lieferkennung="schema",
            )
    vorher = list(Ordnersperre.objects.values())
    fehler, daten = pruefen(client, vorschau_umfeld, **parameter)
    assert not fehler and daten["uebernehmbar"] is False
    grund = next(e["grund"] for e in daten["sperrgruende"] if e["code"] == code)
    assert list(Ordnersperre.objects.values()) == vorher
    wirkung = uebernehmen(vorschau_umfeld, **parameter)
    assert wirkung.status_code == 409 and wirkung.json()["fehler"] == grund
    assert list(Ordnersperre.objects.values()) == vorher
    if sperre == "lieferung":
        liefermeldung = Meldung.objects.create(
            auftrag=anderer,
            revision_nr=anderer.revision_nr,
            art=Meldung.FORTSCHRITT,
            text="Schema geliefert",
            akteur_name="Test",
        )
        Lieferung.objects.create(
            meldung=liefermeldung,
            auftrag=anderer,
            revision_nr=anderer.revision_nr,
            lieferkennung="schema",
            zusammenfassung="Schema geliefert",
            dateien=[],
            pruefungen=[],
            inhalt_sha256="a" * 64,
        )
        assert pruefen(client, vorschau_umfeld)[1]["uebernehmbar"] is True
        assert uebernehmen(vorschau_umfeld).status_code == 200


def test_tatsaechliche_uebernahme_prueft_nach_der_vorschau_erneut(client, vorschau_umfeld, akteur):
    auftrag, _, _ = vorschau_umfeld
    pruefrecht(vorschau_umfeld)
    assert pruefen(client, vorschau_umfeld)[1]["uebernehmbar"] is True
    anderer = arbeitspaket(akteur, auftrag.projekt, "Schnellerer Auftrag", "anderes/**")
    dienst.auftrag_freigeben(akteur, anderer)
    anderer.refresh_from_db()
    dienst.auftrag_uebernehmen(akteur, anderer, anderer.revision_nr)
    antwort = uebernehmen(vorschau_umfeld)
    assert antwort.status_code == 409 and "Arbeitsordner" in antwort.json()["fehler"]


def test_vorschau_weist_einen_abgelaufenen_oder_widerrufenen_zielzugang_ab(client, vorschau_umfeld):
    _, _, ziel = vorschau_umfeld
    pruefrecht(vorschau_umfeld)
    ChatBindung.objects.filter(pk=ziel.pk).update(gueltig_bis=timezone.now() - timedelta(seconds=1))
    fehler, daten = pruefen(client, vorschau_umfeld)
    assert not fehler and daten["uebernehmbar"] is False
    assert daten["sperrgruende"][0]["code"] == "ZIELBINDUNG_UNGUELTIG"
    assert uebernehmen(vorschau_umfeld).status_code == 401


def test_pc_pruefrecht_gilt_nur_fuer_das_konfigurierte_projekt(
    client, vorschau_umfeld, akteur, projekt_b
):
    auftrag, _, _ = vorschau_umfeld
    quelle, quelle_klartext = dienst.bindung_anlegen(akteur, None, name="Codex PC")
    ziel, ziel_klartext = dienst.bindung_anlegen(akteur, None, name="Claude PC")
    quelle.klartext = quelle_klartext
    ziel.klartext = ziel_klartext
    umfeld = (auftrag, quelle, ziel)
    pruefrecht(umfeld)
    anderer = arbeitspaket(akteur, projekt_b)
    fehler, fremd = pruefen(client, (anderer, quelle, ziel))
    assert fehler and "fehlt das Prüfrecht" in fremd["fehler"]

    fehler, ergebnis = pruefen(client, umfeld)
    assert not fehler and ergebnis["uebernehmbar"] is True
    antwort = client.post(
        reverse("api_uebernehmen", args=[auftrag.pk]),
        data={
            "revision": auftrag.revision_nr,
            "unbeaufsichtigt": True,
            "anfrage_kennung": uuid.uuid4().hex,
        },
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {ziel_klartext}",
        HTTP_X_KIPM_PROJEKT=auftrag.projekt.kennung,
    )
    assert antwort.status_code == 200, antwort.content

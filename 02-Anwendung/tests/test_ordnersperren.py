import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Revision
from tests.conftest import arbeitspaket
from tests.test_api import api, post

pytestmark = pytest.mark.django_db


def melden(client, auftrag, name, **daten):
    return post(
        client,
        reverse(name, args=[auftrag.pk]),
        revision=auftrag.revision_nr,
        anfrage_kennung=uuid.uuid4().hex,
        **daten,
    )


@pytest.fixture
def ordnerauftraege(akteur, projekt):
    ort = projekt.arbeitsorte.get()
    ort.pfad = r"C:\Projekte\Arbeitskopie"
    ort.save(update_fields=["pfad"])
    a = arbeitspaket(akteur, projekt, "Erste Arbeit", "eins/**")
    b = arbeitspaket(akteur, projekt, "Zweite Arbeit", "zwei/**")
    # Zwei Arbeitsortdatensätze und getrennte Schreibumfänge bezeichnen denselben Ordner.
    zweiter_ort = projekt.arbeitsorte.create(
        kennung="repo-zwei", name="Zweiter Alias", rechner="pc", pfad="c:/projekte/arbeitskopie/."
    )
    Revision.objects.filter(auftrag=b, nummer=b.revision_nr).update(arbeitsort=zweiter_ort)
    for auftrag in (a, b):
        dienst.auftrag_freigeben(akteur, auftrag)
        auftrag.refresh_from_db()
    return a, b


def test_ordnersperre_blockiert_anderen_auftrag_bis_sitzungsende(ordnerauftraege, bindung):
    a, b = ordnerauftraege
    client = api(bindung.klartext)
    erste = melden(client, a, "api_uebernehmen")
    assert erste.status_code == 200, erste.content
    zweite = melden(client, b, "api_uebernehmen")
    assert zweite.status_code == 409 and a.nummer in zweite.json()["fehler"]
    sperre = erste.json()["auftrag"]["ordnersperre"]
    assert sperre["auftrag"] == a.nummer and sperre["halter"] == bindung.name
    assert timezone.datetime.fromisoformat(sperre["ablauf"]) > timezone.now()
    liste = client.get(reverse("api_auftraege")).json()["auftraege"]
    eintrag = next(e for e in liste if e["nummer"] == b.nummer)
    assert eintrag["ordnersperre"] == sperre and eintrag["uebernehmbar"] is False
    detail = client.get(reverse("api_auftrag", args=[b.pk])).json()["auftrag"]
    assert detail["ordnersperre"] == sperre
    assert melden(client, a, "api_sitzung_beendet").json()["vermerkt"] is True
    assert melden(client, b, "api_uebernehmen").status_code == 200
    spaet = melden(client, a, "api_fortschritt", text="Verspätete Weiterarbeit")
    assert spaet.status_code == 409 and b.nummer in spaet.json()["fehler"]


def test_ordnersperre_verlaengert_sich_und_wird_beim_hindernis_frei(
    ordnerauftraege, bindung, monkeypatch
):
    a, b = ordnerauftraege
    client = api(bindung.klartext)
    jetzt = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: jetzt)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    jetzt += timedelta(minutes=40)
    antwort = melden(client, a, "api_fortschritt", text="Weiter")
    assert antwort.status_code == 200
    assert timezone.datetime.fromisoformat(antwort.json()["auftrag"]["ordnersperre"]["ablauf"]) == (
        jetzt + timedelta(minutes=60)
    )
    jetzt += timedelta(minutes=25)
    assert melden(client, b, "api_uebernehmen").status_code == 409
    assert melden(client, a, "api_hindernis", text="Nicht im Repo lösbar").status_code == 200
    assert melden(client, b, "api_uebernehmen").status_code == 200


def test_ordnersperre_fred_uebernimmt_explizit_mit_verlauf(
    ordnerauftraege, bindung, akteur, projekt
):
    a, b = ordnerauftraege
    client = api(bindung.klartext)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    abgelehnt = melden(client, a, "api_uebernehmen", ordnersperre_uebernehmen=True)
    assert abgelehnt.status_code == 403
    _, pc = dienst.bindung_anlegen(akteur, None, name="Fred am PC")
    abgelehnt = melden(api(pc), a, "api_uebernehmen", ordnersperre_uebernehmen=True, fred_sagte="")
    assert abgelehnt.status_code == 400
    uebernommen = melden(
        api(pc),
        a,
        "api_uebernehmen",
        ordnersperre_uebernehmen=True,
        fred_sagte="Übernimm diesen Ordner jetzt.",
    )
    assert uebernommen.status_code == 200, uebernommen.content
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT and a.bearbeiter_name == "Fred am PC"
    assert Ereignis.objects.filter(
        projekt=projekt,
        objekt_kennung=a.nummer,
        ereignis="Ordnersperre ausdrücklich übernommen",
        anlass__contains="Übernimm diesen Ordner jetzt.",
    ).exists()
    assert melden(client, a, "api_fortschritt", text="Alte Sitzung").status_code == 409
    assert melden(api(pc), a, "api_sitzung_beendet").json()["vermerkt"] is True
    assert melden(client, b, "api_uebernehmen").status_code == 200


def test_ordnersperre_laeuft_ab_und_folgeauftrag_gibt_sie_beim_ergebnis_frei(
    ordnerauftraege, bindung, monkeypatch
):
    a, b = ordnerauftraege
    client = api(bindung.klartext)
    jetzt = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: jetzt)
    assert melden(client, a, "api_uebernehmen").status_code == 200
    jetzt += timedelta(minutes=60)
    assert melden(client, b, "api_uebernehmen").status_code == 200
    assert melden(client, a, "api_fortschritt", text="Weiter").status_code == 409
    fertig = melden(
        client, b, "api_ergebnis", zusammenfassung="Abgeschlossen", dateien=["datei@abc"]
    )
    assert fertig.status_code == 200 and fertig.json()["auftrag"]["ordnersperre"] is None
    assert melden(client, a, "api_fortschritt", text="Wiederaufnahme").status_code == 200


def test_freigegeben_wartet_sichtbar_bis_der_ordner_frei_ist(
    ordnerauftraege, bindung, owner_client
):
    a, b = ordnerauftraege
    client = api(bindung.klartext)
    liste = reverse("projekt_auftraege", args=[b.projekt.kennung])
    detail = reverse("auftrag_detail", args=[b.pk])
    meldung = f"Freigegeben – wartet auf {a.nummer}"

    assert melden(client, a, "api_uebernehmen").status_code == 200
    for url in (liste, liste + "?ansicht=liste", detail):
        assert meldung in owner_client.get(url).content.decode()
    assert b not in dienst.weiter_reihenfolge(b.projekt)
    b.refresh_from_db()
    assert b.zustand == Auftrag.FREIGEGEBEN

    auskunft = client.get(reverse("api_sitzungsstart")).json()
    assert auskunft["naechster"] is None
    assert next(e for e in auskunft["offen"] if e["nummer"] == b.nummer)["uebernehmbar"] is False
    assert melden(client, a, "api_sitzung_beendet").json()["vermerkt"] is True

    for url in (liste, liste + "?ansicht=liste", detail):
        assert meldung not in owner_client.get(url).content.decode()
    assert client.get(reverse("api_sitzungsstart")).json()["naechster"]["nummer"] == b.nummer


def test_verschiedene_ordner_koennen_gleichzeitig_uebernommen_werden(ordnerauftraege, bindung):
    a, b = ordnerauftraege
    ort = b.aktuelle_revision.arbeitsort
    ort.pfad = r"C:\Projekte\ZweiteArbeitskopie"
    ort.save(update_fields=["pfad"])
    client = api(bindung.klartext)

    assert melden(client, a, "api_uebernehmen").status_code == 200
    assert b in dienst.weiter_reihenfolge(b.projekt)
    assert melden(client, b, "api_uebernehmen").status_code == 200

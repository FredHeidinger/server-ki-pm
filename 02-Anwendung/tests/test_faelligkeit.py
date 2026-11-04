"""A-12: optionales Fälligkeitsdatum an Auftrag und Issue; ab dem Termin oben in „Wartet auf dich“."""

import datetime
import importlib

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst, mcp
from steuerung.models import Auftrag, Ereignis
from tests.conftest import arbeitspaket
from tests.test_klickwege import _aktion, _blockiert, _Klicker, _projekt_url, _Seite
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


def _tag(tage: int = 0) -> datetime.date:
    return timezone.localdate() + datetime.timedelta(days=tage)


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def _stand(a: Auftrag) -> tuple:
    """Was eine Terminänderung nicht berühren darf."""
    a = Auftrag.objects.get(pk=a.pk)
    return a.revision_nr, a.zustand, a.freigegeben_am, a.aktualisiert_am, a.revisionen.count()


def _wartet(client, projekt) -> str:
    html = client.get(_projekt_url(projekt)).content.decode()
    return html.split('id="h-wartet"')[1].split("</section>")[0]


def _ereignisse(a: Auftrag) -> list[str]:
    return list(
        Ereignis.objects.filter(objekt_typ="Auftrag", objekt_kennung=a.nummer)
        .order_by("id")
        .values_list("ereignis", flat=True)
    )


def _entwurf_zu(akteur, projekt, zustand, titel="Eintrag"):
    a = dienst.wunsch_erfassen(akteur, projekt, titel=titel, beschreibung="b")
    Auftrag.objects.filter(pk=a.pk).update(zustand=zustand)
    a.refresh_from_db()
    return a


# --- Setzen und entfernen: Chat und Fred, Revision und Freigabe bleiben ----------------------


@pytest.mark.parametrize("zugang", ["projektschluessel", "pc"])
def test_chat_setzt_und_entfernt_ohne_revision_und_freigabe(
    client, freigegeben, bindung, pc_zugang, zugang
):
    schluessel, zusatz = (
        (bindung.klartext, {})
        if zugang == "projektschluessel"
        else (pc_zugang.klartext, {"projekt": freigegeben.projekt.kennung})
    )
    vorher = _stand(freigegeben)
    assert vorher[1] == Auftrag.FREIGEGEBEN

    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_planen",
        auftrag=freigegeben.nummer,
        faellig="2026-12-24",
        **zusatz,
    )
    assert not fehler, daten
    assert daten["auftrag"]["faellig"] == "2026-12-24"
    assert daten["auftrag"]["zustand"] == Auftrag.FREIGEGEBEN
    assert _stand(freigegeben) == vorher
    assert "Fällig am 24.12.2026 gesetzt" in _ereignisse(freigegeben)

    fehler, daten = _werkzeug(
        client, schluessel, "kipm_planen", auftrag=freigegeben.nummer, faellig="", **zusatz
    )
    assert not fehler, daten
    assert daten["auftrag"]["faellig"] is None
    assert _stand(freigegeben) == vorher
    assert "Termin entfernt" in _ereignisse(freigegeben)


def test_laufender_auftrag_behaelt_bearbeiter(akteur, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, freigegeben.revision_nr)
    vorher = _stand(freigegeben)
    dienst.faellig_setzen(chat, freigegeben, _tag(3))
    freigegeben.refresh_from_db()
    assert _stand(freigegeben) == vorher
    assert freigegeben.zustand == Auftrag.IN_ARBEIT and freigegeben.bearbeiter_bindung == bindung


def test_fred_setzt_und_entfernt_auf_der_auftragsseite(owner_client, freigegeben):
    seite = reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
    vorher = _stand(freigegeben)
    html = owner_client.get(seite).content.decode()
    assert "kein Termin" in html

    owner_client.post(_aktion(freigegeben, "faellig"), {"faellig": "2026-12-24", "wahl": "setzen"})
    freigegeben.refresh_from_db()
    assert freigegeben.faellig == datetime.date(2026, 12, 24)
    assert _stand(freigegeben) == vorher
    html = owner_client.get(seite).content.decode()
    assert "fällig am 24.12.2026" in html and "kein Termin" not in html
    assert (
        'value="2026-12-24"' in html
    )  # Das Browserfeld zeigt das Datum, Speichern löscht es nicht
    assert "Fällig am 24.12.2026 gesetzt" in html  # im Verlauf des Auftrags

    owner_client.post(
        _aktion(freigegeben, "faellig"), {"faellig": "2026-12-24", "wahl": "entfernen"}
    )
    freigegeben.refresh_from_db()
    assert freigegeben.faellig is None and _stand(freigegeben) == vorher
    assert "Termin entfernt" in owner_client.get(seite).content.decode()


def test_gleiches_datum_schreibt_kein_ereignis(akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(3))
    zahl = len(_ereignisse(freigegeben))
    dienst.faellig_setzen(akteur, freigegeben, _tag(3))
    assert len(_ereignisse(freigegeben)) == zahl


@pytest.mark.parametrize("wert", ["morgen", "24.12.2026", "2026-13-01"])
def test_ungueltiges_datum_wird_abgewiesen(client, freigegeben, bindung, owner_client, wert):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_planen", auftrag=freigegeben.nummer, faellig=wert
    )
    assert fehler and "ungültig" in str(daten)
    antwort = owner_client.post(
        _aktion(freigegeben, "faellig"), {"faellig": wert, "wahl": "setzen"}, follow=True
    )
    assert "ungültig" in antwort.content.decode()
    freigegeben.refresh_from_db()
    assert freigegeben.faellig is None


def test_fremdes_projekt_gilt_als_nicht_vorhanden(client, freigegeben, bindung_b):
    fehler, daten = _werkzeug(
        client, bindung_b.klartext, "kipm_planen", auftrag=freigegeben.nummer, faellig="2026-12-24"
    )
    assert fehler and "Nicht gefunden" in str(daten)
    freigegeben.refresh_from_db()
    assert freigegeben.faellig is None


def test_zweiter_mensch_darf_nicht(zweiter_mensch, freigegeben):
    with pytest.raises(dienst.KeinZugriff):
        dienst.faellig_setzen(dienst.Akteur.aus_benutzer(zweiter_mensch), freigegeben, _tag(1))


@pytest.mark.parametrize("zustand", [Auftrag.ERLEDIGT, Auftrag.ABGEBROCHEN])
def test_abgeschlossenes_nimmt_keinen_termin(akteur, projekt, zustand):
    a = _entwurf_zu(akteur, projekt, zustand)
    with pytest.raises(dienst.Konflikt):
        dienst.faellig_setzen(akteur, a, _tag(1))


# --- Zusammenspiel mit Planen, Freigeben, Bearbeiten -----------------------------------------


def test_planen_mit_termin_und_freigabe_behaelt_beides(client, akteur, projekt, pc_zugang):
    a = arbeitspaket(akteur, projekt)
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        auftrag=a.nummer,
        projekt=projekt.kennung,
        faellig="2026-12-24",
        freigeben=True,
    )
    assert not fehler, daten
    assert daten["auftrag"]["zustand"] == Auftrag.FREIGEGEBEN
    assert daten["auftrag"]["faellig"] == "2026-12-24"


def test_planaenderung_hebt_freigabe_auf_termin_bleibt(client, akteur, freigegeben, pc_zugang):
    dienst.faellig_setzen(akteur, freigegeben, _tag(3))
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        auftrag=freigegeben.nummer,
        projekt=freigegeben.projekt.kennung,
        ziel="Neues Ziel",
    )
    assert not fehler, daten
    assert daten["auftrag"]["zustand"] == Auftrag.ENTWURF
    assert daten["auftrag"]["faellig"] == _tag(3).isoformat()


def test_abgewiesene_freigabe_nimmt_auch_den_termin_zurueck(client, akteur, projekt, pc_zugang):
    idee = dienst.wunsch_erfassen(akteur, projekt, titel="Nur eine Idee", beschreibung="b")
    fehler, _ = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        auftrag=idee.nummer,
        projekt=projekt.kennung,
        faellig="2026-12-24",
        freigeben=True,
    )
    assert fehler
    idee.refresh_from_db()
    assert idee.faellig is None and "Fällig am 24.12.2026 gesetzt" not in _ereignisse(idee)


def test_bearbeiten_von_hand_behaelt_den_termin(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(3))
    dienst.auftrag_bearbeiten(
        akteur,
        freigegeben,
        titel=freigegeben.titel,
        beschreibung="neu",
        ziel="z",
        nicht_ziele="n",
        arbeitsort=None,
        schreibumfang="a/**",
        quellen="",
        abnahmekriterien="k",
        prioritaet="mittel",
        modul=None,
    )
    freigegeben.refresh_from_db()
    assert freigegeben.faellig == _tag(3)


def test_anlegen_mit_termin_im_chat(client, projekt, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_auftrag_anlegen",
        titel="Auswertung",
        text="Am 10.11. auswerten",
        art="issue",
        faellig="2026-11-10",
    )
    assert not fehler, daten
    a = Auftrag.objects.get(titel="Auswertung")
    assert a.faellig == datetime.date(2026, 11, 10) and a.typ == Auftrag.TYP_ISSUE
    assert a.revision_nr == 1
    assert Ereignis.objects.filter(
        objekt_kennung=a.nummer, anlass__contains="fällig am 10.11.2026"
    ).exists()


def test_anlegen_mit_ungueltigem_termin_legt_nichts_an(client, projekt, bindung):
    fehler, _ = _werkzeug(
        client,
        bindung.klartext,
        "kipm_auftrag_anlegen",
        titel="Auswertung",
        text="t",
        faellig="bald",
    )
    assert fehler and not Auftrag.objects.filter(titel="Auswertung").exists()


def test_anlegen_mit_termin_von_hand(owner_client, projekt):
    antwort = owner_client.post(
        reverse("wunsch_neu"),
        {
            "typ": "issue",
            "projekt": projekt.pk,
            "titel": "Von Hand",
            "beschreibung": "b",
            "prioritaet": "mittel",
            "faellig": "2026-12-24",
        },
    )
    assert antwort.status_code == 302
    assert Auftrag.objects.get(titel="Von Hand").faellig == datetime.date(2026, 12, 24)


def test_werkzeuge_nennen_das_feld():
    liste = {w["name"]: w["inputSchema"]["properties"] for w in mcp.werkzeugliste()}
    assert "faellig" in liste["kipm_planen"] and "faellig" in liste["kipm_auftrag_anlegen"]


def test_kontext_nennt_den_termin(akteur, projekt):
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Auswertung", beschreibung="b", faellig="2026-11-10"
    )
    assert a.faellig is not None
    aktive = akte.kontext_erzeugen(akteur, projekt)
    assert f"{a.nummer} Auswertung" in str(aktive) and "fällig am 10.11.2026" in str(aktive)


# --- „Wartet auf dich“: ab dem Termin zuoberst, Abgeschlossenes nie --------------------------


def test_erst_ab_dem_termin(akteur, projekt):
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Termin", beschreibung="b", faellig=_tag(1))
    assert a not in dienst.wartet_auf_dich(projekt)
    dienst.faellig_setzen(akteur, a, _tag(0))
    assert a in dienst.wartet_auf_dich(projekt)
    dienst.faellig_setzen(akteur, a, _tag(-5))
    assert a in dienst.wartet_auf_dich(projekt)


def test_der_tag_gilt_in_ortszeit(akteur, projekt, monkeypatch):
    """23:30 UTC ist in Wien schon der nächste Tag."""
    jetzt = datetime.datetime(2026, 11, 9, 23, 30, tzinfo=datetime.UTC)
    monkeypatch.setattr(timezone, "now", lambda: jetzt)
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Termin", beschreibung="b", faellig="2026-11-10"
    )
    assert dienst.ist_faellig(a) and a in dienst.wartet_auf_dich(projekt)


@pytest.mark.parametrize(
    "zustand", [Auftrag.ENTWURF, Auftrag.FREIGEGEBEN, Auftrag.IN_ARBEIT, Auftrag.BLOCKIERT]
)
def test_jeder_offene_zustand_erscheint(owner_client, akteur, projekt, zustand):
    a = _entwurf_zu(akteur, projekt, zustand, titel="Fälliger Eintrag")
    assert a not in dienst.wartet_auf_dich(projekt) or zustand == Auftrag.BLOCKIERT
    Auftrag.objects.filter(pk=a.pk).update(faellig=_tag(0))
    wartet = _wartet(owner_client, projekt)
    assert "Fälliger Eintrag" in wartet and "heute fällig" in wartet
    assert wartet.count('<li class="eintrag') == 1


@pytest.mark.parametrize("zustand", [Auftrag.ERLEDIGT, Auftrag.ABGEBROCHEN])
def test_erledigte_und_abgebrochene_nie(owner_client, akteur, projekt, zustand):
    a = _entwurf_zu(akteur, projekt, zustand, titel="Schon fertig")
    Auftrag.objects.filter(pk=a.pk).update(faellig=_tag(-1))
    assert a not in dienst.wartet_auf_dich(projekt)
    assert "Schon fertig" not in _wartet(owner_client, projekt)


def test_nach_erledigen_verschwindet_der_eintrag(akteur, freigegeben, bindung, owner_client):
    dienst.faellig_setzen(akteur, freigegeben, _tag(-1))
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, freigegeben.revision_nr)
    assert freigegeben in dienst.wartet_auf_dich(freigegeben.projekt)
    dienst.ergebnis_einreichen(
        chat,
        freigegeben,
        freigegeben.revision_nr,
        zusammenfassung="fertig",
        dateien="a@1",
        pruefungen="pytest: grün",
    )
    assert freigegeben not in dienst.wartet_auf_dich(freigegeben.projekt)


def test_faelliges_steht_oben_aeltester_termin_zuerst(owner_client, akteur, projekt, bindung):
    blockiert = _blockiert(akteur, projekt, bindung)
    entwurf = arbeitspaket(akteur, projekt, titel="Geplanter Entwurf", umfang="x/**")
    spaeter = dienst.wunsch_erfassen(
        akteur, projekt, titel="Heute fällig", beschreibung="b", faellig=_tag(0)
    )
    frueher = dienst.wunsch_erfassen(
        akteur, projekt, titel="Längst fällig", beschreibung="b", faellig=_tag(-3)
    )
    zukunft = dienst.wunsch_erfassen(
        akteur, projekt, titel="Nächste Woche", beschreibung="b", faellig=_tag(7)
    )
    reihe = dienst.wartet_auf_dich(projekt)
    assert reihe[:3] == [frueher, spaeter, blockiert] and entwurf in reihe[3:]
    assert zukunft not in reihe
    wartet = _wartet(owner_client, projekt)
    assert wartet.index("Längst fällig") < wartet.index("Heute fällig")
    assert wartet.index("Heute fällig") < wartet.index(blockiert.titel)
    assert f"fällig seit {_tag(-3):%d.%m.%Y}" in wartet


def test_blockiert_und_faellig_steht_nur_einmal_da(owner_client, akteur, projekt, bindung):
    a = _blockiert(akteur, projekt, bindung)
    dienst.faellig_setzen(akteur, a, _tag(0))
    assert dienst.wartet_auf_dich(projekt) == [a]
    wartet = _wartet(owner_client, projekt)
    assert wartet.count('<li class="eintrag') == 1 and "Antworten" in wartet


def test_startseite_zaehlt_faelliges_mit(owner_client, akteur, projekt):
    dienst.wunsch_erfassen(akteur, projekt, titel="Fällig", beschreibung="b", faellig=_tag(0))
    html = owner_client.get(reverse("start")).content.decode()
    assert '<span class="wartet">1 wartet auf dich</span>' in html


def test_ungeplant_und_faellig_nennt_was_zu_tun_ist(owner_client, akteur, projekt):
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Idee", beschreibung="b", faellig=_tag(0))
    wartet = _wartet(owner_client, projekt)
    assert f"Plane {a.nummer}" in wartet


def test_freigegeben_und_faellig_sagt_nicht_nichts_zu_tun(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(0))
    wartet = _wartet(owner_client, freigegeben.projekt)
    assert "nichts zu tun" not in wartet and "Der Termin ist da" in wartet
    assert "Freigegeben" in wartet
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
    ).content.decode()
    assert "nichts zu tun" not in seite and "Der Termin ist da" in seite


def test_ohne_faelligkeit_bleibt_nichts_zu_tun(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(7))
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
    ).content.decode()
    assert "nichts zu tun" in seite and "Der Termin ist da" not in seite


def test_board_und_liste_zeigen_den_termin(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(0))
    url = _projekt_url(freigegeben.projekt, "projekt_auftraege")
    assert "heute fällig" in owner_client.get(url).content.decode()
    assert "heute fällig" in owner_client.get(url + "?ansicht=liste").content.decode()


# --- Klickwege: höchstens zwei Klicks ab der Startseite --------------------------------------


def test_klickweg_faelliges_oeffnen(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(0))
    f = _Klicker(owner_client)
    f.link(_projekt_url(freigegeben.projekt))
    f.link(reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk}), freigegeben.titel)
    assert freigegeben.titel in f.html and f.klicks == 2


def test_klickweg_termin_entfernen(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(0))
    f = _Klicker(owner_client)
    f.link(_projekt_url(freigegeben.projekt))
    f.knopf(_aktion(freigegeben, "faellig"), "Termin entfernen")
    freigegeben.refresh_from_db()
    assert freigegeben.faellig is None and f.klicks == 2
    assert freigegeben.titel not in f.html.split('id="h-wartet"')[1].split("</section>")[0]


def test_klickweg_termin_verschieben(owner_client, akteur, freigegeben):
    dienst.faellig_setzen(akteur, freigegeben, _tag(0))
    f = _Klicker(owner_client)
    f.link(_projekt_url(freigegeben.projekt))
    f.knopf(_aktion(freigegeben, "faellig"), "Termin speichern", faellig=_tag(7).isoformat())
    freigegeben.refresh_from_db()
    assert freigegeben.faellig == _tag(7) and f.klicks == 2


def test_klickweg_faelligen_entwurf_freigeben(owner_client, akteur, projekt):
    entwurf = arbeitspaket(akteur, projekt)
    dienst.faellig_setzen(akteur, entwurf, _tag(0))
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    assert "Termin speichern" in [k for form in _Seite(f.html).formulare for k in form["knoepfe"]]
    f.knopf(_aktion(entwurf), "Freigeben")
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.FREIGEGEBEN and f.klicks == 2


# --- Migration -------------------------------------------------------------------------------


def test_migration_0012_ist_nur_additiv():
    modul = importlib.import_module("steuerung.migrations.0012_auftrag_faellig")
    assert [type(o).__name__ for o in modul.Migration.operations] == ["AddField"]
    feld = modul.Migration.operations[0].field
    assert feld.null and feld.blank

"""Verschieben von Aufträgen und Issues: dieselben Regeln in Dienst, HTTP, MCP und Oberfläche."""

import json
import threading

import pytest
from django.db import connection
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Abhaengigkeit, Auftrag, ChatBindung, Ereignis, Meldung, Projekt
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


def zeile(auftrag, projekt, **mehr) -> dict:
    auftrag.refresh_from_db()
    kennung = projekt if isinstance(projekt, str) else projekt.kennung
    return {
        "auftrag": auftrag.nummer,
        "revision": auftrag.revision_nr,
        "projekt": kennung,
        **mehr,
    }


def verschiebe(akteur, *zeilen, archiv=None) -> dict:
    plan = dienst.verschiebung_pruefen(akteur, list(zeilen), archiv=archiv)
    assert plan["ausfuehrbar"], plan
    return dienst.verschiebung_ausfuehren(
        akteur, list(zeilen), archiv=archiv, pruefcode=plan["pruefcode"]
    )


def bestand() -> dict:
    """Alles, was eine abgewiesene Verschiebung nicht ändern darf."""
    return {
        "auftraege": sorted(
            Auftrag.objects.values_list(
                "pk", "projekt_id", "projekt_nummer", "modul_id", "zustand", "revision_nr"
            )
        ),
        "projekte": sorted(
            Projekt.objects.values_list("pk", "aktiv", "naechste_nummer", "inhalt_revision")
        ),
        "abhaengig": sorted(Abhaengigkeit.objects.values_list("auftrag_id", "vorgaenger_id")),
        "ereignisse": Ereignis.objects.count(),
    }


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


@pytest.fixture
def assistent(pc_zugang):
    return dienst.Akteur.aus_bindung(pc_zugang)


@pytest.fixture
def entwurf(akteur, projekt):
    return arbeitspaket(akteur, projekt, titel="Zu verschieben", umfang="zu/**")


# --- Projektwechsel: Identität, Nummern, Verlauf -----------------------------------------


def test_projektwechsel_behaelt_identitaet_und_vergibt_neue_nummer(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    pk, alte, rev_vorher = a.pk, a.nummer, a.revision_nr
    alter_ort = a.aktuelle_revision.arbeitsort
    ergebnis = verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    assert a.pk == pk
    assert a.projekt == projekt_b
    assert a.nummer == f"{projekt_b.kuerzel}-1"
    assert a.zustand == Auftrag.ENTWURF
    assert a.modul is None
    assert ergebnis["verschoben"][0]["alt"] == alte
    assert ergebnis["verschoben"][0]["neu"] == a.nummer
    assert ergebnis["verschoben"][0]["id"] == pk
    # Der aktuelle Stand zeigt nicht mehr ins alte Projekt, der frühere bleibt unverändert.
    assert a.aktuelle_revision.arbeitsort is None
    assert a.revision_nr == rev_vorher + 1
    assert a.revisionen.get(nummer=rev_vorher).arbeitsort == alter_ort


def test_alte_nummern_fuehren_zum_selben_eintrag_und_werden_nie_wieder_vergeben(
    akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    assert dienst.auftrag_id_aus_nummer(alte) == a.pk
    assert dienst.auftrag_id_aus_nummer(a.nummer) == a.pk
    assert dienst.auftrag_id_aus_nummer(a.alte_nummer) == a.pk
    neu = dienst.wunsch_erfassen(akteur, projekt, titel="Danach", beschreibung="x")
    assert neu.nummer != alte
    assert dienst.auftrag_id_aus_nummer(neu.nummer) == neu.pk


def test_hin_und_zurueck_bleiben_alle_nummern_gueltig(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    erste = a.nummer
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    zweite = a.nummer
    verschiebe(akteur, zeile(a, projekt))
    a.refresh_from_db()
    assert a.projekt == projekt
    assert a.nummer not in (erste, zweite)
    for nummer in (erste, zweite, a.nummer):
        assert dienst.auftrag_id_aus_nummer(nummer) == a.pk


def test_verlauf_protokolliert_quelle_ziel_akteur_und_nummern(
    assistent, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    vorher = list(
        Ereignis.objects.filter(objekt_kennung=alte).values_list("pk", "objekt_kennung", "ereignis")
    )
    verschiebe(assistent, zeile(a, projekt_b))
    a.refresh_from_db()
    quelle = Ereignis.objects.get(projekt=projekt, objekt_kennung=alte, ereignis__contains="nach")
    ziel = Ereignis.objects.get(projekt=projekt_b, objekt_kennung=a.nummer)
    for e in (quelle, ziel):
        assert alte in e.ereignis + e.anlass
        assert a.nummer in e.ereignis + e.anlass
        assert e.akteur_name == assistent.name
        assert e.zeitpunkt is not None
    # Frühere Einträge werden nicht umgeschrieben.
    for pk, kennung, text in vorher:
        e = Ereignis.objects.get(pk=pk)
        assert (e.objekt_kennung, e.ereignis) == (kennung, text)


def test_detail_zeigt_verlauf_aus_beiden_projekten_und_fruehere_nummer(
    owner_client, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    verschiebe(akteur, zeile(a, projekt_b))
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}))
    assert seite.status_code == 200
    text = seite.content.decode()
    assert alte in text
    assert "angelegt" in text  # Ereignis von vor dem Umzug, im Quellprojekt
    assert "verschoben" in text.lower()


# --- Zuordnungen ----------------------------------------------------------------------------


def test_modul_untermodul_und_ohne_modul_im_selben_projekt(akteur, projekt):
    kern = projekt.module.get(kennung="kern")
    unter = dienst.modul_anlegen(akteur, projekt, kennung="unter", name="Unter", eltern=kern)
    a = arbeitspaket(akteur, projekt)
    nummer, rev = a.nummer, a.revision_nr
    verschiebe(akteur, zeile(a, projekt, modul="unter"))
    a.refresh_from_db()
    assert a.modul == unter
    assert (a.nummer, a.revision_nr) == (nummer, rev)
    verschiebe(akteur, zeile(a, projekt, modul=""))
    a.refresh_from_db()
    assert a.modul is None
    assert (a.nummer, a.revision_nr) == (nummer, rev)
    assert a.aktuelle_revision.arbeitsort is not None


def test_nichts_wird_nach_gleichem_namen_erraten(akteur, projekt, projekt_b):
    dienst.modul_anlegen(akteur, projekt_b, kennung="kern", name="Kern")
    ms_a = akte.meilenstein_anlegen(akteur, projekt, name="Eins")
    akte.meilenstein_anlegen(akteur, projekt_b, name="Eins")
    a = arbeitspaket(akteur, projekt)
    dienst.meilenstein_zuordnen(akteur, a, ms_a)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b)])
    text = " ".join(plan["eintraege"][0]["aenderungen"])
    assert "Meilenstein" in text and "aufgehoben" in text
    assert "Arbeitsort" in text and "aufgehoben" in text
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    assert a.modul is None
    assert a.meilenstein is None
    assert a.aktuelle_revision.arbeitsort is None


def test_zielzuordnung_ausdruecklich_und_unbestaetigt_bleibt_unbestaetigt(
    akteur, assistent, projekt, projekt_b
):
    modul = dienst.modul_anlegen(akteur, projekt_b, kennung="m", name="M")
    ms = akte.meilenstein_anlegen(akteur, projekt_b, name="Ziel")
    vorschlag = dienst.arbeitsort_anlegen(
        dienst.Akteur.aus_bindung(ChatBindung.objects.get(pk=assistent.bindung.pk), projekt_b),
        projekt_b,
        kennung="vorschlag",
        name="Vorschlag",
    )
    assert not vorschlag.bestaetigt
    a = arbeitspaket(akteur, projekt)
    z = zeile(a, projekt_b, modul="m", arbeitsort="vorschlag", meilenstein=ms.nummer)
    plan = dienst.verschiebung_pruefen(akteur, [z])
    assert "Vorschlag" in " ".join(plan["eintraege"][0]["aenderungen"])
    verschiebe(akteur, z)
    a.refresh_from_db()
    vorschlag.refresh_from_db()
    assert (a.modul, a.meilenstein) == (modul, ms)
    assert a.aktuelle_revision.arbeitsort == vorschlag
    assert not vorschlag.bestaetigt
    assert a.zustand == Auftrag.ENTWURF


@pytest.mark.parametrize(
    ("feld", "wert"),
    [("modul", "gibt-es-nicht"), ("arbeitsort", "gibt-es-nicht"), ("meilenstein", "M-999999")],
)
def test_unbekannte_zielzuordnung_ist_konflikt(akteur, projekt, projekt_b, feld, wert):
    a = arbeitspaket(akteur, projekt)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b, **{feld: wert})])
    assert not plan["ausfuehrbar"]
    assert plan["eintraege"][0]["konflikte"]


def test_unbekanntes_modul_im_selben_projekt_ist_konflikt_nicht_stillstand(akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt, modul="gibt-es-nicht")])
    assert not plan["ausfuehrbar"]
    assert "gibt-es-nicht" in " ".join(plan["eintraege"][0]["konflikte"])


def test_fremdes_modul_und_fremder_ort_sind_konflikt(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    # „kern“ gibt es nur in A, „repo“ wird im Ziel nach Kennung des Ziels gesucht.
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b, modul="kern")])
    assert not plan["ausfuehrbar"]
    ort = projekt.arbeitsorte.first()
    dienst.arbeitsort_aendern(
        akteur, projekt_b.arbeitsorte.first(), erwartete_revision=None, aktiv=False
    )
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b, arbeitsort=ort.kennung)])
    assert not plan["ausfuehrbar"], "archivierter Ort im Ziel"


def test_abhaengigkeit_bleibt_wenn_beide_mitgehen(akteur, projekt, projekt_b):
    v = arbeitspaket(akteur, projekt, titel="Vor", umfang="v/**")
    n = arbeitspaket(akteur, projekt, titel="Nach", umfang="n/**")
    dienst.vorgaenger_eintragen(akteur, n, v, "Reihenfolge")
    verschiebe(akteur, zeile(v, projekt_b), zeile(n, projekt_b))
    assert Abhaengigkeit.objects.filter(auftrag=n, vorgaenger=v).exists()


def test_abhaengigkeit_ueber_projekte_wird_sichtbar_aufgehoben(akteur, projekt, projekt_b):
    v = arbeitspaket(akteur, projekt, titel="Vor", umfang="v/**")
    n = arbeitspaket(akteur, projekt, titel="Nach", umfang="n/**")
    dienst.vorgaenger_eintragen(akteur, n, v, "Reihenfolge")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(v, projekt_b)])
    assert n.nummer in " ".join(plan["eintraege"][0]["aenderungen"])
    alte_v = v.nummer
    verschiebe(akteur, zeile(v, projekt_b))
    assert not Abhaengigkeit.objects.filter(auftrag=n, vorgaenger=v).exists()
    # Der zurückgebliebene Eintrag trägt den Vermerk im Verlauf.
    assert Ereignis.objects.filter(
        projekt=projekt, objekt_kennung=n.nummer, ereignis__contains=alte_v
    ).exists()


def test_freigegebener_nachfolger_wird_nicht_versehentlich_frei(akteur, projekt, projekt_b):
    v = arbeitspaket(akteur, projekt, titel="Vor", umfang="v/**")
    n = arbeitspaket(akteur, projekt, titel="Nach", umfang="n/**")
    dienst.auftrag_freigeben(akteur, n)
    dienst.vorgaenger_eintragen(akteur, n, v, "kam später dazu")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(v, projekt_b)])
    assert not plan["ausfuehrbar"]
    assert n.nummer in " ".join(plan["eintraege"][0]["konflikte"])


def test_herkunft_termin_und_texte_bleiben(akteur, projekt, projekt_b):
    alt = dienst.wunsch_erfassen(akteur, projekt, titel="Herkunft", beschreibung="x")
    a = arbeitspaket(akteur, projekt)
    Auftrag.objects.filter(pk=a.pk).update(abgeleitet_von=alt)
    dienst.faellig_setzen(akteur, a, "2030-01-02")
    a.refresh_from_db()
    rev = a.aktuelle_revision
    texte = (rev.beschreibung, rev.ziel, rev.nicht_ziele, rev.schreibumfang, rev.quellen)
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    neu = a.aktuelle_revision
    assert a.abgeleitet_von == alt
    assert a.faellig.isoformat() == "2030-01-02"
    assert a.prioritaet == "hoch"
    assert (neu.beschreibung, neu.ziel, neu.nicht_ziele, neu.schreibumfang, neu.quellen) == texte
    assert neu.abnahmekriterien == rev.abnahmekriterien


# --- Zustände -------------------------------------------------------------------------------


def test_freigegeben_projektwechsel_hebt_freigabe_sichtbar_auf(akteur, freigegeben, projekt_b):
    plan = dienst.verschiebung_pruefen(akteur, [zeile(freigegeben, projekt_b, arbeitsort="repo")])
    eintrag = plan["eintraege"][0]
    assert plan["ausfuehrbar"]
    assert eintrag["zustand_danach"] == Auftrag.ENTWURF
    assert "Freigabe" in " ".join(eintrag["aenderungen"])
    verschiebe(akteur, zeile(freigegeben, projekt_b, arbeitsort="repo"))
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ENTWURF
    assert freigegeben.freigegeben_am is None
    assert not dienst.weiter_reihenfolge(projekt_b)


def test_freigegeben_modulwechsel_im_projekt_behaelt_freigabe(akteur, freigegeben, projekt):
    dienst.modul_anlegen(akteur, projekt, kennung="anderes", name="Anderes")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(freigegeben, projekt, modul="anderes")])
    assert plan["eintraege"][0]["zustand_danach"] == Auftrag.FREIGEGEBEN
    verschiebe(akteur, zeile(freigegeben, projekt, modul="anderes"))
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.FREIGEGEBEN
    assert freigegeben.freigegeben_am is not None


def test_freigegeben_mit_inzwischen_ungueltiger_planung_verliert_freigabe(
    akteur, freigegeben, projekt
):
    dienst.modul_anlegen(akteur, projekt, kennung="anderes", name="Anderes")
    dienst.arbeitsort_aendern(
        akteur, projekt.arbeitsorte.first(), erwartete_revision=None, aktiv=False
    )
    plan = dienst.verschiebung_pruefen(akteur, [zeile(freigegeben, projekt, modul="anderes")])
    assert plan["eintraege"][0]["zustand_danach"] == Auftrag.ENTWURF
    verschiebe(akteur, zeile(freigegeben, projekt, modul="anderes"))
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ENTWURF


def _in_arbeit(akteur, projekt, chat):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    a.refresh_from_db()
    return a


def test_in_arbeit_wird_nicht_verschoben_und_nennt_den_naechsten_schritt(
    akteur, assistent, projekt, projekt_b
):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    a = _in_arbeit(akteur, projekt, chat)
    for ziel in (zeile(a, projekt_b), zeile(a, projekt, modul="")):
        plan = dienst.verschiebung_pruefen(akteur, [ziel])
        assert not plan["ausfuehrbar"]
        text = " ".join(plan["eintraege"][0]["konflikte"])
        assert "In Arbeit" in text and "Bearbeitung ist unklar" in text
    vorher = bestand()
    with pytest.raises(dienst.Konflikt):
        dienst.verschiebung_ausfuehren(akteur, [zeile(a, projekt_b)], pruefcode=plan["pruefcode"])
    assert bestand() == vorher


def test_blockiert_behaelt_frage_und_zustand(akteur, assistent, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    a = _in_arbeit(akteur, projekt, chat)
    dienst.rueckfrage_melden(chat, a, a.revision_nr, "Welche Sprache?")
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    verschiebe(akteur, zeile(a, projekt_b, arbeitsort="repo"))
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    assert a.bearbeiter_bindung == assistent.bindung
    frage = a.meldungen.get(art=Meldung.RUECKFRAGE)
    assert frage.text == "Welche Sprache?"
    assert not frage.historisch
    assert a in dienst.wartet_auf_dich(projekt_b)
    assert a not in dienst.weiter_reihenfolge(projekt_b)


def test_blockiert_braucht_arbeitsort_im_ziel(akteur, assistent, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    a = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, a, a.revision_nr, "Zugang fehlt")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b)])
    assert not plan["ausfuehrbar"]
    assert "Arbeitsort" in " ".join(plan["eintraege"][0]["konflikte"])


def test_blockiert_mit_projektschluessel_als_bearbeiter_nur_innerhalb(
    akteur, bindung, projekt, projekt_b
):
    chat = dienst.Akteur.aus_bindung(bindung)
    a = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, a, a.revision_nr, "Zugang fehlt")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b, arbeitsort="repo")])
    assert not plan["ausfuehrbar"]
    assert "Projektschlüssel" in " ".join(plan["eintraege"][0]["konflikte"])
    verschiebe(akteur, zeile(a, projekt, modul=""))
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT


def _erledigt(akteur, projekt):
    a = _in_arbeit(akteur, projekt, akteur)
    dienst.ergebnis_einreichen(
        akteur,
        a,
        a.revision_nr,
        zusammenfassung="Fertig",
        dateien="a.py@abc",
        pruefungen="pytest: 1 passed",
    )
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT
    return a


def test_erledigt_administrativ_ohne_abschluss_umzuschreiben(akteur, projekt, projekt_b):
    a = _erledigt(akteur, projekt)
    rev, abgenommen, zeit, ort = (
        a.revision_nr,
        a.abgenommene_revision,
        a.aktualisiert_am,
        a.aktuelle_revision.arbeitsort,
    )
    ergebnis = a.ergebnisse.get()
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    assert a.projekt == projekt_b
    assert a.zustand == Auftrag.ERLEDIGT
    assert (a.revision_nr, a.abgenommene_revision, a.aktualisiert_am) == (rev, abgenommen, zeit)
    assert a.ergebnisse.get().pk == ergebnis.pk
    # Der Ort der abgeschlossenen Arbeit bleibt als Tatsache stehen und ist als früher gekennzeichnet.
    assert a.aktuelle_revision.arbeitsort == ort


def test_abgebrochen_administrativ(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_abbrechen(akteur, a, "nicht mehr nötig")
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    assert (a.projekt, a.zustand) == (projekt_b, Auftrag.ABGEBROCHEN)


def test_abgeschlossene_nehmen_keinen_neuen_arbeitsort(akteur, projekt, projekt_b):
    a = _erledigt(akteur, projekt)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b, arbeitsort="repo")])
    assert not plan["ausfuehrbar"]


def test_schon_am_ziel_aendert_nichts_und_sperrt_die_sammlung_nicht(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt, titel="Bleibt", umfang="b/**")
    b = arbeitspaket(akteur, projekt, titel="Geht", umfang="g/**")
    kern = a.modul.kennung
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt, modul=kern), zeile(b, projekt_b)])
    assert plan["ausfuehrbar"]
    assert plan["eintraege"][0]["aendert"] is False
    nur_noop = dienst.verschiebung_pruefen(akteur, [zeile(a, projekt, modul=kern)])
    assert not nur_noop["ausfuehrbar"]


def test_doppelte_auswahl_und_fehlendes_ziel_sind_konflikte(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    assert not dienst.verschiebung_pruefen(akteur, [zeile(a, projekt_b), zeile(a, projekt_b)])[
        "ausfuehrbar"
    ]
    assert not dienst.verschiebung_pruefen(akteur, [zeile(a, "")])["ausfuehrbar"]
    assert not dienst.verschiebung_pruefen(akteur, [zeile(a, "gibt-es-nicht")])["ausfuehrbar"]
    assert not dienst.verschiebung_pruefen(akteur, [])["ausfuehrbar"]


# --- Archivierte Ziele -----------------------------------------------------------------------


@pytest.fixture
def archiv_projekt(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="alt", name="Altes Projekt")
    alt = dienst.wunsch_erfassen(akteur, p, titel="Altauftrag", beschreibung="x")
    dienst.projekt_archivieren(akteur, p)
    p.refresh_from_db()
    p.altauftrag = alt
    return p


def test_ziele_zeigen_archivierte_erkennbar(akteur, projekt, archiv_projekt):
    ziele = {z["kennung"]: z for z in dienst.verschiebeziele(akteur)}
    assert ziele[archiv_projekt.kennung]["archiviert"] is True
    assert ziele[projekt.kennung]["archiviert"] is False
    assert ziele[projekt.kennung]["module"][0]["pfad"] == "Kern"


def test_archiviertes_ziel_verlangt_ausdrueckliche_wahl(akteur, projekt, archiv_projekt):
    a = arbeitspaket(akteur, projekt)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, archiv_projekt)])
    assert not plan["ausfuehrbar"]
    assert plan["archivziele"] == [
        {"kennung": "alt", "name": "Altes Projekt", "wahl": ""},
    ]


def test_reaktivieren_gibt_keine_arbeit_frei(akteur, projekt, archiv_projekt):
    a = arbeitspaket(akteur, projekt)
    ergebnis = verschiebe(akteur, zeile(a, archiv_projekt), archiv={"alt": "reaktivieren"})
    archiv_projekt.refresh_from_db()
    a.refresh_from_db()
    assert archiv_projekt.aktiv is True
    assert ergebnis["reaktiviert"] == ["alt"]
    assert a.zustand == Auftrag.ENTWURF
    alt = Auftrag.objects.get(pk=archiv_projekt.altauftrag.pk)
    assert alt.zustand == Auftrag.ENTWURF
    assert not dienst.weiter_reihenfolge(archiv_projekt)
    assert Projekt.objects.filter(name="Altes Projekt").count() == 1
    assert Ereignis.objects.filter(projekt=archiv_projekt, ereignis="Projekt zurückgeholt").exists()


def test_im_archiv_lassen_ist_ausdruecklich_moeglich(akteur, projekt, archiv_projekt):
    a = arbeitspaket(akteur, projekt)
    verschiebe(akteur, zeile(a, archiv_projekt), archiv={"alt": "im_archiv"})
    archiv_projekt.refresh_from_db()
    a.refresh_from_db()
    assert archiv_projekt.aktiv is False
    assert a.projekt == archiv_projekt


def test_unbekannte_archivwahl_ist_konflikt(akteur, projekt, archiv_projekt):
    a = arbeitspaket(akteur, projekt)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, archiv_projekt)], archiv={"alt": "egal"})
    assert not plan["ausfuehrbar"]


# --- Befugnis, Stand, Alles-oder-nichts ------------------------------------------------------


def test_projektschluessel_darf_weder_pruefen_noch_verschieben(akteur, bindung, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    chat = dienst.Akteur.aus_bindung(bindung)
    vorher = bestand()
    for z in (zeile(a, projekt_b), zeile(a, projekt, modul="")):
        with pytest.raises(dienst.KeinZugriff):
            dienst.verschiebung_pruefen(chat, [z])
        with pytest.raises(dienst.KeinZugriff):
            dienst.verschiebung_ausfuehren(chat, [z], pruefcode="x")
    with pytest.raises(dienst.KeinZugriff):
        dienst.verschiebeziele(chat)
    assert bestand() == vorher


def test_normales_konto_und_widerrufener_zugang_abgewiesen(
    akteur, zweiter_mensch, pc_zugang, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    mensch = dienst.Akteur.aus_benutzer(zweiter_mensch)
    with pytest.raises(dienst.KeinZugriff):
        dienst.verschiebung_pruefen(mensch, [zeile(a, projekt_b)])
    dienst.bindung_widerrufen(akteur, pc_zugang, "Test")
    pc_zugang.refresh_from_db()
    with pytest.raises(dienst.KeinZugriff):
        dienst.verschiebung_pruefen(dienst.Akteur.aus_bindung(pc_zugang), [zeile(a, projekt_b)])


def test_veraltete_revision_scheitert(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    z = zeile(a, projekt_b)
    z["revision"] = a.revision_nr - 1
    plan = dienst.verschiebung_pruefen(akteur, [z])
    assert not plan["ausfuehrbar"]
    assert "Revision" in " ".join(plan["eintraege"][0]["konflikte"])
    z.pop("revision")
    assert not dienst.verschiebung_pruefen(akteur, [z])["ausfuehrbar"]


def test_aenderung_nach_der_vorschau_erzwingt_neue_pruefung(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    z = zeile(a, projekt_b)
    plan = dienst.verschiebung_pruefen(akteur, [z])
    ms = akte.meilenstein_anlegen(akteur, projekt, name="Neu")
    dienst.meilenstein_zuordnen(akteur, a, ms)  # ändert keine Revision, aber die Folgen
    vorher = bestand()
    with pytest.raises(dienst.Konflikt, match="neu prüfen"):
        dienst.verschiebung_ausfuehren(akteur, [z], pruefcode=plan["pruefcode"])
    assert bestand() == vorher


def test_ziel_nach_der_vorschau_archiviert_erzwingt_neue_pruefung(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    z = zeile(a, projekt_b)
    plan = dienst.verschiebung_pruefen(akteur, [z])
    dienst.projekt_archivieren(akteur, projekt_b)
    vorher = bestand()
    with pytest.raises(dienst.Konflikt):
        dienst.verschiebung_ausfuehren(akteur, [z], pruefcode=plan["pruefcode"])
    assert bestand() == vorher


def test_ein_problem_sperrt_die_ganze_sammlung(akteur, assistent, projekt, projekt_b):
    gut = arbeitspaket(akteur, projekt, titel="Gut", umfang="g/**")
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    laeuft = _in_arbeit(akteur, projekt, chat)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(gut, projekt_b), zeile(laeuft, projekt_b)])
    assert not plan["ausfuehrbar"]
    vorher = bestand()
    with pytest.raises(dienst.Konflikt):
        dienst.verschiebung_ausfuehren(
            akteur, [zeile(gut, projekt_b), zeile(laeuft, projekt_b)], pruefcode=plan["pruefcode"]
        )
    assert bestand() == vorher


def test_fehler_mittendrin_rollt_alles_zurueck(akteur, projekt, projekt_b, monkeypatch):
    a = arbeitspaket(akteur, projekt, titel="Eins", umfang="e/**")
    b = arbeitspaket(akteur, projekt, titel="Zwei", umfang="z/**")
    zeilen = [zeile(a, projekt_b), zeile(b, projekt_b)]
    plan = dienst.verschiebung_pruefen(akteur, zeilen)
    vorher = bestand()
    echt = dienst._ereignis
    aufrufe = []

    def bricht(*args, **kwargs):
        aufrufe.append(1)
        if len(aufrufe) == 3:
            raise RuntimeError("Absturz mitten in der Sammlung")
        return echt(*args, **kwargs)

    monkeypatch.setattr(dienst, "_ereignis", bricht)
    with pytest.raises(RuntimeError):
        dienst.verschiebung_ausfuehren(akteur, zeilen, pruefcode=plan["pruefcode"])
    monkeypatch.undo()
    assert bestand() == vorher
    a.refresh_from_db()
    assert not a.fruehere_nummern.exists()


@pytest.mark.django_db(transaction=True)
def test_gleichzeitige_sammlungen_vergeben_keine_doppelten_nummern(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt, titel="Eins", umfang="e/**")
    b = arbeitspaket(akteur, projekt, titel="Zwei", umfang="z/**")
    plaene = [
        ([zeile(x, projekt_b)], dienst.verschiebung_pruefen(akteur, [zeile(x, projekt_b)]))
        for x in (a, b)
    ]
    fehler = []

    def lauf(z, plan):
        try:
            dienst.verschiebung_ausfuehren(akteur, z, pruefcode=plan["pruefcode"])
        except Exception as e:  # noqa: BLE001 - der Test wertet jeden Fehler aus
            fehler.append(e)
        finally:
            connection.close()

    faeden = [threading.Thread(target=lauf, args=p) for p in plaene]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert not fehler
    a.refresh_from_db()
    b.refresh_from_db()
    assert {a.projekt_nummer, b.projekt_nummer} == {1, 2}
    projekt_b.refresh_from_db()
    assert projekt_b.naechste_nummer == 3


@pytest.mark.django_db(transaction=True)
def test_gleichzeitig_derselbe_eintrag_genau_einer_gewinnt(akteur, projekt, projekt_b):
    p3 = dienst.projekt_registrieren(akteur, kennung="drei", name="Drei")
    a = arbeitspaket(akteur, projekt)
    versuche = [zeile(a, projekt_b), zeile(a, p3)]
    plaene = [dienst.verschiebung_pruefen(akteur, [z]) for z in versuche]
    gewonnen, verloren = [], []

    def lauf(z, plan):
        try:
            gewonnen.append(
                dienst.verschiebung_ausfuehren(akteur, [z], pruefcode=plan["pruefcode"])
            )
        except dienst.Konflikt as e:
            verloren.append(e)
        finally:
            connection.close()

    faeden = [threading.Thread(target=lauf, args=x) for x in zip(versuche, plaene, strict=True)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert (len(gewonnen), len(verloren)) == (1, 1)
    a.refresh_from_db()
    assert a.fruehere_nummern.count() == 1


# --- Listen, Zähler, Sitzungsstart -----------------------------------------------------------


def test_listen_und_sitzungsstart_folgen_dem_ziel(client, akteur, projekt, projekt_b, pc_zugang):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    verschiebe(akteur, zeile(a, projekt_b, arbeitsort="repo"))
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    assert a not in dienst.offene_eintraege(projekt)
    assert a in dienst.offene_eintraege(projekt_b)

    def start(kennung):
        return client.get(
            reverse("api_sitzungsstart"),
            HTTP_AUTHORIZATION=f"Bearer {pc_zugang.klartext}",
            HTTP_X_KIPM_PROJEKT=kennung,
        ).json()

    assert start(projekt_b.kennung)["naechster"]["nummer"] == a.nummer
    assert all(e["nummer"] != a.nummer for e in start(projekt.kennung)["offen"])


def test_zaehler_und_modulfilter_stimmen(owner_client, akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    dienst.modul_anlegen(akteur, projekt_b, kennung="ziel", name="Zielmodul")
    verschiebe(akteur, zeile(a, projekt_b, modul="ziel"))
    a.refresh_from_db()

    def board(p, **abfrage):
        return owner_client.get(
            reverse("projekt_auftraege", kwargs={"kennung": p.kennung}), abfrage
        ).content.decode()

    assert a.nummer not in board(projekt)
    assert a.nummer in board(projekt_b)
    assert a.nummer in board(projekt_b, modul="ziel")
    assert a.titel in board(projekt_b, ansicht="liste")
    start = owner_client.get(reverse("projekte")).context["projekte"]
    zahlen = {p.kennung: p.offen for p in start}
    assert zahlen == {projekt.kennung: 0, projekt_b.kennung: 1}


# --- HTTP ------------------------------------------------------------------------------------


def _post(client, schluessel, name, daten):
    return client.post(
        reverse(name),
        data=json.dumps(daten),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {schluessel}",
    )


def test_http_ablauf_mit_wiederholungsschutz(client, akteur, pc_zugang, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    b = dienst.wunsch_erfassen(
        akteur, projekt, titel="Ein Issue", beschreibung="x", typ=Auftrag.TYP_ISSUE
    )
    zeilen = [zeile(a, projekt_b), zeile(b, projekt_b)]
    vorher = bestand()
    plan = _post(client, pc_zugang.klartext, "api_verschieben_pruefen", {"eintraege": zeilen})
    assert plan.status_code == 200
    assert plan.json()["ausfuehrbar"] is True
    assert bestand() == vorher
    daten = {
        "eintraege": zeilen,
        "pruefcode": plan.json()["pruefcode"],
        "anfrage_kennung": "umzug-1",
    }
    ohne = _post(client, pc_zugang.klartext, "api_verschieben", daten)
    assert ohne.status_code == 400
    assert "fred_sagte" in ohne.json()["fehler"]
    assert bestand() == vorher
    daten["fred_sagte"] = "Ja, verschieben."
    erste = _post(client, pc_zugang.klartext, "api_verschieben", daten)
    assert erste.status_code == 200, erste.content
    assert len(erste.json()["verschoben"]) == 2
    nach = bestand()
    zweite = _post(client, pc_zugang.klartext, "api_verschieben", daten)
    assert zweite.json()["wiederholt"] is True
    assert bestand() == nach
    assert (
        Ereignis.objects.filter(projekt=projekt_b, anlass__contains="Ja, verschieben.").count() == 2
    )


def test_http_verweigert_projektschluessel_ohne_fremdes_zu_verraten(
    client, akteur, bindung, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    vorher = bestand()
    plan = _post(
        client, bindung.klartext, "api_verschieben_pruefen", {"eintraege": [zeile(a, projekt_b)]}
    )
    assert plan.status_code == 403
    aus = _post(
        client,
        bindung.klartext,
        "api_verschieben",
        {
            "eintraege": [zeile(a, projekt_b)],
            "pruefcode": "x",
            "anfrage_kennung": "k",
            "fred_sagte": "Ja",
        },
    )
    assert aus.status_code == 403
    assert (
        client.get(
            reverse("api_verschiebeziele"), HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}"
        ).status_code
        == 403
    )
    assert bestand() == vorher


def test_http_konflikt_liefert_strukturierte_gruende(client, akteur, pc_zugang, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    z = zeile(a, projekt_b)
    z["revision"] = 0
    antwort = _post(
        client,
        pc_zugang.klartext,
        "api_verschieben",
        {"eintraege": [z], "pruefcode": "x", "anfrage_kennung": "k2", "fred_sagte": "Ja"},
    )
    assert antwort.status_code == 409
    assert antwort.json()["vorschau"]["eintraege"][0]["konflikte"]


def test_alte_nummer_und_projektschluessel_nach_dem_umzug(
    client, akteur, bindung, pc_zugang, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    # Der Assistent findet den Eintrag unter der alten Nummer und sieht beide Nummern.
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_lesen", auftrag=alte, fred_sagte=None
    )
    assert not fehler
    assert daten["auftrag"]["id"] == a.pk
    assert daten["auftrag"]["nummer"] == a.nummer
    assert alte in daten["auftrag"]["fruehere_nummern"]
    # Der Schlüssel des Quellprojekts erreicht ihn nicht mehr; fremd gilt als nicht vorhanden.
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_lesen", auftrag=alte, fred_sagte=None)
    assert fehler
    assert daten["fehler"] == "Nicht gefunden."


# --- MCP -------------------------------------------------------------------------------------


def test_mcp_ablauf_mehrere_ziele_mit_archiv(
    client, akteur, pc_zugang, projekt, projekt_b, archiv_projekt
):
    a = arbeitspaket(akteur, projekt, titel="Eins", umfang="e/**")
    b = arbeitspaket(akteur, projekt, titel="Zwei", umfang="z/**")
    s = pc_zugang.klartext
    fehler, ziele = _werkzeug(client, s, "kipm_verschiebeziele", fred_sagte=None)
    assert not fehler
    assert {"alt", projekt_b.kennung} <= {z["kennung"] for z in ziele["ziele"]}
    zeilen = [zeile(a, projekt_b), zeile(b, archiv_projekt)]
    fehler, plan = _werkzeug(
        client, s, "kipm_verschieben_pruefen", eintraege=zeilen, fred_sagte=None
    )
    assert not fehler
    assert plan["ausfuehrbar"] is False
    assert plan["archivziele"][0]["kennung"] == "alt"
    archiv = {"alt": "reaktivieren"}
    fehler, plan = _werkzeug(
        client, s, "kipm_verschieben_pruefen", eintraege=zeilen, archiv=archiv, fred_sagte=None
    )
    assert plan["ausfuehrbar"] is True
    fehler, daten = _werkzeug(
        client,
        s,
        "kipm_verschieben",
        eintraege=zeilen,
        archiv=archiv,
        pruefcode=plan["pruefcode"],
        fred_sagte=None,
    )
    assert fehler
    assert "fred_sagte" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        s,
        "kipm_verschieben",
        eintraege=zeilen,
        archiv=archiv,
        pruefcode=plan["pruefcode"],
        anfrage_kennung="mcp-umzug",
    )
    assert not fehler, daten
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.projekt, b.projekt) == (projekt_b, archiv_projekt)
    assert daten["reaktiviert"] == ["alt"]
    fehler, wieder = _werkzeug(
        client,
        s,
        "kipm_verschieben",
        eintraege=zeilen,
        archiv=archiv,
        pruefcode=plan["pruefcode"],
        anfrage_kennung="mcp-umzug",
    )
    assert wieder["wiederholt"] is True


def test_mcp_projektschluessel_abgewiesen(client, akteur, bindung, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_verschieben_pruefen",
        eintraege=[zeile(a, projekt_b)],
        fred_sagte=None,
    )
    assert fehler
    assert "Owner" in daten["fehler"]


def test_15_2_2_mit_synthetischem_bestand(client, akteur, pc_zugang):
    """Bereinigungsfall im Kleinen: 19 Entwürfe mit drei Zielprojekten, zwei davon archiviert."""
    quelle = dienst.projekt_registrieren(akteur, kennung="quelle", name="Quelle", kuerzel="QU")
    fin = dienst.projekt_registrieren(akteur, kennung="fin", name="Fin", kuerzel="FIN")
    pro = dienst.projekt_registrieren(akteur, kennung="pro", name="Pro", kuerzel="PRO")
    coc = dienst.projekt_registrieren(akteur, kennung="coc", name="Coc", kuerzel="COC")
    for p in (fin, pro, coc):
        dienst.wunsch_erfassen(akteur, p, titel="Historisches Original", beschreibung="alt")
    dienst.projekt_archivieren(akteur, pro)
    dienst.projekt_archivieren(akteur, coc)
    eintraege = [
        dienst.wunsch_erfassen(akteur, quelle, titel=f"Eintrag {i}", beschreibung="x")
        for i in range(21)
    ]
    ziele = [fin] * 15 + [pro] * 2 + [coc] * 2
    zeilen = [zeile(a, z) for a, z in zip(eintraege, ziele, strict=False)]
    archiv = {"pro": "reaktivieren", "coc": "reaktivieren"}
    gesamt = Auftrag.objects.count()
    s = pc_zugang.klartext
    _, plan = _werkzeug(
        client, s, "kipm_verschieben_pruefen", eintraege=zeilen, archiv=archiv, fred_sagte=None
    )
    assert plan["ausfuehrbar"], plan
    fehler, daten = _werkzeug(
        client,
        s,
        "kipm_verschieben",
        eintraege=zeilen,
        archiv=archiv,
        pruefcode=plan["pruefcode"],
        anfrage_kennung="15-2-2",
    )
    assert not fehler, daten
    assert len(daten["verschoben"]) == 19
    assert Auftrag.objects.count() == gesamt, "keine Ersatzaufträge"
    assert quelle.auftraege.count() == 2, "andere Einträge bleiben"
    assert fin.auftraege.count() == 16
    assert pro.auftraege.count() == 3
    assert coc.auftraege.count() == 3
    assert not Auftrag.objects.exclude(zustand=Auftrag.ENTWURF).exists(), "nichts freigegeben"
    assert fin.auftraege.get(projekt_nummer=1).titel == "Historisches Original"
    for a in eintraege[:19]:
        alte = f"QU-{eintraege.index(a) + 1}"
        assert dienst.auftrag_id_aus_nummer(alte) == a.pk


# --- Oberfläche ------------------------------------------------------------------------------


def _ui_pruefen(c, auftraege, **felder):
    daten = {
        "aktion": "pruefen",
        "auftrag": [a.pk for a in auftraege],
        **{f"revision_{a.pk}": a.revision_nr for a in auftraege},
        **felder,
    }
    return c.post(reverse("verschieben"), daten)


def _felder_aus(seite) -> dict:
    """Die Vorschau gibt ihre Werte zurück; so sendet sie auch der Browser."""
    return {"pruefcode": seite.context["vorschau"]["pruefcode"]}


def test_detail_und_board_bieten_verschieben(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    detail = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})
    ).content.decode()
    assert reverse("verschieben") + f"?auftrag={a.pk}" in detail
    for abfrage in ({}, {"ansicht": "liste"}, {"modul": "kern"}):
        seite = owner_client.get(
            reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung}), abfrage
        ).content.decode()
        assert 'id="verschiebe-auswahl"' in seite
        assert f'name="auftrag" value="{a.pk}"' in seite
        assert 'form="verschiebe-auswahl"' in seite
        assert f"{a.nummer} auswählen" in seite


def test_detail_in_arbeit_erklaert_warum_nicht(owner_client, akteur, assistent, projekt):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    a = _in_arbeit(akteur, projekt, chat)
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})
    ).content.decode()
    assert "Verschieben – nicht möglich" in seite
    assert "Bearbeitung ist unklar" in seite


def test_ui_einzeln_vorschau_ausfuehren_ergebnis(owner_client, akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    start = owner_client.get(reverse("verschieben"), {"auftrag": a.pk})
    assert start.status_code == 200
    text = start.content.decode()
    assert alte in text and a.titel in text
    assert "ohne Modul / direkt im Projekt" in text
    assert "<optgroup" in text
    ziel = f"{projekt_b.kennung}|"
    seite = _ui_pruefen(owner_client, [a], ziel=ziel)
    text = seite.content.decode()
    a.refresh_from_db()
    assert a.nummer == alte, "die Vorschau ändert nichts"
    assert "Arbeitsort" in text and "aufgehoben" in text
    assert "Jetzt verschieben" in text
    fertig = owner_client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk],
            f"revision_{a.pk}": a.revision_nr,
            "ziel": ziel,
            **_felder_aus(seite),
        },
        follow=True,
    )
    a.refresh_from_db()
    assert a.projekt == projekt_b
    text = fertig.content.decode()
    assert alte in text and a.nummer in text
    assert reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}) in text


def test_ui_sammlung_mit_mehreren_zielen_und_gemischter_art(
    owner_client, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt, titel="Auftrag", umfang="e/**")
    i = dienst.wunsch_erfassen(
        akteur, projekt, titel="Issue", beschreibung="x", typ=Auftrag.TYP_ISSUE
    )
    gleich = arbeitspaket(akteur, projekt, titel="Bleibt im Projekt", umfang="b/**")
    felder = {
        "ziel": f"{projekt_b.kennung}|",  # gemeinsames Ziel
        f"ziel_{gleich.pk}": f"{projekt.kennung}|",  # eigenes Ziel schlägt das gemeinsame
    }
    seite = _ui_pruefen(owner_client, [a, i, gleich], **felder)
    assert seite.context["vorschau"]["ausfuehrbar"], seite.context["vorschau"]
    owner_client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk, i.pk, gleich.pk],
            **{f"revision_{x.pk}": x.revision_nr for x in (a, i, gleich)},
            **felder,
            **_felder_aus(seite),
        },
    )
    for x in (a, i, gleich):
        x.refresh_from_db()
    assert (a.projekt, i.projekt) == (projekt_b, projekt_b)
    assert i.typ == Auftrag.TYP_ISSUE
    assert gleich.projekt == projekt
    assert gleich.modul is None


def test_ui_geaenderte_auswahl_zeigt_neue_vorschau_statt_auszufuehren(
    owner_client, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    alte = a.nummer
    seite = _ui_pruefen(owner_client, [a], ziel=f"{projekt_b.kennung}|")
    antwort = owner_client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk],
            f"revision_{a.pk}": a.revision_nr,
            "ziel": f"{projekt.kennung}|",  # anders als geprüft
            **_felder_aus(seite),
        },
    )
    assert antwort.status_code == 200
    assert "neu prüfen" in antwort.content.decode() or "neu geprüft" in antwort.content.decode()
    a.refresh_from_db()
    assert a.nummer == alte and a.projekt == projekt


def test_ui_archivziel_bietet_reaktivieren_in_der_vorschau(
    owner_client, akteur, projekt, archiv_projekt
):
    a = arbeitspaket(akteur, projekt)
    start = owner_client.get(reverse("verschieben"), {"auftrag": a.pk}).content.decode()
    assert "Altes Projekt (archiviert)" in start
    seite = _ui_pruefen(owner_client, [a], ziel="alt|")
    text = seite.content.decode()
    assert 'name="archiv_alt"' in text
    assert "reaktivieren" in text
    assert "Jetzt verschieben" not in text or "disabled" in text
    seite = _ui_pruefen(owner_client, [a], ziel="alt|", archiv_alt="reaktivieren")
    assert seite.context["vorschau"]["ausfuehrbar"]
    owner_client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk],
            f"revision_{a.pk}": a.revision_nr,
            "ziel": "alt|",
            "archiv_alt": "reaktivieren",
            **_felder_aus(seite),
        },
    )
    archiv_projekt.refresh_from_db()
    assert archiv_projekt.aktiv


def test_ui_arbeitsort_und_meilenstein_im_ziel_waehlbar(owner_client, akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt)
    ms = akte.meilenstein_anlegen(akteur, projekt_b, name="Ziel-M")
    ort = projekt_b.arbeitsorte.first()
    seite = _ui_pruefen(owner_client, [a], ziel=f"{projekt_b.kennung}|")
    text = seite.content.decode()
    assert f'name="arbeitsort_{a.pk}"' in text and ort.name in text
    assert f'name="meilenstein_{a.pk}"' in text and "Ziel-M" in text
    felder = {
        "ziel": f"{projekt_b.kennung}|",
        f"arbeitsort_{a.pk}": ort.kennung,
        f"meilenstein_{a.pk}": ms.nummer,
    }
    seite = _ui_pruefen(owner_client, [a], **felder)
    owner_client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk],
            f"revision_{a.pk}": a.revision_nr,
            **felder,
            **_felder_aus(seite),
        },
    )
    a.refresh_from_db()
    assert a.meilenstein == ms
    assert a.aktuelle_revision.arbeitsort == ort


def test_ui_normales_konto_bekommt_verstaendliche_abweisung(
    client, zweiter_mensch, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt)
    client.force_login(zweiter_mensch)
    vorher = bestand()
    seite = client.post(
        reverse("verschieben"),
        {
            "aktion": "ausfuehren",
            "auftrag": [a.pk],
            f"revision_{a.pk}": a.revision_nr,
            "ziel": f"{projekt_b.kennung}|",
            "pruefcode": "x",
        },
        follow=True,
    )
    assert "nur der Owner" in seite.content.decode()
    assert bestand() == vorher
    board = client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert 'name="auftrag"' not in board


def test_ui_ohne_auswahl_und_abbrechen(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    leer = owner_client.get(reverse("verschieben"), follow=True).content.decode()
    assert "Nichts ausgewählt" in leer
    zurueck = reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    seite = owner_client.get(reverse("verschieben"), {"auftrag": a.pk, "zurueck": zurueck})
    assert f'href="{zurueck}"' in seite.content.decode()
    assert "Abbrechen" in seite.content.decode()


def test_ui_erledigt_zeigt_frueheren_arbeitsort_gekennzeichnet(
    owner_client, akteur, projekt, projekt_b
):
    a = _erledigt(akteur, projekt)
    verschiebe(akteur, zeile(a, projekt_b))
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})
    ).content.decode()
    assert "früheres Projekt" in seite


# --- Befunde der Gegenprüfung ----------------------------------------------------------------


def test_reaktivieren_weckt_keine_alte_freigabe(akteur, projekt):
    ziel = dienst.projekt_registrieren(akteur, kennung="alt2", name="Alt zwei")
    dienst.arbeitsort_anlegen(akteur, ziel, kennung="ort", name="Ort")
    wartet = arbeitspaket(akteur, ziel, titel="Alte Freigabe", umfang="alt/**")
    dienst.auftrag_freigeben(akteur, wartet)
    dienst.projekt_archivieren(akteur, ziel)
    a = arbeitspaket(akteur, projekt)
    archiv = {"alt2": "reaktivieren"}
    plan = dienst.verschiebung_pruefen(akteur, [zeile(a, ziel)], archiv=archiv)
    assert wartet.nummer in " ".join(plan["reaktivierung_folgen"])
    verschiebe(akteur, zeile(a, ziel), archiv=archiv)
    wartet.refresh_from_db()
    assert wartet.zustand == Auftrag.ENTWURF
    assert not dienst.weiter_reihenfolge(ziel)
    assert (
        Ereignis.objects.filter(
            projekt=ziel, objekt_kennung=wartet.nummer, ereignis__contains="Freigabe"
        )
        .filter(anlass__contains="reaktiviert")
        .exists()
    )


def test_im_archiv_lassen_aendert_alte_freigabe_nicht(akteur, projekt):
    ziel = dienst.projekt_registrieren(akteur, kennung="alt3", name="Alt drei")
    dienst.arbeitsort_anlegen(akteur, ziel, kennung="ort", name="Ort")
    wartet = arbeitspaket(akteur, ziel, titel="Alte Freigabe", umfang="alt/**")
    dienst.auftrag_freigeben(akteur, wartet)
    dienst.projekt_archivieren(akteur, ziel)
    a = arbeitspaket(akteur, projekt)
    verschiebe(akteur, zeile(a, ziel), archiv={"alt3": "im_archiv"})
    wartet.refresh_from_db()
    assert wartet.zustand == Auftrag.FREIGEGEBEN


def test_abgebrochener_vorgaenger_macht_nachfolger_nicht_frei(akteur, projekt, projekt_b):
    v = arbeitspaket(akteur, projekt, titel="Vor", umfang="v/**")
    n = arbeitspaket(akteur, projekt, titel="Nach", umfang="n/**")
    dienst.auftrag_freigeben(akteur, n)
    dienst.vorgaenger_eintragen(akteur, n, v, "Reihenfolge")
    dienst.auftrag_abbrechen(akteur, v, "entfällt")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(v, projekt_b)])
    assert not plan["ausfuehrbar"]
    assert n.nummer in " ".join(plan["eintraege"][0]["konflikte"])


def test_blockiert_mit_ueberschneidung_im_ziel_ist_konflikt(akteur, assistent, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    b = arbeitspaket(akteur, projekt, titel="Blockiert", umfang="src/**")
    dienst.auftrag_freigeben(akteur, b)
    dienst.auftrag_uebernehmen(chat, b, b.revision_nr)
    dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    y = arbeitspaket(akteur, projekt_b, titel="Läuft im Ziel", umfang="src/**")
    dienst.auftrag_freigeben(akteur, y)
    plan = dienst.verschiebung_pruefen(akteur, [zeile(b, projekt_b, arbeitsort="repo")])
    assert not plan["ausfuehrbar"]
    assert y.nummer in " ".join(plan["eintraege"][0]["konflikte"])


def test_blockiert_behaelt_auch_im_selben_projekt_bestaetigten_arbeitsort(
    akteur, assistent, projekt
):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    b = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    plan = dienst.verschiebung_pruefen(akteur, [zeile(b, projekt, arbeitsort="")])
    assert not plan["ausfuehrbar"]
    assert "Arbeitsort" in " ".join(plan["eintraege"][0]["konflikte"])


def test_mitgewaehlter_eintrag_der_bleibt_nennt_die_aufgehobene_abhaengigkeit(
    akteur, projekt, projekt_b
):
    v = arbeitspaket(akteur, projekt, titel="Vor", umfang="v/**")
    n = arbeitspaket(akteur, projekt, titel="Nach", umfang="n/**")
    dienst.vorgaenger_eintragen(akteur, n, v, "Reihenfolge")
    zeilen = [zeile(v, projekt_b), zeile(n, projekt, modul=n.modul.kennung)]
    plan = dienst.verschiebung_pruefen(akteur, zeilen)
    assert v.nummer in " ".join(plan["eintraege"][1]["aenderungen"])
    alte_v = v.nummer
    ergebnis = verschiebe(akteur, *zeilen)
    assert n.nummer not in ergebnis["unveraendert"]
    assert Ereignis.objects.filter(
        projekt=projekt, objekt_kennung=n.nummer, anlass__contains=alte_v
    ).exists()


def test_blockiert_nicht_ins_archiv_ohne_reaktivieren(akteur, assistent, projekt, archiv_projekt):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    b = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    ort = dienst.arbeitsort_anlegen(akteur, archiv_projekt, kennung="ort", name="Ort")
    plan = dienst.verschiebung_pruefen(
        akteur, [zeile(b, archiv_projekt, arbeitsort=ort.kennung)], archiv={"alt": "im_archiv"}
    )
    assert not plan["ausfuehrbar"]
    assert "reaktiv" in " ".join(plan["eintraege"][0]["konflikte"]).lower()


def test_schluessel_des_ziels_sieht_keinen_pfad_des_frueheren_projekts(
    client, akteur, bindung_b, projekt, projekt_b
):
    a = _erledigt(akteur, projekt)
    verschiebe(akteur, zeile(a, projekt_b))
    fehler, daten = _werkzeug(
        client, bindung_b.klartext, "kipm_lesen", auftrag=a.alte_nummer, fred_sagte=None
    )
    assert not fehler
    ort = daten["auftrag"]["arbeitsort"]
    assert ort["frueheres_projekt"] is True
    assert ort["pfad"] == "" and ort["rechner"] == ""


@pytest.mark.parametrize("ziel", ["/\\boese.example", "//boese.example", "https://boese.example"])
def test_zurueck_fuehrt_nie_nach_aussen(owner_client, ziel):
    antwort = owner_client.get(reverse("verschieben"), {"zurueck": ziel})
    assert antwort.status_code == 302
    assert antwort["Location"] == reverse("projekte")


# --- Befunde der zweiten Gegenprüfung ---------------------------------------------------------


def test_zwei_blockierte_in_einer_sammlung_ueberschneiden_sich_im_ziel(
    akteur, assistent, projekt, projekt_b
):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    zweiter_ort = dienst.arbeitsort_anlegen(akteur, projekt, kennung="zwei", name="Zweiter Ort")
    b1 = arbeitspaket(akteur, projekt, titel="Eins", umfang="src/**")
    b2 = arbeitspaket(akteur, projekt, titel="Zwei", umfang="src/**")
    dienst.arbeitsort_setzen(akteur, b2, zweiter_ort)
    for b in (b1, b2):
        b.refresh_from_db()
        dienst.auftrag_freigeben(akteur, b)
        dienst.auftrag_uebernehmen(chat, b, b.revision_nr)
        dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    plan = dienst.verschiebung_pruefen(
        akteur, [zeile(b1, projekt_b, arbeitsort="repo"), zeile(b2, projekt_b, arbeitsort="repo")]
    )
    assert not plan["ausfuehrbar"]
    assert "Überschneidung" in " ".join(plan["konflikte"])


def test_eintrag_der_das_ziel_verlaesst_blockiert_dort_nicht(akteur, assistent, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    b = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    muster = b.aktuelle_revision.schreibumfang
    geht = arbeitspaket(akteur, projekt_b, titel="Geht weg", umfang=muster)
    dienst.auftrag_freigeben(akteur, geht)
    plan = dienst.verschiebung_pruefen(
        akteur, [zeile(b, projekt_b, arbeitsort="repo"), zeile(geht, projekt, arbeitsort="zweit")]
    )
    assert not any("Überschneidung" in k for k in plan["konflikte"])


def test_kontext_nennt_keinen_pfad_des_frueheren_projekts(akteur, projekt, projekt_b):
    ort = projekt.arbeitsorte.first()
    dienst.arbeitsort_aendern(akteur, ort, erwartete_revision=None, pfad="X:/geheim/pfad")
    a = _erledigt(akteur, projekt)
    verschiebe(akteur, zeile(a, projekt_b))
    a.refresh_from_db()
    text = json.dumps(
        akte.kontext_erzeugen(akteur, projekt_b, auftrag=a), ensure_ascii=False, default=str
    )
    assert "X:/geheim/pfad" not in text


@pytest.mark.parametrize("ziel", ["/\t/boese.example", "/\n/boese.example", "/\\boese.example"])
def test_abbrechen_link_fuehrt_nie_nach_aussen(owner_client, akteur, projekt, ziel):
    a = arbeitspaket(akteur, projekt)
    seite = owner_client.get(reverse("verschieben"), {"auftrag": a.pk, "zurueck": ziel})
    assert seite.context["abbrechen"] == reverse("projekte")
    assert seite.context["zurueck"] == ""


def test_mitgewaehlter_freigegebener_im_reaktivierten_ziel_wird_nicht_geweckt(akteur, projekt):
    ziel = dienst.projekt_registrieren(akteur, kennung="alt4", name="Alt vier")
    dienst.modul_anlegen(akteur, ziel, kennung="m", name="M")
    dienst.arbeitsort_anlegen(akteur, ziel, kennung="ort", name="Ort")
    y = arbeitspaket(akteur, ziel, titel="Steht dort", umfang="y/**")
    dienst.auftrag_freigeben(akteur, y)
    dienst.projekt_archivieren(akteur, ziel)
    x = arbeitspaket(akteur, projekt)
    zeilen = [zeile(x, ziel), zeile(y, ziel, modul="m")]
    archiv = {"alt4": "reaktivieren"}
    plan = dienst.verschiebung_pruefen(akteur, zeilen, archiv=archiv)
    zeile_y = next(e for e in plan["eintraege"] if e["id"] == y.pk)
    assert zeile_y["zustand_danach"] == Auftrag.ENTWURF
    assert "Freigabe bleibt" not in " ".join(zeile_y["aenderungen"])
    verschiebe(akteur, *zeilen, archiv=archiv)
    y.refresh_from_db()
    assert y.zustand == Auftrag.ENTWURF
    assert y.freigegeben_am is None
    assert not dienst.weiter_reihenfolge(ziel)


def test_blockiert_neue_revision_gilt_nicht_als_freigegeben(akteur, assistent, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(assistent.bindung, projekt)
    b = _in_arbeit(akteur, projekt, chat)
    dienst.hindernis_melden(chat, b, b.revision_nr, "Zugang fehlt")
    verschiebe(akteur, zeile(b, projekt_b, arbeitsort="repo"))
    b.refresh_from_db()
    assert b.freigegeben_am is None

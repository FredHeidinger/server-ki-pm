"""Dienstregeln für Aufträge: Reifegrad, Revisionen, Freigabe, Überschneidung, Übernahme."""

import threading

import pytest
from django.db import connection

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Meldung
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def test_wunsch_ist_entwurf_und_unvollstaendig(akteur, projekt):
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Schneller starten", beschreibung="Dauert zu lange."
    )
    assert a.zustand == Auftrag.ENTWURF and a.revision_nr == 1
    assert a.aktuelle_revision.ist_wunsch
    assert not dienst.freigabefaehig(a)
    with pytest.raises(dienst.Konflikt, match="Freigabe nicht möglich"):
        dienst.auftrag_freigeben(akteur, a)


def test_chat_legt_wunsch_nur_im_eigenen_projekt_an(bindung, projekt, projekt_b):
    chat = dienst.Akteur.aus_bindung(bindung)
    a = dienst.wunsch_erfassen(chat, projekt, titel="Idee vom Chat", beschreibung="y")
    assert a.zustand == Auftrag.ENTWURF
    assert Ereignis.objects.filter(objekt_kennung=a.nummer, akteur_name="Chat A").exists()
    with pytest.raises(dienst.KeinZugriff):
        dienst.wunsch_erfassen(chat, projekt_b, titel="fremd", beschreibung="y")
    with pytest.raises(dienst.KeinZugriff):
        dienst.auftrag_freigeben(chat, a)


def test_doppelter_titel_wird_erkannt(akteur, projekt):
    dienst.wunsch_erfassen(akteur, projekt, titel="Schneller starten", beschreibung="")
    with pytest.raises(dienst.Konflikt, match=f"{projekt.kuerzel}-1"):
        dienst.wunsch_erfassen(akteur, projekt, titel="  schneller  STARTEN ", beschreibung="")
    zweiter = dienst.wunsch_erfassen(
        akteur, projekt, titel="Schneller starten", beschreibung="", erzwingen=True
    )
    assert zweiter.nummer == f"{projekt.kuerzel}-2"
    assert len(dienst.doppelte_auftraege(projekt, "Schneller starten")) == 2


def test_konkretisieren_erzeugt_revision_und_freigabefaehigkeit(akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    assert a.revision_nr == 2
    assert not a.aktuelle_revision.ist_wunsch
    assert dienst.freigabefaehig(a)
    assert [p.erfuellt for p in dienst.freigabepruefung(a)] == [True] * 7


def test_freigabe_bindet_revision_und_bearbeiten_laesst_sie_erloeschen(
    akteur, projekt, freigegeben
):
    assert freigegeben.freigegeben_am is not None
    dienst.auftrag_bearbeiten(
        akteur,
        freigegeben,
        titel=freigegeben.titel,
        beschreibung="",
        ziel="Neues Ziel",
        nicht_ziele="keine",
        arbeitsort=projekt.arbeitsorte.first(),
        schreibumfang="src/auth/**",
        quellen="",
        abnahmekriterien="Kriterium",
        prioritaet="hoch",
        modul=None,
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ENTWURF and freigegeben.revision_nr == 3
    assert Ereignis.objects.filter(
        objekt_kennung=freigegeben.nummer, anlass__contains="Freigabe erloschen"
    ).exists()


def test_ueberschneidung_sperrt_und_bietet_vorgaenger_an(akteur, projekt, freigegeben):
    zweiter = arbeitspaket(akteur, projekt, titel="Login-Vorlagen", umfang="src/auth/templates/**")
    punkte = dienst.freigabepruefung(zweiter)
    sperre = [p for p in punkte if not p.erfuellt]
    assert len(sperre) == 1 and freigegeben.nummer in sperre[0].text
    assert sperre[0].vorgaenger_angebot == freigegeben
    with pytest.raises(dienst.Konflikt, match="Überschneidung"):
        dienst.auftrag_freigeben(akteur, zweiter)
    dienst.vorgaenger_eintragen(akteur, zweiter, freigegeben, "Überschneidung")
    punkte = dienst.freigabepruefung(zweiter)
    assert not [p for p in punkte if "Überschneidung" in p.text and not p.erfuellt]
    assert any(p.text == "Vorgänger erledigt" and not p.erfuellt for p in punkte)


def test_disjunkter_umfang_ueberschneidet_nicht(akteur, projekt, freigegeben):
    zweiter = arbeitspaket(akteur, projekt, titel="Doku", umfang="docs/**")
    assert dienst.freigabefaehig(zweiter)


@pytest.mark.parametrize(
    ("a", "b", "erwartet"),
    [
        ("src/auth/**", "src/auth/login.py", True),
        ("src/auth/**", "src/authx/**", False),
        ("docs/**", "docs/ZUSTANDSMODELL.md", True),
        ("src/a/**", "src/b/**", False),
        ("*.md", "docs/x.md", True),
        ("src/**/test_*.py", "src/kern/x.py", True),
    ],
)
def test_musterueberschneidung(a, b, erwartet):
    assert dienst.muster_ueberschneiden(a, b) is erwartet


def test_zyklus_wird_verhindert(akteur, projekt):
    a = arbeitspaket(akteur, projekt, titel="A", umfang="a/**")
    b = arbeitspaket(akteur, projekt, titel="B", umfang="b/**")
    c = arbeitspaket(akteur, projekt, titel="C", umfang="c/**")
    dienst.vorgaenger_eintragen(akteur, b, a)
    dienst.vorgaenger_eintragen(akteur, c, b)
    with pytest.raises(dienst.Konflikt, match="Kreis"):
        dienst.vorgaenger_eintragen(akteur, a, c)
    with pytest.raises(dienst.DienstFehler, match="sich selbst"):
        dienst.vorgaenger_eintragen(akteur, a, a)


def test_uebernahme_ist_exklusiv_und_zweiter_versuch_verstaendlich(
    akteur, projekt, freigegeben, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT and freigegeben.bearbeiter_name == "Chat A"
    zweite, _ = dienst.bindung_anlegen(akteur, projekt, name="Chat A2")
    with pytest.raises(dienst.Konflikt, match="bereits von Chat A übernommen"):
        dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(zweite), freigegeben, 2)
    with pytest.raises(dienst.Konflikt):
        dienst.auftrag_uebernehmen(akteur, freigegeben, 2)


def test_veraltete_revision_wird_abgelehnt(akteur, projekt, freigegeben, bindung):
    with pytest.raises(dienst.Konflikt, match="veraltet"):
        dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), freigegeben, 1)


def test_uebernahme_nur_mit_erledigtem_vorgaenger(akteur, projekt, freigegeben, bindung):
    zweiter = arbeitspaket(akteur, projekt, titel="Zweiter", umfang="src/auth/x.py")
    dienst.vorgaenger_eintragen(akteur, zweiter, freigegeben)
    with pytest.raises(dienst.Konflikt, match="Vorgänger"):
        dienst.auftrag_freigeben(akteur, zweiter)


def test_fremdes_projekt_ist_nicht_sichtbar(akteur, projekt, projekt_b, freigegeben, bindung_b):
    fremd = dienst.Akteur.aus_bindung(bindung_b)
    with pytest.raises(dienst.KeinZugriff, match="Nicht gefunden"):
        dienst.auftrag_holen(fremd, freigegeben.pk)
    with pytest.raises(dienst.KeinZugriff):
        dienst.auftrag_uebernehmen(fremd, freigegeben, 2)
    with pytest.raises(dienst.KeinZugriff):
        dienst.fortschritt_melden(fremd, freigegeben, 2, "hallo")
    assert list(dienst.auftraege_fuer_bindung(bindung_b, bindung_b.projekt)) == []


def test_bearbeiten_nach_uebernahme_fuehrt_zurueck_in_entwurf(
    akteur, projekt, freigegeben, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.auftrag_bearbeiten(
        akteur,
        freigegeben,
        titel=freigegeben.titel,
        beschreibung="",
        ziel="Ziel präzisiert",
        nicht_ziele="keine",
        arbeitsort=projekt.arbeitsorte.first(),
        schreibumfang="src/auth/**",
        quellen="",
        abnahmekriterien="K1",
        prioritaet="hoch",
        modul=None,
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ENTWURF and freigegeben.freigegeben_am is None
    assert not freigegeben.hat_bearbeiter and freigegeben.letzter_bearbeiter_name == "Chat A"
    # Meldung des alten Bearbeiters auf alte Revision: historisch abgelegt, Konflikt gemeldet
    with pytest.raises(dienst.Konflikt, match="historisch"):
        dienst.fortschritt_melden(chat, freigegeben, 2, "bin fast fertig")
    assert Meldung.objects.get(text="bin fast fertig").historisch
    # Ergebnis auf alter Revision wird nicht angenommen
    with pytest.raises(dienst.KeinZugriff):
        dienst.ergebnis_einreichen(
            chat, freigegeben, 2, zusammenfassung="x", dateien="a@1", pruefungen="p"
        )
    # Owner gibt neu frei, Chat muss neue Revision ausdrücklich übernehmen
    dienst.auftrag_freigeben(akteur, freigegeben)
    with pytest.raises(dienst.Konflikt, match="veraltet"):
        dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.auftrag_uebernehmen(chat, freigegeben, 3)
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT and freigegeben.uebernommene_revision == 3


def test_bearbeiter_kann_nichts_erweitern(akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    for aufruf in (
        lambda: dienst.auftrag_freigeben(chat, freigegeben),
        lambda: dienst.auftrag_bearbeiten(
            chat,
            freigegeben,
            titel="x",
            beschreibung="",
            ziel="",
            nicht_ziele="",
            arbeitsort=None,
            schreibumfang="**",
            quellen="",
            abnahmekriterien="",
            prioritaet="hoch",
            modul=None,
        ),
        lambda: dienst.vorgaenger_eintragen(chat, freigegeben, freigegeben),
        lambda: dienst.bindung_anlegen(chat, projekt, name="noch ein Chat"),
    ):
        with pytest.raises(dienst.KeinZugriff):
            aufruf()


def test_hindernis_blockiert_und_owner_hebt_auf(akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.hindernis_melden(chat, freigegeben, 2, "Quelldatei fehlt")
    assert freigegeben.zustand == Auftrag.BLOCKIERT
    assert dienst.wartet_auf_dich(projekt) == [freigegeben]
    dienst.rueckfrage_beantworten(akteur, freigegeben, "Datei nachgeliefert")
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT


def test_abbruch_und_unklar(akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.bearbeitung_unklar(akteur, freigegeben, "Chat antwortet seit Tagen nicht")
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ENTWURF and not freigegeben.hat_bearbeiter
    with pytest.raises(dienst.DienstFehler, match="Begründung"):
        dienst.auftrag_abbrechen(akteur, freigegeben, "")
    dienst.auftrag_abbrechen(akteur, freigegeben, "nicht mehr nötig")
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ABGEBROCHEN
    with pytest.raises(dienst.Konflikt):
        dienst.auftrag_bearbeiten(
            akteur,
            freigegeben,
            titel="x",
            beschreibung="",
            ziel="",
            nicht_ziele="",
            arbeitsort=None,
            schreibumfang="",
            quellen="",
            abnahmekriterien="",
            prioritaet="hoch",
            modul=None,
        )


@pytest.mark.django_db(transaction=True)
def test_gleichzeitige_uebernahme_nur_einer_gewinnt(owner):
    """Zwei Threads versuchen dieselbe Übernahme; genau einer bekommt den Auftrag."""
    akteur = dienst.Akteur.aus_benutzer(owner)
    projekt = dienst.projekt_registrieren(akteur, kennung="synth-p", name="Parallel")
    dienst.arbeitsort_anlegen(akteur, projekt, kennung="repo", name="Repo")
    auftrag = arbeitspaket(akteur, projekt, umfang="p/**")
    dienst.auftrag_freigeben(akteur, auftrag)
    b1, _ = dienst.bindung_anlegen(akteur, projekt, name="Chat 1")
    b2, _ = dienst.bindung_anlegen(akteur, projekt, name="Chat 2")
    ergebnisse: list[str] = []
    schranke = threading.Barrier(2)

    def versuch(bindung):
        try:
            schranke.wait(timeout=5)
            dienst.auftrag_uebernehmen(
                dienst.Akteur.aus_bindung(bindung), Auftrag.objects.get(pk=auftrag.pk), 2
            )
            ergebnisse.append("ok")
        except dienst.Konflikt:
            ergebnisse.append("konflikt")
        finally:
            connection.close()

    threads = [threading.Thread(target=versuch, args=(b,)) for b in (b1, b2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert sorted(ergebnisse) == ["konflikt", "ok"]
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.IN_ARBEIT and auftrag.bearbeiter_name in ("Chat 1", "Chat 2")

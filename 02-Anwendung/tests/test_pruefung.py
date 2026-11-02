"""Ergebnis, Prüfung, Abnahme, Nacharbeit, Selbstabnahme."""

import pytest

from steuerung import dienst
from steuerung.models import Auftrag, Kriteriumsbewertung, Pruefung
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db

ERF = Kriteriumsbewertung.ERFUELLT
NICHT = Kriteriumsbewertung.NICHT_ERFUELLT
UNGEPR = Kriteriumsbewertung.NICHT_GEPRUEFT


def _eingereicht(akteur, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    return dienst.ergebnis_einreichen(
        chat,
        freigegeben,
        2,
        zusammenfassung="Hinweistext umgesetzt.",
        dateien="src/auth/login.py@9c2e7b1",
        pruefungen="pytest tests/auth: 12 bestanden",
        luecken="",
    )


def test_ergebnis_braucht_nachweise(akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    with pytest.raises(dienst.DienstFehler, match="Datei"):
        dienst.ergebnis_einreichen(
            chat, freigegeben, 2, zusammenfassung="x", dateien="", pruefungen="p"
        )
    with pytest.raises(dienst.DienstFehler, match="Prüfung"):
        dienst.ergebnis_einreichen(
            chat, freigegeben, 2, zusammenfassung="x", dateien="a@1", pruefungen=""
        )


def test_abnahme_bindet_revision_und_gibt_bearbeiter_frei(akteur, projekt, freigegeben, bindung):
    e = _eingereicht(akteur, freigegeben, bindung)
    assert freigegeben.zustand == Auftrag.ZUR_PRUEFUNG
    assert dienst.kacheln(projekt)["zur_pruefung"] == 1
    p = dienst.ergebnis_pruefen(
        akteur,
        e,
        bewertungen=[(ERF, ""), (UNGEPR, "nur Stichprobe")],
        entscheidung=Pruefung.ABGENOMMEN,
        integration_offen=True,
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ABGENOMMEN
    assert freigegeben.abgenommene_revision == 2 and not freigegeben.hat_bearbeiter
    assert p.bewertungen.count() == 2 and p.integration_offen


def test_abnahme_scheitert_bei_nicht_erfuellt_oder_fehlender_begruendung(
    akteur, projekt, freigegeben, bindung
):
    e = _eingereicht(akteur, freigegeben, bindung)
    with pytest.raises(dienst.Konflikt, match="nicht erfüllt"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, ""), (NICHT, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    with pytest.raises(dienst.DienstFehler, match="Begründung"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, ""), (UNGEPR, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    with pytest.raises(dienst.DienstFehler, match="Jedes Abnahmekriterium"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ZUR_PRUEFUNG


def test_nacharbeit_geht_an_denselben_bearbeiter(akteur, projekt, freigegeben, bindung):
    e = _eingereicht(akteur, freigegeben, bindung)
    with pytest.raises(dienst.DienstFehler, match="Begründung"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, ""), (NICHT, "")], entscheidung=Pruefung.NACHARBEIT
        )
    dienst.ergebnis_pruefen(
        akteur,
        e,
        bewertungen=[(ERF, ""), (NICHT, "Fehlercode noch sichtbar")],
        entscheidung=Pruefung.NACHARBEIT,
        begruendung="Kriterium 2 offen",
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.NACHARBEIT and freigegeben.bearbeiter_name == "Chat A"
    chat = dienst.Akteur.aus_bindung(bindung)
    e2 = dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="Nachgebessert", dateien="a@2", pruefungen="p ok"
    )
    with pytest.raises(dienst.Konflikt, match="zuletzt eingereichte"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, ""), (ERF, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    dienst.ergebnis_pruefen(
        akteur, e2, bewertungen=[(ERF, ""), (ERF, "")], entscheidung=Pruefung.ABGENOMMEN
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ABGENOMMEN


def test_keine_selbstabnahme_durch_menschen(akteur, projekt, zweiter_mensch):
    auftrag = arbeitspaket(akteur, projekt, pruefer=zweiter_mensch)
    dienst.auftrag_freigeben(akteur, auftrag)
    dienst.auftrag_uebernehmen(akteur, auftrag, 2)  # Owner bearbeitet selbst
    e = dienst.ergebnis_einreichen(
        akteur, auftrag, 2, zusammenfassung="fertig", dateien="a@1", pruefungen="p"
    )
    with pytest.raises(dienst.KeinZugriff, match="Selbstabnahme"):
        dienst.ergebnis_pruefen(
            akteur, e, bewertungen=[(ERF, ""), (ERF, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    pruefer = dienst.Akteur.aus_benutzer(zweiter_mensch)
    dienst.ergebnis_pruefen(
        pruefer, e, bewertungen=[(ERF, ""), (ERF, "")], entscheidung=Pruefung.ABGENOMMEN
    )
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ABGENOMMEN


def test_chat_kann_nicht_abnehmen_aber_pruefbericht_liefern(akteur, projekt, freigegeben, bindung):
    e = _eingereicht(akteur, freigegeben, bindung)
    chat = dienst.Akteur.aus_bindung(bindung)
    with pytest.raises(dienst.KeinZugriff, match="nur ein Mensch"):
        dienst.ergebnis_pruefen(
            chat, e, bewertungen=[(ERF, ""), (ERF, "")], entscheidung=Pruefung.ABGENOMMEN
        )
    with pytest.raises(dienst.KeinZugriff, match="eigenen Auftrag"):
        dienst.pruefbericht_einreichen(chat, freigegeben, 2, "sieht gut aus")
    pruefchat, _ = dienst.bindung_anlegen(akteur, projekt, name="Prüfchat")
    m = dienst.pruefbericht_einreichen(
        dienst.Akteur.aus_bindung(pruefchat), freigegeben, 2, "Tests grün"
    )
    assert m.art == "PRUEFBERICHT"
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ZUR_PRUEFUNG


def test_nur_pruefer_oder_owner_prueft(akteur, projekt, freigegeben, bindung, zweiter_mensch):
    e = _eingereicht(akteur, freigegeben, bindung)
    with pytest.raises(dienst.KeinZugriff, match="festgelegte Prüfer"):
        dienst.ergebnis_pruefen(
            dienst.Akteur.aus_benutzer(zweiter_mensch),
            e,
            bewertungen=[(ERF, ""), (ERF, "")],
            entscheidung=Pruefung.ABGENOMMEN,
        )

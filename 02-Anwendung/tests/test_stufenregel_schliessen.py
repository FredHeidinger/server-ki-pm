"""KIPM-34: Die Planung schließt mit Stufe A der Stufenregel einen Entwurf, an dem nichts hängt,
mit Grund und Verweis (aufnehmender Auftrag oder Entscheidung E-n). Der Eintrag wird Abgebrochen,
der Verlauf nennt Stufenregel, Grund und Verweis. Alles andere lehnt KI-PM mit Grund ab und
ändert nichts; dieselbe anfrage_kennung wirkt nur einmal."""

import uuid

import pytest

from steuerung import akte, dienst, lieferungen
from steuerung.models import Abhaengigkeit, Auftrag, Ereignis
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug
from tests.test_stufenregel import _regel

pytestmark = pytest.mark.django_db
GRUND = "In KIPM-Sammelauftrag aufgegangen; nichts Eigenes mehr offen."


@pytest.fixture
def planung(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="ChatGPT Desktop (Codex)")
    b.klartext = schluessel
    return b


@pytest.fixture
def paar(akteur, projekt):
    """Ein überholter Entwurf und der Sammelauftrag, in dem er aufgegangen ist."""
    rest = arbeitspaket(akteur, projekt, titel="Rest aus erledigtem Auftrag", umfang="rest/**")
    sammel = arbeitspaket(akteur, projekt, titel="Sammelauftrag", umfang="sammel/**")
    return rest, sammel


def _schliessen(client, zugang, projekt, a, **werte):
    werte.setdefault("anfrage_kennung", uuid.uuid4().hex)
    werte.setdefault("revision", a.revision_nr)
    werte.setdefault("grund", GRUND)
    return _werkzeug(
        client,
        zugang.klartext,
        "kipm_planung_schliessen",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        fred_sagte=None,
        **werte,
    )


def _unveraendert(a, zustand=Auftrag.ENTWURF):
    vorher = a.revision_nr
    a.refresh_from_db()
    assert a.zustand == zustand and a.revision_nr == vorher
    assert not Ereignis.objects.filter(objekt_kennung=a.nummer, ereignis="Abgebrochen").exists()


# --- Kriterium 1: schließen mit Grund und Verweis ---------------------------------------------


def test_stufe_a_schliesst_einen_entwurf_mit_grund_und_verweis(
    client, owner_client, projekt, planung, paar
):
    rest, sammel = paar
    regel = _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert not fehler, daten
    assert daten["auftrag"]["zustand"] == "ABGEBROCHEN"
    assert daten["regel"] == regel.nummer and daten["verweis"] == sammel.nummer
    rest.refresh_from_db()
    assert rest.zustand == Auftrag.ABGEBROCHEN
    ereignis = Ereignis.objects.get(objekt_kennung=rest.nummer, ereignis="Abgebrochen")
    assert regel.nummer in ereignis.anlass
    assert GRUND in ereignis.anlass and sammel.nummer in ereignis.anlass
    assert "Fred sagte" not in ereignis.anlass
    sammel.refresh_from_db()
    assert sammel.zustand == Auftrag.ENTWURF


def test_issue_mit_entscheidung_als_verweis(client, owner_client, akteur, projekt, planung):
    issue = dienst.wunsch_erfassen(
        akteur, projekt, titel="Hinweis aus Lauf", beschreibung="Rest.", typ=Auftrag.TYP_ISSUE
    )
    e = akte.entscheidung_eintragen(akteur, projekt, entscheidung="Hinweise dieser Art entfallen.")
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, issue, verweis=e.nummer)
    assert not fehler, daten
    issue.refresh_from_db()
    assert issue.zustand == Auftrag.ABGEBROCHEN
    ereignis = Ereignis.objects.get(objekt_kennung=issue.nummer, ereignis="Abgebrochen")
    assert e.nummer in ereignis.anlass


def test_werkzeug_und_anweisung_nennen_den_weg():
    from steuerung import mcp

    namen = {w["name"]: w for w in mcp.werkzeugliste()}
    eintrag = namen["kipm_planung_schliessen"]
    assert "fred_sagte" not in eintrag["inputSchema"]["properties"]
    for feld in ("auftrag", "revision", "grund", "verweis", "anfrage_kennung"):
        assert feld in eintrag["inputSchema"]["required"]
    assert "Entwurf" in eintrag["description"] and "Stufe A" in eintrag["description"]
    assert "kipm_planung_schliessen" in mcp.ANWEISUNGEN


# --- Kriterium 2: Ablehnungen ändern nichts ---------------------------------------------------


def test_ohne_regel_oder_nur_stufe_b_lehnt_ab(client, owner_client, projekt, planung, paar):
    rest, sammel = paar
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Stufenregel" in daten["fehler"]
    _unveraendert(rest)
    _regel(owner_client, projekt, planung, a=False, b=True)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Stufe A" in daten["fehler"]
    _unveraendert(rest)


def test_regel_eines_anderen_zugangs_traegt_nicht(
    client, owner_client, akteur, projekt, planung, paar
):
    rest, sammel = paar
    anderer, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    anderer.klartext = schluessel
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, anderer, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Stufenregel" in daten["fehler"]
    _unveraendert(rest)


@pytest.mark.parametrize("zustand", [Auftrag.FREIGEGEBEN, Auftrag.IN_ARBEIT, Auftrag.BLOCKIERT])
def test_nur_entwuerfe(client, owner_client, akteur, projekt, planung, bindung, paar, zustand):
    rest, sammel = paar
    dienst.auftrag_freigeben(akteur, rest)
    rest.refresh_from_db()
    if zustand != Auftrag.FREIGEGEBEN:
        chat = dienst.Akteur.aus_bindung(bindung)
        dienst.auftrag_uebernehmen(chat, rest, rest.revision_nr)
        if zustand == Auftrag.BLOCKIERT:
            dienst.hindernis_melden(chat, rest, rest.revision_nr, "Fehlt etwas")
        rest.refresh_from_db()
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Entwurf" in daten["fehler"]
    _unveraendert(rest, zustand)


def test_veraltete_revision(client, owner_client, projekt, planung, paar):
    rest, sammel = paar
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(
        client, planung, projekt, rest, verweis=sammel.nummer, revision=rest.revision_nr - 1
    )
    assert fehler and "Revision" in daten["fehler"]
    _unveraendert(rest)


@pytest.mark.parametrize("feld", ["grund", "verweis"])
def test_grund_und_verweis_sind_pflicht(client, owner_client, projekt, planung, paar, feld):
    rest, sammel = paar
    _regel(owner_client, projekt, planung, a=True, b=False)
    werte = {"verweis": sammel.nummer, feld: "   "}
    fehler, daten = _schliessen(client, planung, projekt, rest, **werte)
    assert fehler and feld in daten["fehler"]
    _unveraendert(rest)


@pytest.mark.parametrize("art", ["unbekannt", "fremdes_projekt", "selbst", "offene_frage"])
def test_verweis_muss_im_projekt_vorhanden_sein(
    client, owner_client, akteur, projekt, projekt_b, planung, paar, art
):
    rest, sammel = paar
    if art == "unbekannt":
        verweis = "KIPM-99999"
    elif art == "fremdes_projekt":
        verweis = arbeitspaket(akteur, projekt_b, titel="Fremd", umfang="f/**").nummer
    elif art == "selbst":
        verweis = rest.nummer
    else:
        verweis = akte.entscheidung_fragen(akteur, projekt, frage="Soll das bleiben?").nummer
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=verweis)
    assert fehler and "Verweis" in daten["fehler"]
    _unveraendert(rest)


def test_nachfolger_haengt_daran(client, owner_client, akteur, projekt, planung, paar):
    rest, sammel = paar
    Abhaengigkeit.objects.create(auftrag=sammel, vorgaenger=rest, grund="baut darauf")
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Vorgänger" in daten["fehler"] and sammel.nummer in daten["fehler"]
    _unveraendert(rest)


def test_lieferant_eines_offenen_auftrags(client, owner_client, akteur, projekt, planung, paar):
    rest, sammel = paar
    verbraucher = arbeitspaket(akteur, projekt, titel="Verbraucher", umfang="v/**")
    lieferungen.festlegen(
        akteur,
        verbraucher,
        verbraucher.revision_nr,
        bedarf=[{"auftrag": rest.nummer, "revision": rest.revision_nr, "lieferkennung": "x"}],
        nur_lieferungen=True,
        hindernis_id=None,
    )
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Liefer" in daten["fehler"] and verbraucher.nummer in daten["fehler"]
    _unveraendert(rest)


def test_nur_fuer_menschen(client, owner_client, projekt, planung, paar):
    rest, sammel = paar
    Auftrag.objects.filter(pk=rest.pk).update(nur_mensch=True)
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Menschen" in daten["fehler"]
    _unveraendert(rest)


def test_gebuchtes_ergebnis_oder_lieferung(
    client, owner_client, akteur, projekt, planung, bindung, paar
):
    rest, sammel = paar
    dienst.auftrag_freigeben(akteur, rest)
    rest.refresh_from_db()
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, rest, rest.revision_nr)
    rest.refresh_from_db()
    lieferungen.melden(
        chat,
        rest,
        rest.revision_nr,
        lieferkennung="schema",
        zusammenfassung="Schema geliefert",
        dateien=["a.py@" + "a" * 40],
        pruefungen=["pytest: bestanden"],
    )
    # Neu planen führt in den Entwurf zurück; die gebuchte Lieferung bleibt.
    dienst.bearbeitung_unklar(akteur, rest, "neu schneiden")
    rest.refresh_from_db()
    assert rest.zustand == Auftrag.ENTWURF and rest.lieferungen.exists()
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(client, planung, projekt, rest, verweis=sammel.nummer)
    assert fehler and "Lieferung" in daten["fehler"]
    _unveraendert(rest)


def test_anfrage_kennung_ist_pflicht(client, owner_client, projekt, planung, paar):
    rest, sammel = paar
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _schliessen(
        client, planung, projekt, rest, verweis=sammel.nummer, anfrage_kennung=None
    )
    assert fehler and "anfrage_kennung" in daten["fehler"]
    _unveraendert(rest)


def test_wiederholung_wirkt_nur_einmal(client, owner_client, projekt, planung, paar):
    rest, sammel = paar
    _regel(owner_client, projekt, planung, a=True, b=False)
    kennung = uuid.uuid4().hex
    erst = _schliessen(
        client, planung, projekt, rest, verweis=sammel.nummer, anfrage_kennung=kennung
    )
    noch = _schliessen(
        client, planung, projekt, rest, verweis=sammel.nummer, anfrage_kennung=kennung
    )
    assert not erst[0] and not noch[0]
    # Der Wiederholungsschutz liefert die gespeicherte Antwort mit dem Vermerk „wiederholt“.
    assert noch[1].pop("wiederholt") is True and erst[1] == noch[1]
    assert Ereignis.objects.filter(objekt_kennung=rest.nummer, ereignis="Abgebrochen").count() == 1

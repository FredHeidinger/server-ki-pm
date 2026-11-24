"""KIPM-33: Lücken der Stufenregel an den echten Blockaden vom 24.11.2026, mit synthetischen Daten.

- Fortsetzen nach Freds Entscheidung (kipm_planung_fortsetzen, Stufe A): Eine von Fred
  entschiedene E-n, die den Auftrag nennt, setzt ihn beim bisherigen Bearbeiter fort, ohne eine
  zweite Entscheidung zu buchen; je Blockademeldung höchstens einmal.
- Eine Fehler-Blockade braucht dazu einen Beleg: gebuchte Lieferung oder erledigter
  Korrekturauftrag.
- Der Wortfilter für Stufe C trifft „Testziel“, „Metadaten“ und „bisheriger Bearbeiter“ nicht mehr.
- Nachstellung: NEX-236/237 (Fehler), NEX-68 (Hindernis ohne Kante), NEX-17/6/21/12 (Lieferwarten)."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import akte, dienst, lieferungen, stufenregeln
from steuerung.models import Auftrag, Entscheidung, Ereignis, Meldung, Planungsfortsetzung
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug
from tests.test_stufenregel import QUELLTEXT, _entscheiden, _fundstelle

pytestmark = pytest.mark.django_db


@pytest.fixture
def planung(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="ChatGPT Desktop (Codex)")
    b.klartext = schluessel
    return b


@pytest.fixture
def quelle(akteur, projekt):
    return akte.entscheidung_eintragen(akteur, projekt, entscheidung=QUELLTEXT)


@pytest.fixture
def regel(akteur, projekt, planung):
    return stufenregeln.erteilen(
        akteur,
        projekt,
        zugang=planung,
        stufe_a=True,
        stufe_b=True,
        gueltig_bis=timezone.now() + timedelta(days=7),
    )


@pytest.fixture
def chat(bindung):
    return dienst.Akteur.aus_bindung(bindung)


def _laufend(akteur, projekt, chat, titel):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel.lower().replace(' ', '-')}/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    a.refresh_from_db()
    return a


def _fehler(akteur, projekt, chat, titel):
    a = _laufend(akteur, projekt, chat, titel)
    m = dienst.fehler_melden(chat, a, a.revision_nr, "Strukturprüfung fehlgeschlagen.")
    a.refresh_from_db()
    return a, m


def _korrektur_erledigt(akteur, projekt, chat):
    k = _laufend(akteur, projekt, chat, "Strukturkorrektur")
    dienst.ergebnis_einreichen(
        chat,
        k,
        k.revision_nr,
        zusammenfassung="Korrigiert.",
        dateien="a.py@" + "e" * 40,
        pruefungen="verify: grün",
    )
    k.refresh_from_db()
    assert k.zustand == Auftrag.ERLEDIGT
    return k


def _fred_entscheidet(akteur, projekt, text):
    return akte.entscheidung_eintragen(akteur, projekt, entscheidung=text)


def _fortsetzen(client, zugang, projekt, a, meldung, e, **werte):
    werte.setdefault("anfrage_kennung", uuid.uuid4().hex)
    return _werkzeug(
        client,
        zugang.klartext,
        "kipm_planung_fortsetzen",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
        meldung_id=meldung.pk,
        entscheidung=e.nummer,
        stand=e.stand,
        fred_sagte=None,
        **werte,
    )


# --- Fortsetzen nach Freds Entscheidung ------------------------------------------------------


def test_rueckfrage_wird_nach_freds_entscheidung_fortgesetzt(
    client,
    akteur,
    projekt,
    planung,
    regel,
    chat,
    bindung,
):
    a = _laufend(akteur, projekt, chat, "Export")
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder nicht?")
    a.refresh_from_db()
    e = _fred_entscheidet(akteur, projekt, f"{a.nummer}: ohne BOM.")
    vorher = Entscheidung.objects.count()
    kennung = uuid.uuid4().hex
    fehler, daten = _fortsetzen(client, planung, projekt, a, frage, e, anfrage_kennung=kennung)
    assert not fehler, daten
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT and a.bearbeiter_bindung_id == bindung.pk
    assert Entscheidung.objects.count() == vorher  # keine zweite Entscheidung
    ereignis = Ereignis.objects.get(
        objekt_kennung=a.nummer, ereignis__startswith="Von Planung fortgesetzt"
    )
    assert regel.nummer in ereignis.ereignis + ereignis.anlass and e.nummer in ereignis.anlass
    assert "Fred sagte" not in ereignis.anlass

    fehler, wieder = _fortsetzen(client, planung, projekt, a, frage, e, anfrage_kennung=kennung)
    assert not fehler and wieder.get("wiederholt") is True
    assert Planungsfortsetzung.objects.count() == 1
    assert a.meldungen.filter(art=Meldung.ANTWORT).count() == 1


def test_entscheidung_muss_den_auftrag_nennen(client, akteur, projekt, planung, regel, chat):
    a = _laufend(akteur, projekt, chat, "Export")
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder nicht?")
    a.refresh_from_db()
    e = _fred_entscheidet(akteur, projekt, "Ohne BOM, für alle Exporte.")
    fehler, daten = _fortsetzen(client, planung, projekt, a, frage, e)
    assert fehler and a.nummer in daten["fehler"]
    assert not Planungsfortsetzung.objects.exists()


def test_planungsentscheidung_traegt_kein_fortsetzen(
    client,
    akteur,
    projekt,
    planung,
    regel,
    chat,
    quelle,
):
    a = _laufend(akteur, projekt, chat, "Export")
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder nicht?")
    a.refresh_from_db()
    fehler, daten = _entscheiden(
        client,
        planung,
        projekt,
        fundstelle=_fundstelle(quelle),
        entscheidung=f"{a.nummer} schreibt UTF-8 ohne BOM.",
    )
    assert not fehler, daten
    gebucht = Entscheidung.objects.get(pk=int(daten["entscheidung"].removeprefix("E-")))
    fehler, daten = _fortsetzen(client, planung, projekt, a, frage, gebucht)
    assert fehler and "Planung" in daten["fehler"]


def test_ohne_stufenregel_kein_fortsetzen(client, akteur, projekt, planung, chat):
    a = _laufend(akteur, projekt, chat, "Export")
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder nicht?")
    a.refresh_from_db()
    e = _fred_entscheidet(akteur, projekt, f"{a.nummer}: ohne BOM.")
    fehler, daten = _fortsetzen(client, planung, projekt, a, frage, e)
    assert fehler and "Stufenregel" in daten["fehler"]


# --- Fehler-Blockade nur mit Beleg -----------------------------------------------------------


def test_fehler_ohne_beleg_abgelehnt_mit_erledigter_korrektur_fortgesetzt(
    client,
    akteur,
    projekt,
    planung,
    regel,
    chat,
):
    a, fehlermeldung = _fehler(akteur, projekt, chat, "Strukturgeprüft")
    e = _fred_entscheidet(akteur, projekt, f"{a.nummer} nach der Strukturkorrektur fortsetzen.")
    fehler, daten = _fortsetzen(client, planung, projekt, a, fehlermeldung, e)
    assert fehler and "Beleg" in daten["fehler"]
    offen = _laufend(akteur, projekt, chat, "Noch offen")
    fehler, daten = _fortsetzen(
        client, planung, projekt, a, fehlermeldung, e, nachweis={"auftrag": offen.nummer}
    )
    assert fehler and "erledigt" in daten["fehler"]
    assert not Planungsfortsetzung.objects.exists()

    k = _korrektur_erledigt(akteur, projekt, chat)
    fehler, daten = _fortsetzen(
        client, planung, projekt, a, fehlermeldung, e, nachweis={"auftrag": k.nummer}
    )
    assert not fehler, daten
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT


def test_fehler_mit_gebuchter_lieferung_fortgesetzt(
    client,
    akteur,
    projekt,
    planung,
    regel,
    chat,
):
    a, fehlermeldung = _fehler(akteur, projekt, chat, "Strukturgeprüft")
    anbieter = _laufend(akteur, projekt, chat, "Korrekturlieferung")
    lieferung = lieferungen.melden(
        chat,
        anbieter,
        anbieter.revision_nr,
        lieferkennung="struktur",
        zusammenfassung="Struktur korrigiert",
        dateien=["a.py@" + "f" * 40],
        pruefungen=["grün"],
    )
    e = _fred_entscheidet(akteur, projekt, f"{a.nummer} mit der Korrekturlieferung fortsetzen.")
    fehler, daten = _fortsetzen(
        client, planung, projekt, a, fehlermeldung, e, nachweis={"lieferung": f"L-{lieferung.pk}"}
    )
    assert not fehler, daten


# --- Wortfilter ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Das Testziel ist die Strukturprüfung der Exporte.",
        "Die Metadaten des Exports bleiben UTF-8.",
        "Der bisherige Bearbeiter wiederholt die Prüfung.",
    ],
    ids=["testziel", "metadaten", "bisheriger_bearbeiter"],
)
def test_wortfilter_laesst_harmlose_woerter_durch(
    client,
    akteur,
    projekt,
    planung,
    regel,
    quelle,
    text,
):
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(quelle), entscheidung=text
    )
    assert not fehler, daten


@pytest.mark.parametrize(
    "text",
    [
        "Die Kosten trägt das Projekt.",
        "Die Rechte für das Archiv entfallen.",
        "Wir nutzen Microsoft Graph.",
        "Die Sicherheit prüft niemand mehr.",
        "Die Löschung alter Exporte erfolgt sofort.",
        "Ein anderer Bearbeiter übernimmt.",
    ],
    ids=["kosten", "rechte", "microsoft", "sicherheit", "loeschung", "bearbeiterwechsel"],
)
def test_wortfilter_haelt_stufe_c(client, akteur, projekt, planung, regel, quelle, text):
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(quelle), entscheidung=text
    )
    assert fehler and "Stufe C" in daten["fehler"]


# --- Nachstellung 24.11.2026 -----------------------------------------------------------------


def test_nachstellung_der_sieben_blockaden(client, akteur, projekt, planung, regel, chat):
    # Zwei Fehler-Blockaden mit erfüllten Kriterien (wie NEX-236, NEX-237).
    f1, m1 = _fehler(akteur, projekt, chat, "Wie 236")
    f2, m2 = _fehler(akteur, projekt, chat, "Wie 237")
    k = _korrektur_erledigt(akteur, projekt, chat)
    e = _fred_entscheidet(akteur, projekt, f"{f1.nummer} und {f2.nummer} fortsetzen.")
    for a, m in ((f1, m1), (f2, m2)):
        fehler, daten = _fortsetzen(
            client, planung, projekt, a, m, e, nachweis={"auftrag": k.nummer}
        )
        assert not fehler, daten
        a.refresh_from_db()
        assert a.zustand == Auftrag.IN_ARBEIT

    # Hindernis ohne Lieferkante (wie NEX-68) bekommt über Stufe A die Kante zum Korrekturauftrag.
    h = _laufend(akteur, projekt, chat, "Wie 68")
    hindernis = dienst.hindernis_melden(chat, h, h.revision_nr, "Rückgabe fehlt.")
    h.refresh_from_db()
    korrektur = _laufend(akteur, projekt, chat, "Neue Korrektur")
    fehler, daten = _werkzeug(
        client,
        planung.klartext,
        "kipm_planung_lieferkante",
        projekt=projekt.kennung,
        auftrag=h.nummer,
        revision=h.revision_nr,
        hindernis_id=hindernis.pk,
        nur_lieferungen=True,
        bedarf=[
            {
                "auftrag": korrektur.nummer,
                "revision": korrektur.revision_nr,
                "lieferkennung": "rueckgabe",
            }
        ],
        fred_sagte=None,
    )
    assert not fehler, daten
    h.refresh_from_db()
    assert lieferungen.blockade(h)["art"] == "lieferung"

    # Vier Lieferwarten (wie NEX-17, NEX-6, NEX-21, NEX-12) bleiben, wie sie sind.
    warten = []
    for titel in ("Wie 17", "Wie 6", "Wie 21", "Wie 12"):
        w = _laufend(akteur, projekt, chat, titel)
        blocker = lieferungen.warten(
            chat,
            w,
            w.revision_nr,
            text="Wartet auf Rückgabe.",
            bedarf=[{"auftrag": h.nummer, "revision": h.revision_nr, "lieferkennung": "basis"}],
            nur_lieferungen=True,
        )
        w.refresh_from_db()
        warten.append((w, blocker))
    e2 = _fred_entscheidet(akteur, projekt, " ".join(w.nummer for w, _ in warten) + " fortsetzen.")
    for w, blocker in warten:
        fehler, daten = _fortsetzen(client, planung, projekt, w, blocker, e2)
        assert fehler and "Lieferautomatik" in daten["fehler"]
        w.refresh_from_db()
        assert w.zustand == Auftrag.BLOCKIERT
        assert lieferungen.blockade(w)["art"] == "lieferung"
        assert w not in dienst.wartet_auf_dich(projekt)


def test_migrationskette_nimmt_den_ct100_stand_auf():
    """0030_blockadenabgleich (so auf CT100) liegt vor den eigenen Migrationen; die frühere
    Pflichtspalte klaerungsbelege hat im Modell einen Standardwert (Ablauf in
    04-Pruefung/kipm-stufenregel-luecken/migration_abgleich.py)."""
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    graph = MigrationLoader(connection).graph
    blaetter = graph.leaf_nodes("steuerung")
    assert len(blaetter) == 1
    kette = [n for a, n in graph.forwards_plan(blaetter[0]) if a == "steuerung"]
    assert kette.index("0030_blockadenabgleich") < kette.index("0031_lieferersatz")
    assert Entscheidung._meta.get_field("klaerungsbelege").default is list


# --- Gegenprüfung KIPM-33 ----------------------------------------------------------------------


def test_klaerung_des_alten_blockadenabgleichs_traegt_kein_fortsetzen(
    client, akteur, projekt, planung, regel, chat
):
    a = _laufend(akteur, projekt, chat, "Export")
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder nicht?")
    a.refresh_from_db()
    e = _fred_entscheidet(akteur, projekt, f"{a.nummer}: ohne BOM.")
    Entscheidung.objects.filter(pk=e.pk).update(klaerungsbelege=[{"regel": "KR-1"}])
    fehler, daten = _fortsetzen(client, planung, projekt, a, frage, e)
    assert fehler and "Klärungsregel" in daten["fehler"]
    assert not Planungsfortsetzung.objects.exists()


@pytest.mark.parametrize(
    "text",
    [
        "Das Hauptziel wird auf Offline-Betrieb geändert.",
        "Kontaktdaten und Patientendaten werden exportiert.",
        "Dienst X kostet 40 Euro im Monat.",
        "Der Dienst bekommt Schreibrechte auf das Netzlaufwerk.",
        "Die Ablage zieht nach Teams/M365 und Outlook.",
        "Firewall-Port 3389 öffnen, Verschlüsselung abschalten.",
        "Die alte Tabelle wird entfernt.",
        "Die Löschung der alten Exporte.",
        "Wir nutzen Mi­crosoft Graph.",
        "Wechsel des Bearbeiters zu Codex.",
        "Ein anderer  Bearbeiter übernimmt.",
    ],
    ids=[
        "hauptziel",
        "patientendaten",
        "kostet",
        "schreibrechte",
        "m365",
        "firewall",
        "entfernt",
        "nfd",
        "trennstrich",
        "wechsel_des_bearbeiters",
        "doppeltes_leerzeichen",
    ],
)
def test_wortfilter_faengt_umschreibungen(client, akteur, projekt, planung, regel, quelle, text):
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(quelle), entscheidung=text
    )
    assert fehler and "Stufe C" in daten["fehler"], daten


def test_wortfilter_trifft_zentral_nicht(client, akteur, projekt, planung, regel, quelle):
    fehler, daten = _entscheiden(
        client,
        planung,
        projekt,
        fundstelle=_fundstelle(quelle),
        entscheidung="Der Export liegt zentral im Ordner ausgabe.",
    )
    assert not fehler, daten


@pytest.mark.parametrize(
    "text",
    [
        "Zielsetzung ändern und Teilziel streichen.",
        "Datenbank und Datensätze nach außen geben.",
        "Kostenrahmen erhöhen, 500 EUR.",
        "Rechtevergabe ändern.",
        "Nextcloud einbinden.",
        "MS365 und O365 anbinden, Teamskanal anlegen.",
        "Ports freigeben, Port: 443 öffnen.",
        "Verschluesselung abschalten.",
        "Bearbeiter-Wechsel zu Codex.",
        "Neu-Schnitt des Auftrags.",
        "Der Bearbeiter wird gewechselt.",
    ],
    ids=[
        "zielsetzung",
        "datenbank",
        "kostenrahmen",
        "rechtevergabe",
        "nextcloud",
        "ms365",
        "ports",
        "ue_schreibung",
        "bindestrich_wechsel",
        "neu_schnitt",
        "wird_gewechselt",
    ],
)
def test_wortfilter_faengt_zusammensetzungen(client, akteur, projekt, planung, regel, quelle, text):
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(quelle), entscheidung=text
    )
    assert fehler and "Stufe C" in daten["fehler"], daten

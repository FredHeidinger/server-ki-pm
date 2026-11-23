"""Bestätigungspflichtige Auftragsaktionen: Die Planung schlägt eine konkrete Aktion an einem
Auftrag vor (Wiederaufnahme oder Neuschnitt mit Freigabe), Fred bestätigt sie in der Oberfläche,
KI-PM führt sie über die bestehende Auftragslogik aus. Fallvorlagen NEX-75 (Wiederaufnahme nach
fehlgeschlagener Prüfung, Chat beendet) und NEX-90 (Neuschnitt nach beantworteter Entscheidung),
hier mit synthetischen Daten."""

import json
import uuid

import pytest
from django.urls import reverse

from steuerung import akte, auftragsaktionen, dienst, lieferungen
from steuerung.models import Aktionsvorschlag, Auftrag, Entscheidung, Ereignis, Meldung
from tests.conftest import arbeitspaket
from tests.test_api import api
from tests.test_klickwege import _Klicker, _projekt_url
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db
COMMIT = "b" * 40


# --- Synthetische Fallvorlagen -----------------------------------------------------------------


@pytest.fixture
def planung(akteur):
    """PC-Zugang: Freds KI-Assistent, der plant."""
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Planender Chat")
    b.klartext = schluessel
    return b


@pytest.fixture
def planer(planung, projekt):
    return dienst.Akteur.aus_bindung(planung, projekt=projekt)


@pytest.fixture
def chat(bindung):
    """Der Bearbeiter (Projektschlüssel „Chat A“)."""
    return dienst.Akteur.aus_bindung(bindung)


@pytest.fixture
def nex75(akteur, projekt, chat):
    """Wie NEX-75: Ergebnis mit fehlgeschlagener Strukturprüfung → Blockiert mit FEHLER; danach
    noch ein Fortschritt und das Ende des Chats."""
    a = arbeitspaket(akteur, projekt, titel="Identitätsprofil liefern", umfang="contracts/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.ergebnis_einreichen(
        chat,
        a,
        a.revision_nr,
        zusammenfassung="Profil geprüft, Strukturprüfung rot (fremde Altbefunde).",
        dateien=f"contracts/manifest.json@{COMMIT}",
        pruefungen="verify.py: PASS",
        fehlgeschlagen="verify_structure.py: FAIL, keine Meldung aus eigenen Dateien",
    )
    dienst.fortschritt_melden(chat, a, a.revision_nr, "Folgeauftrag gleicht Prüfregeln an.")
    dienst.sitzungsende_melden(chat, a)
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    return a


@pytest.fixture
def nex90(akteur, projekt, chat, planer):
    """Wie NEX-90: Issue, Hindernis „Schnittfehler“, Frage an Fred als Entscheidung, Fred hat
    geantwortet. Der Auftrag bleibt dabei blockiert."""
    a = arbeitspaket(akteur, projekt, titel="Centumwandlung exakt", umfang="src/shared/money.ts")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.hindernis_melden(
        chat, a, a.revision_nr, "Schnittfehler: Kriterium 3 hängt an evidence/u1a/herkunft.json."
    )
    e = akte.entscheidung_fragen(
        planer, a.projekt, frage="Soll die Planung NEX-90 neu schneiden?", warum="Schnittfehler"
    )
    akte.entscheidung_beantworten(
        akteur, a.projekt, e, erwarteter_stand=0, antwort="nutze die empfehlung"
    )
    a.refresh_from_db()
    a.e = Entscheidung.objects.get(pk=e.pk)
    return a


def _blocker(a: Auftrag) -> Meldung:
    return lieferungen._blocker(a)


def _wieder(planer, a, **extra):
    daten = dict(
        art=Aktionsvorschlag.WIEDERAUFNAHME,
        revision=a.revision_nr,
        meldung_id=_blocker(a).pk,
        text="Strukturprüfung am aktuellen Stand wiederholen.",
    )
    return auftragsaktionen.vorschlagen(planer, a, **(daten | extra))


def _schnitt(planer, a, **extra):
    daten = dict(
        art=Aktionsvorschlag.NEUSCHNITT,
        revision=a.revision_nr,
        meldung_id=_blocker(a).pk if a.zustand == Auftrag.BLOCKIERT else None,
        text="Herkunftsmanifest in den Schreibumfang aufnehmen.",
        entscheidung=getattr(a, "e", None),
        plan={
            "schreibumfang": ["src/shared/money.ts", "evidence/u1a/herkunft.json"],
            "freigeben": True,
        },
    )
    return auftragsaktionen.vorschlagen(planer, a, **(daten | extra))


def _ereignisse(a: Auftrag) -> list[str]:
    return list(
        Ereignis.objects.filter(objekt_kennung=a.nummer)
        .order_by("id")
        .values_list("ereignis", flat=True)
    )


# --- Ausgangsbefund (Altweg bleibt unverändert; diese Tests sind absichtlich grün) -------------


def test_ausgang_nex90_antwort_auf_entscheidung_bewegt_den_auftrag_nicht(nex90):
    """Belegt den Befund: Die beantwortete Entscheidung trägt Text und ENTSCHIEDEN, der Auftrag
    bleibt blockiert; ein Übergang braucht einen getrennten Weg."""
    assert nex90.e.zustand == Entscheidung.ENTSCHIEDEN
    assert "nutze die empfehlung" in nex90.e.text
    assert nex90.zustand == Auftrag.BLOCKIERT


def test_ausgang_nex75_alte_antwort_hebt_auch_eine_neuere_blockade_auf(nex75, chat, planer):
    """Belegt die Lücke des Altwegs: Die Antwort nennt keine Meldung und keine Revision. Kommt
    nach dem Lesen eine neue Rückfrage, hebt die für den Fehler gedachte Antwort auch sie auf."""
    dienst.rueckfrage_melden(chat, nex75, nex75.revision_nr, "Darf ich tools/ ändern?")
    dienst.rueckfrage_beantworten(planer, nex75, "Strukturprüfung wiederholen.")
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.IN_ARBEIT


# --- Vorschlag: gebunden, ohne Wirkung am Auftrag ----------------------------------------------


def test_vorschlag_bindet_auftrag_revision_meldung_und_aendert_nichts(nex75, planer):
    vorher = (nex75.zustand, nex75.bearbeiter_bindung_id, nex75.revision_nr)
    meldungen = nex75.meldungen.count()
    v = _wieder(planer, nex75)
    nex75.refresh_from_db()
    assert (nex75.zustand, nex75.bearbeiter_bindung_id, nex75.revision_nr) == vorher
    assert nex75.meldungen.count() == meldungen
    assert v.zustand == Aktionsvorschlag.OFFEN
    assert (v.auftrag_id, v.erwartete_revision) == (nex75.pk, nex75.revision_nr)
    assert v.meldung.art == Meldung.FEHLER and v.art == Aktionsvorschlag.WIEDERAUFNAHME
    assert v.vorgeschlagen_von_name == "Planender Chat"
    assert _ereignisse(nex75)[-1] == f"Aktion vorgeschlagen: Wiederaufnahme ({v.nummer})"


def test_wiederaufnahme_braucht_die_massgebliche_meldung(nex75, planer, chat):
    fortschritt = nex75.meldungen.filter(art=Meldung.FORTSCHRITT).first()
    with pytest.raises(dienst.Konflikt, match="maßgebliche"):
        _wieder(planer, nex75, meldung_id=fortschritt.pk)
    with pytest.raises(dienst.Konflikt, match="Revision"):
        _wieder(planer, nex75, revision=nex75.revision_nr - 1)
    assert not Aktionsvorschlag.objects.exists()


def test_wiederaufnahme_nur_fuer_blockierte(freigegeben, planer, chat):
    dienst.auftrag_uebernehmen(chat, freigegeben, freigegeben.revision_nr)
    with pytest.raises(dienst.Konflikt, match="blockiert"):
        auftragsaktionen.vorschlagen(
            planer,
            freigegeben,
            art=Aktionsvorschlag.WIEDERAUFNAHME,
            revision=freigegeben.revision_nr,
            meldung_id=None,
            text="weiter",
        )


def test_nur_die_planung_schlaegt_vor(nex75, chat, zweiter_mensch):
    with pytest.raises(dienst.KeinZugriff):
        _wieder(chat, nex75)
    with pytest.raises(dienst.KeinZugriff):
        _wieder(dienst.Akteur.aus_benutzer(zweiter_mensch), nex75)


def test_neuer_vorschlag_ersetzt_den_offenen(nex75, planer):
    alt = _wieder(planer, nex75)
    neu = _wieder(planer, nex75, text="Nur die Prüfung wiederholen.")
    alt.refresh_from_db()
    assert alt.zustand == Aktionsvorschlag.VERALTET and neu.nummer in alt.grund
    assert auftragsaktionen.offener_vorschlag(nex75) == neu


# --- Auswirkungen vor der Bestätigung ----------------------------------------------------------


def test_auswirkungen_der_wiederaufnahme(nex75, planer, chat):
    v = _wieder(planer, nex75)
    w = auftragsaktionen.auswirkungen(v)
    assert w["gueltig"] and w["pruefcode"]
    r = nex75.revision_nr
    assert w["vorher"] == {"zustand": "Blockiert", "bearbeiter": "Chat A", "revision": r}
    assert w["nachher"] == {"zustand": "In Arbeit", "bearbeiter": "Chat A", "revision": r}
    assert w["lieferketten"] == []
    assert any("beendet" in h and nex75.nummer in h for h in w["hinweise"])
    # Die Vorschau ändert nichts.
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT
    assert not nex75.meldungen.filter(art=Meldung.ANTWORT).exists()
    assert auftragsaktionen.auswirkungen(v)["pruefcode"] == w["pruefcode"]


# --- Bestätigen und Ausführen ------------------------------------------------------------------


def test_bestaetigen_fuehrt_ueber_die_auftragslogik_aus(nex75, planer, akteur):
    v = _wieder(planer, nex75)
    r = nex75.revision_nr
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    nex75.refresh_from_db()
    v.refresh_from_db()
    assert nex75.zustand == Auftrag.IN_ARBEIT
    assert nex75.bearbeiter_name == "Chat A" and nex75.revision_nr == r
    antwort = nex75.meldungen.filter(art=Meldung.ANTWORT).get()
    assert v.nummer in antwort.text and "Strukturprüfung" in antwort.text
    assert antwort.akteur_name == akteur.name
    assert v.zustand == Aktionsvorschlag.AUSGEFUEHRT
    assert v.bestaetigt_von_id == akteur.benutzer.pk
    assert v.bestaetigt_am is not None and v.ausgefuehrt_am is not None
    assert v.bestaetigt_am <= v.ausgefuehrt_am and v.ergebnis_revision == r
    folge = _ereignisse(nex75)
    i = folge.index(f"Aktion bestätigt ({v.nummer})")
    assert folge[i + 1] == "Antwort gegeben, Blockade aufgehoben"
    assert folge[i + 2] == f"Aktion ausgeführt ({v.nummer})"
    bestaetigt = Ereignis.objects.get(ereignis=f"Aktion bestätigt ({v.nummer})")
    assert bestaetigt.akteur_id == akteur.benutzer.pk


def test_wiederholte_bestaetigung_wirkt_nur_einmal(nex75, planer, akteur):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    with pytest.raises(dienst.Konflikt, match="ausgeführt"):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    assert nex75.meldungen.filter(art=Meldung.ANTWORT).count() == 1


def test_nur_fred_bestaetigt(nex75, planer, zweiter_mensch):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    for wer in (planer, dienst.Akteur.aus_benutzer(zweiter_mensch)):
        with pytest.raises(dienst.KeinZugriff):
            auftragsaktionen.bestaetigen(wer, v, pruefcode=code)
        with pytest.raises(dienst.KeinZugriff):
            auftragsaktionen.ablehnen(wer, v)
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT


def test_abgelehnt_bleibt_ohne_wirkung(nex75, planer, akteur):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    auftragsaktionen.ablehnen(akteur, v, grund="Erst NEX-77 abwarten.")
    v.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.ABGELEHNT and v.grund == "Erst NEX-77 abwarten."
    with pytest.raises(dienst.Konflikt, match="abgelehnt"):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT
    assert _ereignisse(nex75)[-1] == f"Aktionsvorschlag abgelehnt ({v.nummer})"


# --- Veraltete Vorschläge: Revision, Hindernis, Berechtigung erneut geprüft ----------------------


def _veraltet(akteur, v, code, muster):
    with pytest.raises(dienst.Konflikt, match=muster):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    v.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.VERALTET and v.grund


def test_veraltet_nach_neuer_meldung_des_bearbeiters(nex75, planer, akteur, chat):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    dienst.rueckfrage_melden(chat, nex75, nex75.revision_nr, "Darf ich tools/ ändern?")
    assert not auftragsaktionen.auswirkungen(v)["gueltig"]
    _veraltet(akteur, v, code, "neuere")
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT
    assert not nex75.meldungen.filter(art=Meldung.ANTWORT).exists()


def test_veraltet_nach_neuer_revision(nex75, planer, akteur):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    rev = nex75.aktuelle_revision
    dienst.auftrag_bearbeiten(
        akteur,
        nex75,
        titel=nex75.titel,
        beschreibung=rev.beschreibung,
        ziel="Anderes Ziel",
        nicht_ziele=rev.nicht_ziele,
        arbeitsort=rev.arbeitsort,
        schreibumfang=rev.schreibumfang,
        quellen=rev.quellen,
        abnahmekriterien=rev.abnahmekriterien,
        prioritaet=nex75.prioritaet,
        modul=nex75.modul,
    )
    _veraltet(akteur, v, code, "Revision")


def test_veraltet_wenn_schon_anders_aufgehoben(nex75, planer, akteur):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    dienst.rueckfrage_beantworten(akteur, nex75, "Mach weiter.")
    _veraltet(akteur, v, code, "nicht blockiert")
    assert nex75.meldungen.filter(art=Meldung.ANTWORT).count() == 1


def test_veraltet_wenn_der_bearbeiterzugang_widerrufen_ist(nex75, planer, akteur, bindung):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    dienst.bindung_widerrufen(akteur, bindung, "Rechner gewechselt")
    _veraltet(akteur, v, code, "widerrufen")
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT


# --- Technische Lieferbedarfe -----------------------------------------------------------------


@pytest.fixture
def lieferpaar(akteur, projekt, chat):
    jobs = []
    for name in ("quelle", "verbraucher"):
        a = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, a)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        jobs.append(a)
    q, v = jobs
    lieferungen.warten(
        chat,
        v,
        v.revision_nr,
        text="Kontextanschluss fehlt.",
        bedarf=[{"auftrag": q.nummer, "revision": q.revision_nr, "lieferkennung": "kontext"}],
        nur_lieferungen=True,
    )
    v.refresh_from_db()
    return q, v


def _liefern(chat, q):
    return lieferungen.melden(
        chat,
        q,
        q.revision_nr,
        lieferkennung="kontext",
        zusammenfassung="Kontextanschluss",
        dateien=[f"core.py@{COMMIT}"],
        pruefungen=["Anschluss grün"],
    )


def test_wiederaufnahme_zeigt_und_verdraengt_den_lieferbedarf(lieferpaar, planer, akteur, chat):
    q, v_auftrag = lieferpaar
    v = _wieder(planer, v_auftrag)
    w = auftragsaktionen.auswirkungen(v)
    assert w["lieferketten"] and f"{q.nummer}/kontext" in w["lieferketten"][0]
    auftragsaktionen.bestaetigen(akteur, v, pruefcode=w["pruefcode"])
    v_auftrag.refresh_from_db()
    assert v_auftrag.zustand == Auftrag.IN_ARBEIT
    assert (
        "Technischer Lieferbedarf entfällt"
        in Ereignis.objects.get(
            objekt_kennung=v_auftrag.nummer, ereignis="Antwort gegeben, Blockade aufgehoben"
        ).anlass
    )
    _liefern(chat, q)  # eine spätere Lieferung weckt nichts mehr
    assert v_auftrag.meldungen.filter(art=Meldung.ANTWORT).count() == 1


def test_lieferung_vor_der_bestaetigung_macht_den_vorschlag_veraltet(
    lieferpaar, planer, akteur, chat
):
    q, v_auftrag = lieferpaar
    v = _wieder(planer, v_auftrag)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    _liefern(chat, q)  # die bestehende Automatik setzt fort
    v_auftrag.refresh_from_db()
    assert v_auftrag.zustand == Auftrag.IN_ARBEIT
    _veraltet(akteur, v, code, "nicht blockiert")
    assert v_auftrag.meldungen.filter(art=Meldung.ANTWORT).count() == 1


# --- Neuschnitt mit Freigabe (NEX-90) -----------------------------------------------------------


def test_neuschnitt_auswirkungen_vor_der_bestaetigung(nex90, planer):
    v = _schnitt(planer, nex90)
    assert v.entscheidung_id == nex90.e.pk and v.meldung.art == Meldung.HINDERNIS
    w = auftragsaktionen.auswirkungen(v)
    assert w["gueltig"]
    r = nex90.revision_nr
    assert w["vorher"] == {"zustand": "Blockiert", "bearbeiter": "Chat A", "revision": r}
    assert w["nachher"] == {"zustand": "Freigegeben", "bearbeiter": "niemand", "revision": r + 1}
    assert any("Chat A" in h and "erlischt" in h for h in w["hinweise"])
    nex90.refresh_from_db()
    assert nex90.revision_nr == r and nex90.zustand == Auftrag.BLOCKIERT


def test_neuschnitt_bestaetigt_neue_revision_und_freigabe(nex90, planer, akteur):
    v = _schnitt(planer, nex90)
    r = nex90.revision_nr
    auftragsaktionen.bestaetigen(akteur, v, pruefcode=auftragsaktionen.auswirkungen(v)["pruefcode"])
    nex90.refresh_from_db()
    assert nex90.revision_nr == r + 1 and nex90.zustand == Auftrag.FREIGEGEBEN
    assert "evidence/u1a/herkunft.json" in nex90.aktuelle_revision.muster
    assert nex90.aktuelle_revision.ziel  # unveränderte Felder bleiben
    assert not nex90.hat_bearbeiter and nex90.letzter_bearbeiter_name == "Chat A"
    folge = _ereignisse(nex90)
    i = folge.index(f"Aktion bestätigt ({v.nummer})")
    assert folge[i + 1 : i + 4] == [
        f"Revision {r + 1} angelegt",
        "Freigegeben",
        f"Aktion ausgeführt ({v.nummer})",
    ]
    v.refresh_from_db()
    assert v.ergebnis_revision == r + 1


def test_neuschnitt_ohne_moegliche_freigabe_wird_abgewiesen(nex90, planer, akteur, chat, projekt):
    anderer = arbeitspaket(akteur, projekt, titel="Herkunft pflegen", umfang="evidence/**")
    dienst.auftrag_freigeben(akteur, anderer)
    with pytest.raises(dienst.Konflikt, match="Überschneidung"):
        _schnitt(planer, nex90)
    assert not Aktionsvorschlag.objects.exists()


def test_neuschnitt_wird_unmoeglich_bleibt_offen(nex90, planer, akteur, projekt):
    v = _schnitt(planer, nex90)
    r = nex90.revision_nr
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    anderer = arbeitspaket(akteur, projekt, titel="Herkunft pflegen", umfang="evidence/**")
    dienst.auftrag_freigeben(akteur, anderer)
    w = auftragsaktionen.auswirkungen(v)
    assert not w["gueltig"] and "Überschneidung" in w["grund"]
    with pytest.raises(dienst.Konflikt, match="Überschneidung"):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    v.refresh_from_db()
    nex90.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.OFFEN
    assert nex90.revision_nr == r and nex90.zustand == Auftrag.BLOCKIERT


def test_neuschnitt_nur_mit_planfeldern(nex90, planer):
    with pytest.raises(dienst.DienstFehler, match="Planfeld"):
        _schnitt(planer, nex90, plan={"titel": "Anders", "freigeben": True})
    with pytest.raises(dienst.DienstFehler, match="Planfeld"):
        _schnitt(planer, nex90, plan={"freigeben": True})


def test_konkurrierende_aenderung_der_auswirkungen(akteur, projekt, chat, planer):
    """Ein Verbraucher beginnt nach dem Vorschlag auf den Auftrag zu warten: Der Neuschnitt würde
    nun eine Lieferkette trennen. Die alte Anzeige gilt nicht mehr; nach erneutem Ansehen geht es."""
    anbieter = arbeitspaket(akteur, projekt, titel="Anbieter", umfang="anbieter/**")
    dienst.auftrag_freigeben(akteur, anbieter)
    dienst.auftrag_uebernehmen(chat, anbieter, anbieter.revision_nr)
    v = auftragsaktionen.vorschlagen(
        planer,
        anbieter,
        art=Aktionsvorschlag.NEUSCHNITT,
        revision=anbieter.revision_nr,
        meldung_id=None,
        text="Umfang erweitern.",
        plan={"schreibumfang": ["anbieter/**", "doku/**"], "freigeben": True},
    )
    alt = auftragsaktionen.auswirkungen(v)
    verbraucher = arbeitspaket(akteur, projekt, titel="Verbraucher", umfang="verbraucher/**")
    dienst.auftrag_freigeben(akteur, verbraucher)
    dienst.auftrag_uebernehmen(chat, verbraucher, verbraucher.revision_nr)
    lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Anbieter-Schnittstelle fehlt.",
        bedarf=[
            {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": "api"}
        ],
        nur_lieferungen=True,
    )
    with pytest.raises(dienst.Konflikt, match="geändert"):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=alt["pruefcode"])
    v.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.OFFEN
    neu = auftragsaktionen.auswirkungen(v)
    assert neu["pruefcode"] != alt["pruefcode"]
    assert any(verbraucher.nummer in k for k in neu["lieferketten"])
    auftragsaktionen.bestaetigen(akteur, v, pruefcode=neu["pruefcode"])
    assert Ereignis.objects.filter(
        objekt_kennung=verbraucher.nummer, ereignis="Technischer Lieferbedarf verwaist"
    ).exists()


def test_gleichzeitige_bestaetigung_wirkt_einmal(transactional_db, akteur, projekt, bindung):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    chat = dienst.Akteur.aus_bindung(bindung)
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Planender Chat")
    planer = dienst.Akteur.aus_bindung(pc, projekt=projekt)
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.hindernis_melden(chat, a, a.revision_nr, "Zugang fehlt.")
    a.refresh_from_db()
    v = _wieder(planer, a)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    start = Barrier(2)

    def lauf(_):
        close_old_connections()
        try:
            start.wait(timeout=5)
            auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
            return "ok"
        except dienst.Konflikt:
            return "konflikt"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ergebnisse = sorted(pool.map(lauf, range(2)))
    assert ergebnisse == ["konflikt", "ok"]
    assert a.meldungen.filter(art=Meldung.ANTWORT).count() == 1


def test_werkbank_auftraege_haben_eigene_aktionen(w_ausfuehrung, planer):
    a = w_ausfuehrung.auftrag
    with pytest.raises(dienst.Konflikt, match="Werkbank"):
        auftragsaktionen.vorschlagen(
            planer,
            a,
            art=Aktionsvorschlag.NEUSCHNITT,
            revision=a.revision_nr,
            meldung_id=None,
            text="x",
            plan={"ziel": "y"},
        )


# --- HTTP und MCP: vorschlagen und lesen, aber nie bestätigen -----------------------------------


def test_mcp_vorschlag_ohne_fred_sagte_und_lesbar(client, nex75, planung, projekt):
    fehler, daten = _werkzeug(
        client,
        planung.klartext,
        "kipm_aktion_vorschlagen",
        projekt=projekt.kennung,
        auftrag=nex75.nummer,
        revision=nex75.revision_nr,
        art="wiederaufnahme",
        meldung_id=_blocker(nex75).pk,
        text="Strukturprüfung wiederholen.",
        fred_sagte=None,
    )
    assert not fehler, daten
    v = daten["vorschlag"]
    assert v["zustand"] == "OFFEN" and v["auswirkungen"]["nachher"]["zustand"] == "In Arbeit"
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT
    fehler, gelesen = _werkzeug(
        client, planung.klartext, "kipm_lesen", projekt=projekt.kennung, auftrag=nex75.nummer
    )
    eintrag = gelesen["auftrag"]["aktionsvorschlaege"][0]
    assert eintrag["nummer"] == v["nummer"] and eintrag["meldung_id"] == _blocker(nex75).pk
    assert eintrag["veraltet_grund"] == ""


def test_mcp_neuschnitt_mit_entscheidung(client, nex90, planung, projekt):
    fehler, daten = _werkzeug(
        client,
        planung.klartext,
        "kipm_aktion_vorschlagen",
        projekt=projekt.kennung,
        auftrag=nex90.nummer,
        revision=nex90.revision_nr,
        art="neuschnitt",
        meldung_id=_blocker(nex90).pk,
        entscheidung=nex90.e.nummer,
        schreibumfang=["src/shared/money.ts", "evidence/u1a/herkunft.json"],
        freigeben=True,
        text="Neu schneiden nach E-Antwort.",
        fred_sagte=None,
    )
    assert not fehler, daten
    assert daten["vorschlag"]["entscheidung"] == nex90.e.nummer
    assert daten["vorschlag"]["auswirkungen"]["nachher"]["revision"] == nex90.revision_nr + 1


def test_http_wiederholung_legt_nur_einen_vorschlag_an(nex75, planung, projekt):
    c = api(planung.klartext)
    daten = {
        "revision": nex75.revision_nr,
        "art": "wiederaufnahme",
        "meldung_id": _blocker(nex75).pk,
        "text": "weiter",
        "anfrage_kennung": uuid.uuid4().hex,
    }
    pfad = reverse("api_aktion_vorschlagen", kwargs={"auftrag_id": nex75.pk})
    kopf = {"HTTP_X_KIPM_PROJEKT": projekt.kennung}
    r1 = c.post(pfad, json.dumps(daten), content_type="application/json", **kopf)
    r2 = c.post(pfad, json.dumps(daten), content_type="application/json", **kopf)
    assert r1.status_code == 200, r1.content
    assert r2.json().get("wiederholt") is True
    assert Aktionsvorschlag.objects.count() == 1


def test_projektschluessel_kann_nicht_vorschlagen(nex75, bindung):
    r = api(bindung.klartext).post(
        reverse("api_aktion_vorschlagen", kwargs={"auftrag_id": nex75.pk}),
        json.dumps(
            {
                "revision": nex75.revision_nr,
                "art": "wiederaufnahme",
                "meldung_id": _blocker(nex75).pk,
                "text": "weiter",
                "anfrage_kennung": uuid.uuid4().hex,
            }
        ),
        content_type="application/json",
    )
    assert r.status_code == 403
    assert not Aktionsvorschlag.objects.exists()


def test_bestaetigen_nur_angemeldet_in_der_oberflaeche(nex75, planer, planung):
    v = _wieder(planer, nex75)
    url = reverse("vorschlag_entscheiden", kwargs={"vorschlag_id": v.pk, "wahl": "bestaetigen"})
    r = api(planung.klartext).post(
        url, {"pruefcode": auftragsaktionen.auswirkungen(v)["pruefcode"]}
    )
    assert r.status_code == 302 and "anmelden" in r["Location"]
    v.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.OFFEN


# --- Oberfläche ---------------------------------------------------------------------------------


def test_auftragsseite_zeigt_auswirkungen_und_bestaetigt(owner_client, nex75, planer):
    v = _wieder(planer, nex75)
    html = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": nex75.pk})).content
    html = html.decode()
    assert f"Vorschlag {v.nummer}" in html and "Wiederaufnahme" in html
    assert "Blockiert" in html and "In Arbeit" in html and "Chat A" in html
    assert "Bestätigen und ausführen" in html and "Ablehnen" in html
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    url = reverse("vorschlag_entscheiden", kwargs={"vorschlag_id": v.pk, "wahl": "bestaetigen"})
    r = owner_client.post(url, {"pruefcode": code}, follow=True)
    assert "ausgeführt" in r.content.decode()
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.IN_ARBEIT


def test_oberflaeche_veraltete_anzeige_aendert_nichts(owner_client, nex75, planer, chat):
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    dienst.rueckfrage_melden(chat, nex75, nex75.revision_nr, "Neue Frage")
    url = reverse("vorschlag_entscheiden", kwargs={"vorschlag_id": v.pk, "wahl": "bestaetigen"})
    r = owner_client.post(url, {"pruefcode": code}, follow=True)
    assert "neuere" in r.content.decode()
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.BLOCKIERT


def test_oberflaeche_ablehnen(owner_client, nex75, planer):
    v = _wieder(planer, nex75)
    url = reverse("vorschlag_entscheiden", kwargs={"vorschlag_id": v.pk, "wahl": "ablehnen"})
    owner_client.post(url, {"grund": "Nicht jetzt."})
    v.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.ABGELEHNT


def test_klickweg_vorschlag_bestaetigen(owner_client, nex75, planer, projekt):
    v = _wieder(planer, nex75)
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    assert "Vorschlag zur Bestätigung" in f.html
    url = reverse("vorschlag_entscheiden", kwargs={"vorschlag_id": v.pk, "wahl": "bestaetigen"})
    f.knopf(url, "Bestätigen und ausführen")
    nex75.refresh_from_db()
    assert nex75.zustand == Auftrag.IN_ARBEIT and f.klicks == 2


def test_neuschnitt_eines_laufenden_auftrags_wartet_auf_fred(
    owner_client, akteur, projekt, chat, planer
):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    auftragsaktionen.vorschlagen(
        planer,
        a,
        art=Aktionsvorschlag.NEUSCHNITT,
        revision=a.revision_nr,
        meldung_id=None,
        text="Kriterium ergänzen.",
        plan={"abnahmekriterien": ["Hinweis nennt nächsten Schritt", "Neu"], "freigeben": True},
    )
    assert a in dienst.wartet_auf_dich(projekt)
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    assert "Vorschlag zur Bestätigung" in html and a.nummer in html


def test_zugang_mit_vorschlag_gilt_als_benutzt(nex75, planer, planung, akteur):
    """Der Vorschlag hält fest, wer ihn gemacht hat; der Zugang bleibt darum für den Verlauf."""
    assert not dienst.bindung_benutzt(planung)
    _wieder(planer, nex75)
    assert dienst.bindung_benutzt(planung)
    with pytest.raises(dienst.DienstFehler):
        dienst.bindung_loeschen(akteur, planung)


def test_archiviertes_projekt_zeigt_keinen_moeglichen_knopf(nex75, planer, akteur):
    """Befund der Gegenprüfung: Die Anzeige prüft dieselben Grundbedingungen wie die Bestätigung.
    Nach dem Archivieren steht kein Knopf da, der dann doch scheitert; der Vorschlag bleibt offen."""
    v = _wieder(planer, nex75)
    code = auftragsaktionen.auswirkungen(v)["pruefcode"]
    dienst.projekt_archivieren(akteur, nex75.projekt)
    w = auftragsaktionen.auswirkungen(v)
    assert not w["gueltig"] and "archiviert" in w["grund"]
    with pytest.raises(dienst.Konflikt, match="archiviert"):
        auftragsaktionen.bestaetigen(akteur, v, pruefcode=code)
    v.refresh_from_db()
    nex75.refresh_from_db()
    assert v.zustand == Aktionsvorschlag.OFFEN and nex75.zustand == Auftrag.BLOCKIERT

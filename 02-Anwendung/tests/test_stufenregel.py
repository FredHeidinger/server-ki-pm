"""KIPM-31: Stufenregel für die Planung. Fred speichert je Projekt und PC-Zugang eine befristete,
widerrufbare Regel. Stufe A: fertig geplante Aufträge freigeben und Lieferkanten nachtragen.
Stufe B: technische Entscheidungen mit geprüfter wörtlicher Fundstelle buchen und den davon
abhängigen blockierten Auftrag beim bisherigen Bearbeiter fortsetzen. Stufe C bleibt bei Fred.
Ohne gespeicherte Regel ändert sich nichts."""

import hashlib
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst, lieferungen
from steuerung.models import Auftrag, Entscheidung, Ereignis, Meldung, Planungsentscheid
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db
MASTERSPEC = "7" * 64
QUELLTEXT = "Exporte werden immer als UTF-8 ohne BOM geschrieben, Zeilenende LF."


@pytest.fixture
def planung(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="ChatGPT Desktop (Codex)")
    b.klartext = schluessel
    return b


@pytest.fixture
def quelle(akteur, projekt):
    return akte.entscheidung_eintragen(akteur, projekt, entscheidung=QUELLTEXT)


def _regel(owner_client, projekt, zugang, *, a=True, b=True, masterspec=""):
    daten = {
        "zugang": zugang.pk,
        "gueltig_bis": (timezone.localdate() + timedelta(days=30)).isoformat(),
        "masterspec_bezeichnung": "BASELINE-005" if masterspec else "",
        "masterspec_sha256": masterspec,
    }
    if a:
        daten["stufe_a"] = "on"
    if b:
        daten["stufe_b"] = "on"
    url = reverse("stufenregel_erteilen", kwargs={"kennung": projekt.kennung})
    antwort = owner_client.post(url, daten)
    assert antwort.status_code == 302
    return projekt.stufenregeln.filter(widerrufen_am__isnull=True).last()


def _fundstelle(e, text=QUELLTEXT[:40]):
    return {
        "quelle": e.nummer,
        "stand": e.stand,
        "sha256": hashlib.sha256(e.text.encode("utf-8")).hexdigest(),
        "text": text,
    }


def _entscheiden(client, zugang, projekt, **werte):
    werte.setdefault("anfrage_kennung", uuid.uuid4().hex)
    werte.setdefault("bereich", "technik")
    werte.setdefault("entscheidung", "Der CSV-Export schreibt UTF-8 ohne BOM.")
    return _werkzeug(
        client,
        zugang.klartext,
        "kipm_planung_entscheiden",
        projekt=projekt.kennung,
        fred_sagte=None,
        **werte,
    )


@pytest.fixture
def blockiert(akteur, projekt, bindung):
    a = arbeitspaket(akteur, projekt, titel="Export", umfang="export/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    frage = dienst.rueckfrage_melden(chat, a, a.revision_nr, "BOM oder ohne BOM?")
    a.refresh_from_db()
    return a, frage


# --- K1: Regel speichern, ohne Regel wie bisher, widerrufen ---------------------------------


def test_ohne_regel_lehnt_die_planung_ab(client, projekt, planung, quelle):
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert fehler and "Stufenregel" in daten["fehler"]
    assert not Planungsentscheid.objects.exists()


def test_owner_speichert_und_widerruft_die_regel(client, owner_client, projekt, planung, quelle):
    regel = _regel(owner_client, projekt, planung, a=False, b=True)
    assert regel is not None and regel.stufe_b and not regel.stufe_a
    seite = owner_client.get(reverse("projekt_einstellungen", kwargs={"kennung": projekt.kennung}))
    assert regel.nummer in seite.content.decode()
    assert Ereignis.objects.filter(ereignis="Stufenregel erteilt", objekt_kennung=regel.nummer)

    url = reverse("stufenregel_widerrufen", kwargs={"kennung": projekt.kennung, "nummer": regel.pk})
    assert owner_client.post(url).status_code == 302
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert fehler and "Stufenregel" in daten["fehler"]


def test_neue_regel_ersetzt_die_alte_desselben_zugangs(owner_client, projekt, planung):
    alt = _regel(owner_client, projekt, planung, a=True, b=False)
    neu = _regel(owner_client, projekt, planung, a=True, b=True)
    alt.refresh_from_db()
    assert alt.widerrufen_am is not None and neu.widerrufen_am is None


def test_projektschluessel_bekommt_keine_regel(owner_client, projekt, bindung):
    url = reverse("stufenregel_erteilen", kwargs={"kennung": projekt.kennung})
    owner_client.post(
        url,
        {
            "zugang": bindung.pk,
            "stufe_b": "on",
            "gueltig_bis": (timezone.localdate() + timedelta(days=3)).isoformat(),
        },
    )
    assert not projekt.stufenregeln.exists()


# --- K2: Stufe B bucht nur mit geprüfter Fundstelle -----------------------------------------


def test_stufe_b_bucht_mit_geprueft_fundstelle(client, owner_client, projekt, planung, quelle):
    regel = _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert not fehler, daten
    e = Entscheidung.objects.get(pk=int(daten["entscheidung"].removeprefix("E-")))
    assert e.zustand == Entscheidung.ENTSCHIEDEN
    pe = e.planungsentscheid
    assert pe.regel == regel and pe.zugang == planung
    assert pe.fundstelle["quelle"] == quelle.nummer and pe.fundstelle["wortlaut_geprueft"]
    ereignis = Ereignis.objects.get(objekt_kennung=e.nummer, ereignis__startswith="Von Planung")
    assert planung.name in ereignis.anlass and regel.nummer in ereignis.anlass
    assert quelle.nummer in ereignis.anlass and "Fred sagte" not in ereignis.anlass
    seite = owner_client.get(
        reverse("wissen_entscheidung", kwargs={"kennung": projekt.kennung, "nummer": e.pk})
    ).content.decode()
    assert "von Planung entschieden" in seite and regel.nummer in seite
    assert (
        "von Planung entschieden"
        in owner_client.get(
            reverse("akte_wissen", kwargs={"kennung": projekt.kennung})
        ).content.decode()
    )


def test_fundstelle_in_einem_text_des_projekts(client, owner_client, akteur, projekt, planung):
    _regel(owner_client, projekt, planung)
    u = akte.text_anlegen(akteur, projekt, titel="Exportregeln", text=QUELLTEXT)
    v = u.versionen.get(nummer=u.aktuelle_nr)
    fundstelle = {
        "quelle": u.kennung,
        "stand": u.aktuelle_nr,
        "sha256": v.sha256 or hashlib.sha256(v.text.encode()).hexdigest(),
        "text": QUELLTEXT[10:60],
    }
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=fundstelle)
    assert not fehler, daten


def test_masterspec_nur_mit_dem_gebundenen_hash(client, owner_client, projekt, planung):
    _regel(owner_client, projekt, planung, masterspec=MASTERSPEC)
    fundstelle = {
        "quelle": "masterspec",
        "sha256": MASTERSPEC,
        "stelle": "P12313",
        "text": "Die Shell zeigt die Navigation einheitlich an.",
    }
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=fundstelle)
    assert not fehler, daten
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=fundstelle | {"sha256": "8" * 64}
    )
    assert fehler and "MasterSpec" in daten["fehler"]


@pytest.mark.parametrize(
    "aenderung",
    [
        {"sha256": "0" * 64},
        {"stand": 99},
        {"text": "Dieser Satz steht nirgends in der Quelle drin."},
        {"quelle": "E-99999"},
        {"quelle": "masterspec", "stelle": "P1"},  # Regel bindet keine MasterSpec
        None,
    ],
    ids=["hash", "stand", "wortlaut", "unbekannt", "masterspec_ungebunden", "ohne"],
)
def test_ohne_pruefbare_fundstelle_nichts_gebucht(
    client, owner_client, projekt, planung, quelle, aenderung
):
    _regel(owner_client, projekt, planung)
    fundstelle = None if aenderung is None else _fundstelle(quelle) | aenderung
    werte = {} if fundstelle is None else {"fundstelle": fundstelle}
    vorher = Entscheidung.objects.count()
    fehler, daten = _entscheiden(client, planung, projekt, **werte)
    assert fehler, daten
    assert Entscheidung.objects.count() == vorher and not Planungsentscheid.objects.exists()


def test_gebuchte_planungsentscheidung_traegt_keine_weitere(
    client, owner_client, projekt, planung, quelle
):
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert not fehler
    gebucht = Entscheidung.objects.get(pk=int(daten["entscheidung"].removeprefix("E-")))
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(gebucht, gebucht.text[:30])
    )
    assert fehler and "Planung" in daten["fehler"]


# --- K3: Stufe C und fehlende Stufe ---------------------------------------------------------


@pytest.mark.parametrize(
    "werte",
    [
        {"bereich": "kosten"},
        {"bereich": "sicherheit"},
        {"entscheidung": "Wir nutzen Microsoft Graph für den Export."},
        {"entscheidung": "Die Berechtigung für das Archiv entfällt."},
        {"entscheidung": "Alte Exporte werden gelöscht."},
        {"warum": "Spart Kosten beim Speicher."},
    ],
    ids=["kosten", "sicherheit", "microsoft", "rechte", "loeschung", "kosten_im_warum"],
)
def test_stufe_c_wird_mit_grund_abgelehnt(client, owner_client, projekt, planung, quelle, werte):
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle), **werte)
    assert fehler and "Stufe C" in daten["fehler"]
    assert not Planungsentscheid.objects.exists()


def test_regel_nur_stufe_a_bucht_keine_entscheidung(client, owner_client, projekt, planung, quelle):
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert fehler and "Stufe B" in daten["fehler"]


# --- K4: abhängigen Auftrag fortsetzen, höchstens einmal ------------------------------------


def test_abhaengiger_auftrag_wird_beim_bisherigen_bearbeiter_fortgesetzt(
    client, owner_client, projekt, planung, quelle, blockiert, bindung
):
    a, frage = blockiert
    _regel(owner_client, projekt, planung)
    kennung = uuid.uuid4().hex
    werte = {
        "fundstelle": _fundstelle(quelle),
        "auftrag": a.nummer,
        "revision": a.revision_nr,
        "meldung_id": frage.pk,
        "anfrage_kennung": kennung,
    }
    fehler, daten = _entscheiden(client, planung, projekt, **werte)
    assert not fehler, daten
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT and a.bearbeiter_bindung_id == bindung.pk
    antwort = a.meldungen.filter(art=Meldung.ANTWORT).get()
    assert daten["entscheidung"] in antwort.text

    # Dieselbe Kennung: nichts Neues.
    fehler, wieder = _entscheiden(client, planung, projekt, **werte)
    assert not fehler and wieder.get("wiederholt") is True
    assert a.meldungen.filter(art=Meldung.ANTWORT).count() == 1
    assert Planungsentscheid.objects.count() == 1

    # Andere Kennung, dieselbe Blockademeldung: höchstens einmal.
    fehler, daten = _entscheiden(
        client, planung, projekt, **(werte | {"anfrage_kennung": uuid.uuid4().hex})
    )
    assert fehler
    assert Planungsentscheid.objects.count() == 1


def test_veraltete_blockademeldung_bucht_nichts(
    client, owner_client, projekt, planung, quelle, blockiert
):
    a, frage = blockiert
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(
        client,
        planung,
        projekt,
        fundstelle=_fundstelle(quelle),
        auftrag=a.nummer,
        revision=a.revision_nr,
        meldung_id=frage.pk + 1000,
    )
    assert fehler and not Planungsentscheid.objects.exists()
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT


def test_lieferblocker_bleibt_bei_der_lieferautomatik(
    client, owner_client, akteur, projekt, planung, quelle, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    paar = []
    for name in ("anbieter", "verbraucher"):
        x = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, x)
        x.refresh_from_db()
        dienst.auftrag_uebernehmen(chat, x, x.revision_nr)
        x.refresh_from_db()
        paar.append(x)
    anbieter, verbraucher = paar
    blocker = lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Anschluss fehlt",
        bedarf=[
            {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": "x"}
        ],
        nur_lieferungen=True,
    )
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(
        client,
        planung,
        projekt,
        fundstelle=_fundstelle(quelle),
        auftrag=verbraucher.nummer,
        revision=verbraucher.revision_nr,
        meldung_id=blocker.pk,
    )
    assert fehler and "Lieferautomatik" in daten["fehler"]


# --- K5: Widerruf in der Oberfläche ---------------------------------------------------------


def test_fred_widerruft_eine_planungsentscheidung(client, owner_client, projekt, planung, quelle):
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(client, planung, projekt, fundstelle=_fundstelle(quelle))
    assert not fehler
    e = Entscheidung.objects.get(pk=int(daten["entscheidung"].removeprefix("E-")))
    url = reverse(
        "planungsentscheid_widerrufen", kwargs={"kennung": projekt.kennung, "nummer": e.pk}
    )
    assert owner_client.post(url).status_code == 302
    e.refresh_from_db()
    assert e.zustand == Entscheidung.ZURUECKGEZOGEN
    assert e.planungsentscheid.widerrufen_am is not None
    assert Ereignis.objects.filter(
        objekt_kennung=e.nummer, ereignis="Planungsentscheidung widerrufen"
    ).exists()


# --- K6: Freigaben und Lieferkanten über Stufe A --------------------------------------------


def _geplant(akteur, projekt):
    return arbeitspaket(akteur, projekt, titel="Fertig geplant", umfang="geplant/**")


def test_stufe_a_gibt_fertig_geplante_auftraege_ohne_fred_sagte_frei(
    client, owner_client, akteur, projekt, planung
):
    a = _geplant(akteur, projekt)
    werte = {"auftrag": a.nummer, "revision": a.revision_nr, "fred_sagte": None}
    fehler, daten = _werkzeug(
        client, planung.klartext, "kipm_planung_freigeben", projekt=projekt.kennung, **werte
    )
    assert fehler and "Stufenregel" in daten["fehler"]

    regel = _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _werkzeug(
        client, planung.klartext, "kipm_planung_freigeben", projekt=projekt.kennung, **werte
    )
    assert not fehler, daten
    a.refresh_from_db()
    assert a.zustand == Auftrag.FREIGEGEBEN
    ereignis = Ereignis.objects.get(objekt_kennung=a.nummer, ereignis="Freigegeben")
    assert regel.nummer in ereignis.anlass and "Fred sagte" not in ereignis.anlass
    assert dienst.IM_CHAT_FREIGEGEBEN not in ereignis.anlass


def test_nur_stufe_b_gibt_nichts_frei(client, owner_client, akteur, projekt, planung):
    a = _geplant(akteur, projekt)
    _regel(owner_client, projekt, planung, a=False, b=True)
    fehler, daten = _werkzeug(
        client,
        planung.klartext,
        "kipm_planung_freigeben",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
        fred_sagte=None,
    )
    assert fehler and "Stufe A" in daten["fehler"]
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF


def test_stufe_a_traegt_eine_fehlende_lieferkante_nach(
    client, owner_client, akteur, projekt, planung, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    paar = []
    for name in ("anbieter", "verbraucher"):
        x = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, x)
        x.refresh_from_db()
        dienst.auftrag_uebernehmen(chat, x, x.revision_nr)
        x.refresh_from_db()
        paar.append(x)
    anbieter, verbraucher = paar
    hindernis = dienst.hindernis_melden(chat, verbraucher, verbraucher.revision_nr, "Fehlt")
    verbraucher.refresh_from_db()
    _regel(owner_client, projekt, planung, a=True, b=False)
    fehler, daten = _werkzeug(
        client,
        planung.klartext,
        "kipm_planung_lieferkante",
        projekt=projekt.kennung,
        auftrag=verbraucher.nummer,
        revision=verbraucher.revision_nr,
        hindernis_id=hindernis.pk,
        nur_lieferungen=True,
        bedarf=[
            {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": "x"}
        ],
        fred_sagte=None,
    )
    assert not fehler, daten
    assert hindernis.lieferbedarf.filter(lieferauftrag=anbieter).exists()


def test_regel_gilt_nur_fuer_ihren_zugang(client, owner_client, akteur, projekt, planung):
    anderer, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    a = _geplant(akteur, projekt)
    _regel(owner_client, projekt, planung, a=True, b=True)
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_planung_freigeben",
        projekt=projekt.kennung,
        auftrag=a.nummer,
        revision=a.revision_nr,
        fred_sagte=None,
    )
    assert fehler and "Stufenregel" in daten["fehler"]


# --- Gegenprüfung: Stufe A ergänzt nur, Stufe C auch für Daten und Ziel -----------------------


def _lieferkante(client, zugang, projekt, auftrag, bedarf, **werte):
    return _werkzeug(
        client,
        zugang.klartext,
        "kipm_planung_lieferkante",
        projekt=projekt.kennung,
        auftrag=auftrag.nummer,
        revision=auftrag.revision_nr,
        nur_lieferungen=True,
        bedarf=bedarf,
        fred_sagte=None,
        **werte,
    )


def test_stufe_a_entfernt_oder_ersetzt_keine_geplante_lieferkante(
    client, owner_client, akteur, projekt, planung
):
    anbieter = arbeitspaket(akteur, projekt, titel="Anbieter", umfang="anbieter/**")
    anderer = arbeitspaket(akteur, projekt, titel="Anderer", umfang="anderer/**")
    entwurf = _geplant(akteur, projekt)
    kante = {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": "x"}
    lieferungen.festlegen(
        akteur, entwurf, entwurf.revision_nr, bedarf=[kante], nur_lieferungen=True
    )
    _regel(owner_client, projekt, planung, a=True, b=False)

    fehler, daten = _lieferkante(client, planung, projekt, entwurf, [])
    assert fehler and "nur Fred" in daten["fehler"]
    ersatz = {"auftrag": anderer.nummer, "revision": anderer.revision_nr, "lieferkennung": "y"}
    fehler, daten = _lieferkante(client, planung, projekt, entwurf, [ersatz])
    assert fehler and "nur Fred" in daten["fehler"]
    assert lieferungen._geplante_bedarfe(entwurf, entwurf.revision_nr).get().lieferauftrag_id == (
        anbieter.pk
    )


@pytest.mark.parametrize(
    "entscheidung",
    [
        "Kundendaten werden in die neue Tabelle übernommen.",
        "Das Ziel von Modul Export ändert sich auf Berichte.",
    ],
    ids=["daten", "ziel"],
)
def test_stufe_c_auch_fuer_daten_und_ziel(
    client, owner_client, projekt, planung, quelle, entscheidung
):
    _regel(owner_client, projekt, planung)
    fehler, daten = _entscheiden(
        client, planung, projekt, fundstelle=_fundstelle(quelle), entscheidung=entscheidung
    )
    assert fehler and "Stufe C" in daten["fehler"]
    assert not Planungsentscheid.objects.exists()


def test_stufe_a_ersetzt_auch_keine_kante_auf_verschobenen_anbieter(
    client, owner_client, akteur, projekt, projekt_b, planung
):
    anbieter = arbeitspaket(akteur, projekt, titel="Anbieter", umfang="anbieter/**")
    anderer = arbeitspaket(akteur, projekt, titel="Anderer", umfang="anderer/**")
    entwurf = _geplant(akteur, projekt)
    kante = {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": "x"}
    lieferungen.festlegen(
        akteur, entwurf, entwurf.revision_nr, bedarf=[kante], nur_lieferungen=True
    )
    Auftrag.objects.filter(pk=anbieter.pk).update(projekt=projekt_b)  # inzwischen verschoben
    _regel(owner_client, projekt, planung, a=True, b=False)
    ersatz = {"auftrag": anderer.nummer, "revision": anderer.revision_nr, "lieferkennung": "y"}
    fehler, daten = _lieferkante(client, planung, projekt, entwurf, [ersatz])
    assert fehler and "nur Fred" in daten["fehler"]
    assert entwurf.meldungen.filter(art=Meldung.LIEFERPLAN, historisch=False).count() == 1

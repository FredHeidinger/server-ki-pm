"""KIPM-01: Lieferketten reißen bei einer Planungsänderung nicht mehr still ab.

Anbieter und Verbraucher warnen vor einer neuen Revision, verwaiste Bedarfe sind auf beiden
Seiten und in „Wartet auf dich“ sichtbar, der Manager bindet sie neu, und „Antworten“ nennt den
Lieferbedarf, den es verdrängt."""

import pytest

from steuerung import dienst, lieferungen, views
from steuerung.models import Auftrag, Ereignis, Meldung
from tests import test_lieferungen
from tests.conftest import arbeitspaket
from tests.test_api import api
from tests.test_lieferungen import _bedarf, _liefern, _post, _warten

paar = test_lieferungen.paar  # Fixture wiederverwenden

pytestmark = pytest.mark.django_db


@pytest.fixture
def manager(akteur):
    _, klartext = dienst.bindung_anlegen(akteur, None, name="Manager")
    return api(klartext)


def _planen(c, a, **daten):
    daten.setdefault("ziel", "Geändertes Ziel nach neuer Erkenntnis.")
    daten.setdefault("fred_sagte", "Ja, Ziel so ändern.")
    return _post(c, a, "planen", **daten)


def _antworten(c, a, **daten):
    daten.setdefault("text", "Mach ohne die Lieferung weiter.")
    daten.setdefault("fred_sagte", "Ja, so antworten.")
    return _post(c, a, "antworten", **daten)


def _verwaisen(manager, q, v):
    r = _planen(manager, q, lieferketten_bestaetigt=True)
    assert r.status_code == 200, r.content
    q.refresh_from_db()
    v.refresh_from_db()


# 1 ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("rolle", ["anbieter", "verbraucher"])
def test_aenderung_ohne_bestaetigung_abgelehnt_und_nennt_kette(paar, manager, akteur, rolle):
    chat, q, v = paar
    _warten(chat, v, q)
    ziel, gegenueber = (q, v) if rolle == "anbieter" else (v, q)
    vorher = ziel.revision_nr
    r = _planen(manager, ziel)
    assert r.status_code == 409, r.content
    text = r.json()["fehler"]
    assert gegenueber.nummer in text and "kontext" in text
    assert "lieferketten_bestaetigt" in text
    ziel.refresh_from_db()
    assert ziel.revision_nr == vorher and ziel.zustand != Auftrag.ENTWURF
    with pytest.raises(dienst.Konflikt):
        rev = ziel.aktuelle_revision
        dienst.auftrag_bearbeiten(
            akteur,
            ziel,
            titel=ziel.titel,
            beschreibung=rev.beschreibung,
            ziel="Anders",
            nicht_ziele=rev.nicht_ziele,
            arbeitsort=rev.arbeitsort,
            schreibumfang=rev.schreibumfang,
            quellen=rev.quellen,
            abnahmekriterien=rev.abnahmekriterien,
            prioritaet=ziel.prioritaet,
            modul=ziel.modul,
        )


# 2 ------------------------------------------------------------------------------------------------
def test_mit_bestaetigung_wird_bedarf_sichtbar_verwaist(paar, manager, projekt, owner_client):
    chat, q, v = paar
    _warten(chat, v, q)
    alte = q.revision_nr
    _verwaisen(manager, q, v)
    assert q.revision_nr == alte + 1 and q.zustand == Auftrag.ENTWURF
    assert v.zustand == Auftrag.BLOCKIERT

    eintrag = lieferungen.details(v)["lieferbedarf"][0]
    assert eintrag["verwaist"] is True and eintrag["status"] == "verwaist"
    assert str(q.revision_nr) in eintrag["grund"] and q.nummer in eintrag["grund"]

    anbieter = lieferungen.details(q)
    assert anbieter["angeforderte_lieferungen"] == []
    verwaist = anbieter["verwaiste_anforderungen"]
    assert [x["verbraucher"] for x in verwaist] == [v.nummer]
    assert verwaist[0]["gebunden_an_revision"] == alte and verwaist[0]["grund"]

    assert v in dienst.wartet_auf_dich(projekt)
    h = views.hauptaktion(v)
    assert [x["status"] for x in h["verwaist"]] == ["verwaist"]
    seite = owner_client.get(f"/projekt/{projekt.kennung}/").content.decode()
    assert v.nummer in seite and "verwaist" in seite

    ereignis = Ereignis.objects.filter(objekt_kennung=v.nummer, ereignis__icontains="verwaist")
    assert ereignis.exists()
    assert q.nummer in ereignis.first().anlass and str(q.revision_nr) in ereignis.first().anlass
    anbieter_ereignis = Ereignis.objects.get(
        objekt_kennung=q.nummer, ereignis=f"Revision {q.revision_nr} angelegt"
    )
    assert v.nummer in anbieter_ereignis.anlass


# 3 ------------------------------------------------------------------------------------------------
def test_manager_bindet_verwaisten_bedarf_neu(paar, manager, akteur):
    chat, q, v = paar
    blocker = _warten(chat, v, q)
    _verwaisen(manager, q, v)
    dienst.auftrag_freigeben(akteur, q)
    dienst.auftrag_uebernehmen(chat, q, q.revision_nr)
    bearbeiter, revision = v.bearbeiter_bindung_id, v.revision_nr

    _liefern(chat, q)  # neue Revision, aber Bedarf noch alt gebunden: weckt nicht
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT

    r = _post(
        manager,
        v,
        "lieferbedarf/festlegen",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
        hindernis_id=blocker.pk,
        fred_sagte="Ja, an die neue Revision binden.",
    )
    assert r.status_code == 200, r.content
    eintrag = lieferungen.details(v)["lieferbedarf"][0]
    assert eintrag["revision"] == q.revision_nr and eintrag["verwaist"] is False
    assert Ereignis.objects.filter(
        objekt_kennung=v.nummer, ereignis__icontains="neu gebunden"
    ).exists()

    v.refresh_from_db()
    if v.zustand == Auftrag.BLOCKIERT:  # Lieferung vor dem Neubinden zählt nicht als neu
        _liefern(chat, q, dateien=[f"src/core.py@{'c' * 40}"])
        v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT
    assert v.bearbeiter_bindung_id == bearbeiter and v.revision_nr == revision
    assert not v.ergebnisse.exists()


# 4 ------------------------------------------------------------------------------------------------
def test_neubinden_mit_veralteter_hindernis_id_bleibt_konflikt(paar, manager, akteur):
    chat, q, v = paar
    alt = _warten(chat, v, q)
    neu = _warten(chat, v, q)  # derselbe Bedarf neu gemeldet: neue Blockermeldung
    assert neu.pk != alt.pk
    _verwaisen(manager, q, v)
    r = _post(
        manager,
        v,
        "lieferbedarf/festlegen",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
        hindernis_id=alt.pk,
        fred_sagte="Ja, neu binden.",
    )
    assert r.status_code == 409, r.content
    eintrag = lieferungen.details(v)["lieferbedarf"][0]
    assert eintrag["verwaist"] is True


@pytest.mark.parametrize("fall", ["nicht_verwaist", "anderer_schluessel", "zusaetzlich"])
def test_neubinden_nur_fuer_verwaiste_bedarfe(paar, manager, akteur, fall):
    """Schutzgrenze des Neubindens: kein Umetikettieren eines gültigen Bedarfs."""
    chat, q, v = paar
    blocker = _warten(chat, v, q)
    if fall != "nicht_verwaist":
        _verwaisen(manager, q, v)
    bedarf = _bedarf(q, nach_lieferung=0)
    if fall == "nicht_verwaist":
        bedarf = _bedarf(q, lieferkennung="anders")
    elif fall == "anderer_schluessel":
        bedarf = _bedarf(q, lieferkennung="anders")
    else:
        bedarf = bedarf + [
            {"auftrag": q.nummer, "revision": q.revision_nr, "lieferkennung": "zweite"}
        ]
    vorher = [(b.lieferrevision, b.lieferkennung) for b in blocker.lieferbedarf.all()]
    r = _post(
        manager,
        v,
        "lieferbedarf/festlegen",
        bedarf=bedarf,
        nur_lieferungen=True,
        hindernis_id=blocker.pk,
        fred_sagte="Ja, neu binden.",
    )
    assert r.status_code == 409, r.content
    assert "Neu binden lassen sich nur verwaiste Bedarfe" in r.json()["fehler"]
    assert [(b.lieferrevision, b.lieferkennung) for b in blocker.lieferbedarf.all()] == vorher


# 5 ------------------------------------------------------------------------------------------------
def test_antworten_auf_lieferblocker_braucht_bestaetigung(paar, manager, owner_client):
    chat, q, v = paar
    _warten(chat, v, q)
    r = _antworten(manager, v)
    assert r.status_code == 409, r.content
    assert "Damit entfällt der technische Lieferbedarf auf" in r.json()["fehler"]
    assert f"{q.nummer}/kontext" in r.json()["fehler"]
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT

    seite = owner_client.get(f"/auftraege/{v.pk}/").content.decode()
    assert "Damit entfällt der technische Lieferbedarf auf" in seite
    assert 'name="lieferketten_bestaetigt"' in seite
    owner_client.post(f"/auftraege/{v.pk}/antwort/", {"text": "Ohne Häkchen"})
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT

    r = _antworten(manager, v, lieferketten_bestaetigt=True)
    assert r.status_code == 200, r.content
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT
    assert Ereignis.objects.filter(
        objekt_kennung=v.nummer, anlass__icontains="Lieferbedarf entfällt"
    ).exists()


def test_oberflaeche_antwort_mit_haekchen_hebt_auf(paar, owner_client):
    chat, q, v = paar
    _warten(chat, v, q)
    owner_client.post(
        f"/auftraege/{v.pk}/antwort/", {"text": "Mit Häkchen", "lieferketten_bestaetigt": "ja"}
    )
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_oberflaeche_bearbeiten_warnt_und_verlangt_haekchen(paar, owner_client):
    chat, q, v = paar
    _warten(chat, v, q)
    seite = owner_client.get(f"/auftraege/{q.pk}/bearbeiten/").content.decode()
    assert v.nummer in seite and "kontext" in seite
    assert 'name="lieferketten_bestaetigt"' in seite
    rev = q.aktuelle_revision
    daten = {
        "titel": q.titel,
        "prioritaet": q.prioritaet,
        "ziel": "Neu",
        "nicht_ziele": rev.nicht_ziele,
        "arbeitsort": rev.arbeitsort_id,
        "schreibumfang": rev.schreibumfang,
        "abnahmekriterien": rev.abnahmekriterien,
    }
    owner_client.post(f"/auftraege/{q.pk}/bearbeiten/", daten)
    q.refresh_from_db()
    assert q.zustand == Auftrag.IN_ARBEIT and q.revision_nr == rev.nummer
    owner_client.post(f"/auftraege/{q.pk}/bearbeiten/", daten | {"lieferketten_bestaetigt": "on"})
    q.refresh_from_db()
    assert q.zustand == Auftrag.ENTWURF
    assert lieferungen.details(v)["lieferbedarf"][0]["verwaist"] is True


# 6 ------------------------------------------------------------------------------------------------
def test_unbeteiligte_auftraege_verhalten_sich_wie_vorher(paar, manager, akteur, projekt):
    """Gegenprobe, schon vorher grün: ohne Lieferkette kein Bestätigungsfeld nötig."""
    chat, q, v = paar
    # erfüllte Kette: Verbraucher wieder in Arbeit, Anbieter ohne offene Anforderung
    _warten(chat, v, q)
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT
    for a in (q, v):
        r = _planen(manager, a)
        assert r.status_code == 200, r.content
        a.refresh_from_db()
        assert a.zustand == Auftrag.ENTWURF
    frei = arbeitspaket(akteur, projekt, titel="frei", umfang="frei/**")
    dienst.auftrag_freigeben(akteur, frei)
    dienst.auftrag_uebernehmen(chat, frei, frei.revision_nr)
    dienst.rueckfrage_melden(chat, frei, frei.revision_nr, "Welche Variante?")
    r = _antworten(manager, frei)
    assert r.status_code == 200, r.content
    frei.refresh_from_db()
    assert frei.zustand == Auftrag.IN_ARBEIT


# Abnahmekriterium 3: jeder Abbruchgrund sichtbar, an genau einer Stelle bestimmt ------------------
@pytest.mark.parametrize(
    "aenderung,status",
    [
        ("nichts", "wartet"),
        ("revision", "verwaist"),
        ("abbruch", "nicht_passend"),
        ("werkbank", "nicht_passend"),
    ],
)
def test_jeder_abbruchgrund_des_bedarfs_ist_benannt(paar, aenderung, status):
    chat, q, v = paar
    _warten(chat, v, q)
    if aenderung == "revision":
        Auftrag.objects.filter(pk=q.pk).update(revision_nr=q.revision_nr + 1)
    elif aenderung == "abbruch":
        Auftrag.objects.filter(pk=q.pk).update(zustand=Auftrag.ABGEBROCHEN)
    elif aenderung == "werkbank":
        Auftrag.objects.filter(pk=q.pk).update(ausfuehrungsweg=Auftrag.WEG_WERKBANK)
    eintrag = lieferungen.details(v)["lieferbedarf"][0]
    assert eintrag["status"] == status
    assert eintrag["grund"]
    assert eintrag["verwaist"] is (status == "verwaist")
    meldung = Meldung.objects.get(pk=v.meldungen.first().pk)
    lieferungen._freisetzen(dienst.Akteur.aus_bindung(v.bearbeiter_bindung), meldung)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT


def test_abgeloester_blocker_und_projektwechsel_sind_benannt(paar, projekt_b):
    chat, q, v = paar
    _warten(chat, v, q)
    dienst.rueckfrage_melden(chat, v, v.revision_nr, "Jetzt doch eine Frage")
    eintrag = lieferungen.details(v)["lieferbedarf"][0]
    assert eintrag["aktiv"] is False and eintrag["inaktiv_grund"]
    Auftrag.objects.filter(pk=q.pk).update(projekt=projekt_b, modul=None)
    details = lieferungen.details(v)
    assert details["lieferbedarf"] == []
    assert details["lieferbedarf_ausgeblendet"][0]["grund"]
    assert q.nummer not in str(details["lieferbedarf_ausgeblendet"])

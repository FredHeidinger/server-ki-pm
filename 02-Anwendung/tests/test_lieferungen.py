"""Lieferung -> gezielte technische Wiederaufnahme, ohne Freitext-/Rechtefreigabe."""

import json
import uuid

import pytest

from steuerung import dienst
from steuerung.models import Auftrag, Meldung
from tests.conftest import arbeitspaket
from tests.test_api import api

pytestmark = pytest.mark.django_db
COMMIT = "a" * 40


@pytest.fixture
def paar(akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    auftraege = []
    for name in ("quelle", "verbraucher"):
        a = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, a)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        auftraege.append(a)
    return chat, *auftraege


def _post(c, a, weg, **daten):
    daten.setdefault("revision", a.revision_nr)
    daten.setdefault("anfrage_kennung", uuid.uuid4().hex)
    return c.post(
        f"/api/auftraege/{a.pk}/{weg}/", json.dumps(daten), content_type="application/json"
    )


def _bedarf(quelle, **extra):
    return [
        {
            "auftrag": quelle.nummer,
            "revision": quelle.revision_nr,
            "lieferkennung": "kontext",
            **extra,
        }
    ]


def _warten(chat, verbraucher, quelle, **extra):
    from steuerung import lieferungen

    return lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Es fehlt der echte Kontextanschluss.",
        bedarf=_bedarf(quelle, **extra),
        nur_lieferungen=True,
    )


def _liefern(chat, quelle, **extra):
    from steuerung import lieferungen

    daten = dict(
        lieferkennung="kontext",
        zusammenfassung="Echter Kontextanschluss",
        dateien=[f"src/core.py@{COMMIT}"],
        pruefungen=["Echter Anschluss: positiv und negativ grün"],
    )
    return lieferungen.melden(chat, quelle, quelle.revision_nr, **(daten | extra))


def test_http_lieferung_weckt_genau_ihren_verbraucher(paar, bindung):
    _, quelle, verbraucher = paar
    c = api(bindung.klartext)
    r = _post(
        c,
        verbraucher,
        "lieferbedarf",
        text="Kontext fehlt",
        bedarf=_bedarf(quelle),
        nur_lieferungen=True,
    )
    assert r.status_code == 200, r.content
    assert r.json()["auftrag"]["zustand"] == Auftrag.BLOCKIERT
    quelle_detail = c.get(f"/api/auftraege/{quelle.pk}/").json()["auftrag"]
    assert quelle_detail["angeforderte_lieferungen"][0]["verbraucher"] == verbraucher.nummer
    assert quelle_detail["angeforderte_lieferungen"][0]["lieferkennung"] == "kontext"
    r = _post(
        c,
        quelle,
        "lieferung",
        lieferkennung="kontext",
        zusammenfassung="Kontextanschluss",
        dateien=[f"core.py@{COMMIT}"],
        pruefungen=["Anschluss grün"],
    )
    assert r.status_code == 200, r.content
    verbraucher.refresh_from_db()
    assert verbraucher.zustand == Auftrag.IN_ARBEIT
    assert verbraucher.bearbeiter_bindung_id == bindung.pk
    assert not verbraucher.ergebnisse.exists()
    detail = c.get(f"/api/auftraege/{verbraucher.pk}/").json()["auftrag"]
    assert detail["lieferbedarf"][0]["erfuellt_durch"] == r.json()["lieferung"]["nummer"]
    assert verbraucher.meldungen.first().art == Meldung.ANTWORT
    assert not c.get(f"/api/auftraege/{quelle.pk}/").json()["auftrag"]["angeforderte_lieferungen"]


def test_lieferung_vor_bedarf_wird_nicht_verloren(paar):
    chat, q, v = paar
    _liefern(chat, q)
    _warten(chat, v, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


@pytest.mark.parametrize("art", [Meldung.RUECKFRAGE, Meldung.FEHLER, Meldung.HINDERNIS])
def test_neuer_anderer_blocker_wird_nicht_aufgehoben(paar, art):
    chat, q, v = paar
    _warten(chat, v, q)
    fn = {
        Meldung.RUECKFRAGE: dienst.rueckfrage_melden,
        Meldung.FEHLER: dienst.fehler_melden,
        Meldung.HINDERNIS: dienst.hindernis_melden,
    }[art]
    fn(chat, v, v.revision_nr, "Werkzeug verweigert oder Entscheidung erforderlich")
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT


def test_erneuter_blocker_verbraucht_alte_lieferung_auch_ohne_cursor(paar):
    chat, q, v = paar
    erste = _liefern(chat, q)
    _warten(chat, v, q)
    _warten(chat, v, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    assert _liefern(chat, q).pk == erste.pk
    assert (
        _liefern(chat, q, zusammenfassung="Anders formuliert", pruefungen=["Neu behauptet"]).pk
        == erste.pk
    )
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    _liefern(chat, q, dateien=[f"src/core.py@{'b' * 40}"])
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_alle_bedarfe_muessen_geliefert_sein(paar):
    from steuerung import lieferungen

    chat, q, v = paar
    bedarf = _bedarf(q) + [
        {"auftrag": q.nummer, "revision": q.revision_nr, "lieferkennung": "quittung"}
    ]
    lieferungen.warten(
        chat, v, v.revision_nr, text="Zwei Lieferungen fehlen", bedarf=bedarf, nur_lieferungen=True
    )
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    _liefern(chat, q, lieferkennung="quittung")
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_falscher_lieferkennung_und_revision_wecken_nicht(paar):
    chat, q, v = paar
    _warten(chat, v, q)
    _liefern(chat, q, lieferkennung="andere")
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    Auftrag.objects.filter(pk=q.pk).update(revision_nr=q.revision_nr + 1)
    with pytest.raises(dienst.Konflikt):
        _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT


@pytest.mark.parametrize("aenderung", ["revision", "abbruch", "archiv", "werkbank"])
def test_veralteter_oder_beendeter_verbraucher_bleibt_unberuehrt(paar, projekt, aenderung):
    chat, q, v = paar
    _warten(chat, v, q)
    if aenderung == "revision":
        Auftrag.objects.filter(pk=v.pk).update(revision_nr=v.revision_nr + 1)
    elif aenderung == "abbruch":
        Auftrag.objects.filter(pk=v.pk).update(zustand=Auftrag.ABGEBROCHEN)
    elif aenderung == "werkbank":
        Auftrag.objects.filter(pk=v.pk).update(ausfuehrungsweg=Auftrag.WEG_WERKBANK)
    else:
        projekt.aktiv = False
        projekt.save(update_fields=["aktiv"])
        with pytest.raises(dienst.Konflikt):
            _liefern(chat, q)
        return
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand != Auftrag.IN_ARBEIT


@pytest.mark.parametrize(
    "daten",
    [
        {"pruefungen": []},
        {"dateien": ["core.py@kurz"]},
        {"dateien": "core.py@x"},
        {"fehlgeschlagen": ["Anschluss rot"]},
        {"lieferkennung": "../falsch"},
        {"fehlgeschlagen": False},
    ],
)
def test_unbelegte_oder_fehlgeschlagene_lieferung_weckt_nicht(paar, daten):
    chat, q, v = paar
    _warten(chat, v, q)
    with pytest.raises(dienst.DienstFehler):
        _liefern(chat, q, **daten)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    assert not q.lieferungen.exists()


def test_mischblocker_braucht_explizit_nur_lieferungen(paar, bindung):
    _, q, v = paar
    r = _post(
        api(bindung.klartext),
        v,
        "lieferbedarf",
        text="Lieferung und Freds Entscheidung",
        bedarf=_bedarf(q),
        nur_lieferungen=False,
    )
    assert r.status_code == 400
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_selbstabhaengigkeit_und_kreis_verweigert(paar):
    chat, q, v = paar
    with pytest.raises(dienst.DienstFehler, match="kreis"):
        _warten(chat, v, v)
    _warten(chat, v, q)
    with pytest.raises(dienst.DienstFehler, match="kreis"):
        _warten(chat, q, v)


def test_fremdes_projekt_und_fremder_bearbeiter_verweigert(paar, bindung_b):
    from steuerung import lieferungen

    chat, q, v = paar
    fremder = dienst.Akteur.aus_bindung(bindung_b)
    with pytest.raises(dienst.KeinZugriff):
        _liefern(fremder, q)
    with pytest.raises(dienst.KeinZugriff):
        lieferungen.warten(
            fremder, v, v.revision_nr, text="x", bedarf=_bedarf(q), nur_lieferungen=True
        )
    assert not q.lieferungen.exists()


def test_nur_ausdruecklich_registrierte_blockade(paar):
    chat, q, v = paar
    dienst.hindernis_melden(chat, v, v.revision_nr, "Warte auf Kontext, aber auch Werkzeugfreigabe")
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT


def test_managerordnet_altblocker_ohne_sitzungsende_zu_verdecken(paar, akteur):
    from steuerung import lieferungen

    chat, q, v = paar
    hindernis = dienst.hindernis_melden(chat, v, v.revision_nr, "Kontext fehlt")
    dienst.sitzungsende_melden(chat, v)
    ende = v.meldungen.first()
    lieferungen.festlegen(
        akteur, v, v.revision_nr, bedarf=_bedarf(q), nur_lieferungen=True, hindernis_id=hindernis.pk
    )
    assert v.meldungen.first().pk == ende.pk
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT
    assert dienst.verstummt(v)["beendet"] is True


def test_managerfrage_oder_fehler_nicht_als_lieferbedarf_einordnen(paar, akteur):
    from steuerung import lieferungen

    chat, q, v = paar
    dienst.rueckfrage_melden(chat, v, v.revision_nr, "Fred muss entscheiden")
    with pytest.raises(dienst.DienstFehler):
        lieferungen.festlegen(akteur, v, v.revision_nr, bedarf=_bedarf(q), nur_lieferungen=True)


def test_get_lesen_ist_wirkungslos(paar, bindung):
    chat, q, v = paar
    _warten(chat, v, q)
    vorher = v.meldungen.count()
    c = api(bindung.klartext)
    for _ in range(2):
        assert c.get(f"/api/auftraege/{v.pk}/").status_code == 200
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT and v.meldungen.count() == vorher


def test_http_wiederholung_idempotent(paar, bindung):
    chat, q, v = paar
    _warten(chat, v, q)
    c = api(bindung.klartext)
    daten = dict(
        lieferkennung="kontext",
        zusammenfassung="Kontextanschluss",
        dateien=[f"x@{COMMIT}"],
        pruefungen=["Anschluss grün"],
        anfrage_kennung="liefere-einmal",
    )
    a = _post(c, q, "lieferung", **daten)
    b = _post(c, q, "lieferung", **daten)
    assert a.status_code == b.status_code == 200
    wiederholt = b.json()
    assert wiederholt.pop("wiederholt") is True
    assert a.json() == wiederholt
    assert v.meldungen.filter(art=Meldung.ANTWORT).count() == 1
    assert q.lieferungen.count() == 1


def test_mcp_vollstaendiger_lieferweg_ohne_neue_fredfreigabe(paar, bindung, client, projekt):
    from tests.test_mcp import _werkzeug

    _, q, v = paar

    def rufe(name, **daten):
        fehler, antwort = _werkzeug(
            client, bindung.klartext, name, projekt=projekt.kennung, fred_sagte=None, **daten
        )
        assert not fehler, antwort
        return antwort

    rufe(
        "kipm_warten_auf_lieferung",
        auftrag=v.nummer,
        revision=v.revision_nr,
        text="Kontext fehlt",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
    )
    rufe(
        "kipm_lieferung_melden",
        auftrag=q.nummer,
        revision=q.revision_nr,
        lieferkennung="kontext",
        zusammenfassung="Kontextanschluss",
        dateien=[f"x@{COMMIT}"],
        pruefungen=["Anschluss grün"],
    )
    antwort = rufe("kipm_lesen", auftrag=v.nummer)
    assert antwort["auftrag"]["zustand"] == Auftrag.IN_ARBEIT


def test_managerweg_braucht_fredworte_und_planungsrecht(paar, bindung):
    chat, q, v = paar
    dienst.hindernis_melden(chat, v, v.revision_nr, "Nur Lieferung fehlt")
    c = api(bindung.klartext)
    r = _post(c, v, "lieferbedarf/festlegen", bedarf=_bedarf(q), nur_lieferungen=True)
    assert r.status_code == 400 and "Fred" in r.json()["fehler"]
    r = _post(
        c,
        v,
        "lieferbedarf/festlegen",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
        fred_sagte="Diesen technischen Bedarf zuordnen",
    )
    assert r.status_code == 403


def test_andere_lieferquelle_bleibt_unpassend(paar, akteur, projekt, bindung):
    chat, q, v = paar
    andere = arbeitspaket(akteur, projekt, titel="Andere Quelle", umfang="andere/**")
    dienst.auftrag_freigeben(akteur, andere)
    dienst.auftrag_uebernehmen(chat, andere, andere.revision_nr)
    _warten(chat, v, q)
    _liefern(chat, andere)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT


def test_fremder_nach_lieferung_cursor_verweigert(paar):
    chat, q, v = paar
    lieferung = _liefern(chat, q, lieferkennung="andere")
    with pytest.raises(dienst.DienstFehler):
        _warten(chat, v, q, nach_lieferung=lieferung.pk)
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_gleichzeitige_liefermeldungen_erzeugen_nur_eine_wiederaufnahme(
    transactional_db, akteur, projekt, bindung
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    chat = dienst.Akteur.aus_bindung(bindung)
    jobs = []
    for n in ("quelle", "verbraucher"):
        a = arbeitspaket(akteur, projekt, titel=n, umfang=f"{n}/**")
        dienst.auftrag_freigeben(akteur, a)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        jobs.append(a)
    q, v = jobs
    _warten(chat, v, q)
    start = Barrier(2)

    def lauf(_):
        close_old_connections()
        try:
            start.wait(timeout=5)
            return _liefern(chat, q).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        nummern = list(pool.map(lauf, range(2)))
    assert nummern[0] == nummern[1]
    assert v.meldungen.filter(art=Meldung.ANTWORT).count() == 1
    assert q.lieferungen.count() == 1


@pytest.mark.parametrize("revision", [True, 2.5, "2"])
def test_http_revision_ist_strikt_ganzzahlig(paar, bindung, revision):
    _, q, v = paar
    r = _post(
        api(bindung.klartext),
        v,
        "lieferbedarf",
        revision=revision,
        text="Kontext fehlt",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
    )
    assert r.status_code == 400
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT


def test_gleichzeitige_neue_rueckfrage_ueberlebt(transactional_db, akteur, projekt, bindung):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    chat = dienst.Akteur.aus_bindung(bindung)
    jobs = []
    for n in ("q", "v"):
        a = arbeitspaket(akteur, projekt, titel=n, umfang=f"{n}/**")
        dienst.auftrag_freigeben(akteur, a)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        jobs.append(a)
    q, v = jobs
    _warten(chat, v, q)
    start = Barrier(2)

    def lauf(art):
        close_old_connections()
        try:
            start.wait(timeout=5)
            if art == "lieferung":
                _liefern(chat, q)
            else:
                dienst.rueckfrage_melden(chat, v, v.revision_nr, "Jetzt muss Fred entscheiden")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lauf, ["lieferung", "frage"]))
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    assert v.meldungen.exclude(art=Meldung.FORTSCHRITT).first().art == Meldung.RUECKFRAGE


def test_keine_quellinformationen_nach_projektwechsel(paar, projekt_b):
    from steuerung import lieferungen

    chat, q, v = paar
    _warten(chat, v, q)
    Auftrag.objects.filter(pk=v.pk).update(projekt=projekt_b, modul=None)
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    assert lieferungen.details(v)["lieferbedarf"] == []
    assert lieferungen.details(q)["angeforderte_lieferungen"] == []


def test_manager_sieht_alte_meldung_und_darf_neuen_werkzeugblocker_nicht_umetikettieren(
    paar, akteur, bindung
):
    chat, q, v = paar
    alt = dienst.hindernis_melden(chat, v, v.revision_nr, "Nur technische Lieferung fehlt")
    neu = dienst.hindernis_melden(
        chat, v, v.revision_nr, "Inzwischen wurde ein Werkzeug verweigert"
    )
    _, klartext = dienst.bindung_anlegen(akteur, None, name="Manager")
    r = _post(
        api(klartext),
        v,
        "lieferbedarf/festlegen",
        bedarf=_bedarf(q),
        nur_lieferungen=True,
        hindernis_id=alt.pk,
        fred_sagte="Technischen Altblocker einordnen",
    )
    assert r.status_code == 409
    assert not neu.lieferbedarf.exists()
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT
    assert v.meldungen.first().pk == neu.pk


def test_manager_pin_ist_strikt_und_fehlt_nicht_stillschweigend(paar, akteur):
    from steuerung import lieferungen

    chat, q, v = paar
    h = dienst.hindernis_melden(chat, v, v.revision_nr, "Nur technische Lieferung fehlt")
    for pin in (None, True, str(h.pk)):
        with pytest.raises(dienst.DienstFehler):
            lieferungen.festlegen(
                akteur, v, v.revision_nr, bedarf=_bedarf(q), nur_lieferungen=True, hindernis_id=pin
            )
    assert not h.lieferbedarf.exists()


@pytest.mark.parametrize("art", [Meldung.RUECKFRAGE, Meldung.FEHLER, Meldung.HINDERNIS])
def test_bearbeiter_darf_bestehenden_ungeklaerten_blocker_nicht_ueberschreiben(paar, bindung, art):
    chat, q, v = paar
    _liefern(chat, q)
    fn = {
        Meldung.RUECKFRAGE: dienst.rueckfrage_melden,
        Meldung.FEHLER: dienst.fehler_melden,
        Meldung.HINDERNIS: dienst.hindernis_melden,
    }[art]
    h = fn(chat, v, v.revision_nr, "Entscheidung oder Werkzeugverweigerung bleibt offen")
    r = _post(
        api(bindung.klartext),
        v,
        "lieferbedarf",
        text="Nur Lieferung fehlt",
        nur_lieferungen=True,
        bedarf=_bedarf(q),
    )
    assert r.status_code == 409
    v.refresh_from_db()
    assert v.zustand == Auftrag.BLOCKIERT and v.meldungen.first().pk == h.pk
    assert not h.lieferbedarf.exists()


def test_bestehender_reiner_lieferbedarf_darf_aktualisiert_werden(paar):
    chat, q, v = paar
    _warten(chat, v, q)
    _warten(chat, v, q)
    _liefern(chat, q)
    v.refresh_from_db()
    assert v.zustand == Auftrag.IN_ARBEIT
    assert v.meldungen.filter(art=Meldung.ANTWORT).count() == 1

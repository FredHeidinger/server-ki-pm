"""KIPM-29: Ein Anbieterwechsel beim Lieferbedarf wirkt nur mit einer von Fred bestätigten,
strukturierten Zulassung „Lieferersatz“ (Verbraucher, Revision, Meldung des Lieferblockers,
Lieferkennung, alter und neuer Anbieter mit Revision, Liefernummer, Inhaltshash). Nachführen
ohne Anbietertausch bleibt unverändert."""

import uuid

import pytest

from steuerung import dienst, lieferungen
from steuerung.models import Auftrag, Lieferersatz
from tests.conftest import arbeitspaket
from tests.test_api import api, post

pytestmark = pytest.mark.django_db
COMMIT = "b" * 40


@pytest.fixture
def lage(akteur, projekt, bindung):
    """Verbraucher wartet auf „kontext“ vom alten Anbieter; der neue hat schon geliefert."""
    chat = dienst.Akteur.aus_bindung(bindung)
    auftraege = []
    for name in ("alt", "neu", "verbraucher"):
        a = arbeitspaket(akteur, projekt, titel=name, umfang=f"{name}/**")
        dienst.auftrag_freigeben(akteur, a)
        a.refresh_from_db()
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        a.refresh_from_db()
        auftraege.append(a)
    alt, neu, verbraucher = auftraege
    blocker = lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Es fehlt der Kontextanschluss.",
        bedarf=[_eintrag(alt)],
        nur_lieferungen=True,
    )
    lieferung = lieferungen.melden(
        chat,
        neu,
        neu.revision_nr,
        lieferkennung="kontext",
        zusammenfassung="Kontextanschluss vom neuen Anbieter",
        dateien=[f"src/kontext.py@{COMMIT}"],
        pruefungen=["Anschluss grün"],
    )
    for a in auftraege:
        a.refresh_from_db()
    return chat, alt, neu, verbraucher, blocker, lieferung


def _eintrag(anbieter, kennung="kontext"):
    return {"auftrag": anbieter.nummer, "revision": anbieter.revision_nr, "lieferkennung": kennung}


def _werte(lage, **abweichend):
    _, alt, neu, verbraucher, blocker, lieferung = lage
    return {
        "revision": verbraucher.revision_nr,
        "meldung_id": blocker.pk,
        "lieferkennung": "kontext",
        "alt_auftrag": alt.nummer,
        "alt_revision": alt.revision_nr,
        "neu_auftrag": neu.nummer,
        "neu_revision": neu.revision_nr,
        "lieferung": lieferung.pk,
        "inhalt_sha256": lieferung.inhalt_sha256,
    } | abweichend


def _zulassen(akteur, lage, **abweichend):
    verbraucher = lage[3]
    return lieferungen.ersatz_zulassen(akteur, verbraucher, **_werte(lage, **abweichend))


def _wechseln(lage):
    chat, _, neu, verbraucher, _, _ = lage
    return lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Der Anschluss kommt jetzt vom neuen Anbieter.",
        bedarf=[_eintrag(neu)],
        nur_lieferungen=True,
    )


def test_wechsel_ohne_zulassung_wird_abgelehnt(lage):
    _, _, _, verbraucher, blocker, _ = lage
    with pytest.raises(dienst.Konflikt, match="Lieferersatz"):
        _wechseln(lage)
    verbraucher.refresh_from_db()
    assert verbraucher.zustand == Auftrag.BLOCKIERT
    assert lieferungen._blocker(verbraucher).pk == blocker.pk


def test_mit_passender_zulassung_wird_der_wechsel_angenommen(akteur, lage):
    _, _, neu, verbraucher, blocker, lieferung = lage
    z = _zulassen(akteur, lage)
    meldung = _wechseln(lage)
    assert meldung.pk != blocker.pk
    assert [b.lieferauftrag_id for b in meldung.lieferbedarf.all()] == [neu.pk]
    verbraucher.refresh_from_db()
    # Die schon vorhandene Lieferung des neuen Anbieters setzt sofort frei.
    assert verbraucher.zustand == Auftrag.IN_ARBEIT
    z.refresh_from_db()
    assert z.angewendet_meldung_id == meldung.pk
    assert z.lieferung_id == lieferung.pk


@pytest.mark.parametrize(
    "abweichend",
    [
        {"revision": 99},
        {"meldung_id": 999999},
        {"lieferung": 999999},
        {"inhalt_sha256": "c" * 64},
        {"neu_revision": 99},
        {"alt_auftrag": "neu"},
        {"lieferkennung": "anderes"},
    ],
    ids=[
        "revision",
        "meldung",
        "liefernummer",
        "hash",
        "neue_revision",
        "alter_anbieter",
        "kennung",
    ],
)
def test_abweichende_zulassung_wird_abgelehnt(akteur, lage, abweichend):
    if abweichend.get("alt_auftrag") == "neu":
        abweichend = {"alt_auftrag": lage[2].nummer}
    with pytest.raises((dienst.Konflikt, dienst.DienstFehler)):
        _zulassen(akteur, lage, **abweichend)
    assert not Lieferersatz.objects.exists()
    with pytest.raises(dienst.Konflikt, match="Lieferersatz"):
        _wechseln(lage)


def test_veraltete_zulassung_wirkt_nicht_mehr(akteur, lage):
    chat, alt, _, verbraucher, _, _ = lage
    _zulassen(akteur, lage)
    # Neuer Blocker durch Nachführen beim alten Anbieter: die Zulassung hängt an der alten Meldung.
    lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Noch einmal beim alten Anbieter.",
        bedarf=[_eintrag(alt)],
        nur_lieferungen=True,
    )
    with pytest.raises(dienst.Konflikt, match="Lieferersatz"):
        _wechseln(lage)


def test_nachfuehren_ohne_anbietertausch_bleibt_unveraendert(lage):
    """Altverträglichkeit, darum von Anfang an grün: gleicher Anbieter braucht keine Zulassung."""
    chat, alt, _, verbraucher, blocker, _ = lage
    meldung = lieferungen.warten(
        chat,
        verbraucher,
        verbraucher.revision_nr,
        text="Weiter beim alten Anbieter.",
        bedarf=[_eintrag(alt)],
        nur_lieferungen=True,
    )
    assert meldung.pk != blocker.pk
    assert [b.lieferauftrag_id for b in meldung.lieferbedarf.all()] == [alt.pk]


def test_zulassung_nur_mit_freds_worten_und_im_einzelabruf(akteur, lage):
    _, _, neu, verbraucher, blocker, lieferung = lage
    pc, schluessel = dienst.bindung_anlegen(akteur, None, name="Codex PC")
    c = api(schluessel)
    pfad = f"/api/auftraege/{verbraucher.pk}/lieferersatz/"
    daten = _werte(lage) | {"anfrage_kennung": uuid.uuid4().hex}
    ohne = c.post(pfad, data=daten, content_type="application/json")
    assert ohne.status_code == 400 and "fred_sagte" in ohne.json()["fehler"]
    assert not Lieferersatz.objects.exists()

    r = post(c, pfad, **daten)
    assert r.status_code == 200, r.content
    ersatz = r.json()["auftrag"]["lieferersatz"]
    assert ersatz == [
        {
            "nummer": f"LE-{Lieferersatz.objects.get().pk}",
            "verbraucher": verbraucher.nummer,
            "revision": verbraucher.revision_nr,
            "meldung_id": blocker.pk,
            "lieferkennung": "kontext",
            "alt": f"{lage[1].nummer}/{lage[1].revision_nr}",
            "neu": f"{neu.nummer}/{neu.revision_nr}",
            "lieferung": lieferung.pk,
            "inhalt_sha256": lieferung.inhalt_sha256,
            "angewendet_meldung_id": None,
        }
    ]
    lesen = c.get(f"/api/auftraege/{verbraucher.pk}/").json()["auftrag"]
    assert lesen["lieferersatz"] == ersatz


def test_projektschluessel_darf_keine_zulassung_anlegen(lage, bindung):
    verbraucher = lage[3]
    r = post(
        api(bindung.klartext),
        f"/api/auftraege/{verbraucher.pk}/lieferersatz/",
        **_werte(lage),
        anfrage_kennung=uuid.uuid4().hex,
    )
    assert r.status_code == 403
    assert not Lieferersatz.objects.exists()

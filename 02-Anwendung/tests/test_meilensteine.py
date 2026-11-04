"""Umbau 4/6 (A-6): Meilensteine mit Name und Datum, Zuordnung durch den Chat, automatische Zählung."""

import datetime

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.forms import MeilensteinForm
from steuerung.models import Auftrag, Ereignis, Meilenstein, Objektversion
from tests.conftest import PASSWORT, arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def _ueberblick(client, projekt) -> str:
    return client.get(
        reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
    ).content.decode()


def _chat(akteur, auftrag):
    """Der Prüfer darf nicht selbst bearbeiten; jeder Auftrag bekommt einen eigenen Chat."""
    b, _ = dienst.bindung_anlegen(akteur, auftrag.projekt, name=f"Chat {auftrag.nummer}")
    return dienst.Akteur.aus_bindung(b, projekt=auftrag.projekt)


def _erledigen(akteur, auftrag):
    dienst.auftrag_freigeben(akteur, auftrag)
    auftrag.refresh_from_db()
    chat = _chat(akteur, auftrag)
    dienst.auftrag_uebernehmen(chat, auftrag, auftrag.revision_nr)
    dienst.ergebnis_einreichen(
        chat,
        auftrag,
        auftrag.revision_nr,
        zusammenfassung="Fertig.",
        dateien="a.py@abc1234",
        pruefungen="pytest: 1 bestanden",
    )
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ERLEDIGT


def _zuordnen(akteur, auftrag, m):
    dienst.meilenstein_zuordnen(akteur, auftrag, m)
    auftrag.refresh_from_db()
    return auftrag


# --- Name und Datum: Chat und Fred über denselben Dienst -------------------------------------


def test_form_hat_nur_name_und_datum():
    assert set(MeilensteinForm().fields) == {"revision", "name", "datum"}


@pytest.mark.parametrize("zugang", ["projektschluessel", "pc"])
def test_chat_legt_an_und_bearbeitet(client, projekt, bindung, pc_zugang, zugang):
    schluessel, zusatz = (
        (bindung.klartext, {})
        if zugang == "projektschluessel"
        else (pc_zugang.klartext, {"projekt": "synth-a"})
    )
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_meilenstein_anlegen",
        name="Erste Version",
        datum="2026-12-01",
        **zusatz,
    )
    assert not fehler, daten
    angelegt = daten["meilenstein"]
    assert angelegt["name"] == "Erste Version" and angelegt["datum"] == "2026-12-01"
    assert angelegt["revision"] == 1 and angelegt["stand"] == "noch keine Aufträge"
    m = Meilenstein.objects.get()
    assert angelegt["nummer"] == m.nummer == f"M-{m.pk}"

    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_meilenstein_aendern",
        meilenstein=m.nummer,
        revision=1,
        name="Erste Fassung",
        **zusatz,
    )
    assert not fehler, daten
    assert daten["meilenstein"]["name"] == "Erste Fassung"
    assert daten["meilenstein"]["datum"] == "2026-12-01"  # nur Angegebenes ändert sich

    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_meilenstein_aendern",
        meilenstein=m.nummer,
        revision=2,
        datum="",
        **zusatz,
    )
    assert not fehler and daten["meilenstein"]["datum"] is None

    fehler, daten = _werkzeug(client, schluessel, "kipm_meilensteine", **zusatz)
    assert not fehler and [x["name"] for x in daten["meilensteine"]] == ["Erste Fassung"]
    assert daten["meilensteine"][0]["revision"] == 3


def test_aenderung_steht_im_verlauf_mit_version(akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1", datum="2026-12-01")
    akte.meilenstein_bearbeiten(akteur, m, erwartete_revision=1, name="M2")
    ereignisse = list(
        Ereignis.objects.filter(projekt=projekt, objekt_typ="Meilenstein")
        .order_by("id")
        .values_list("ereignis", "objekt_kennung", "anlass")
    )
    assert ereignisse == [
        ("Meilenstein angelegt", m.nummer, "M1"),
        ("Meilenstein geändert", m.nummer, "M2"),
    ]
    versionen = Objektversion.objects.filter(objekt_typ="Meilenstein", objekt_id=m.pk)
    assert [v.daten for v in versionen.order_by("nummer")] == [
        {"name": "M1", "datum": "2026-12-01"},
        {"name": "M2", "datum": "2026-12-01"},
    ]


def test_unveraendert_speichern_schreibt_nichts(akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1", datum="2026-12-01")
    vorher = Ereignis.objects.count()
    m = akte.meilenstein_bearbeiten(
        akteur, m, erwartete_revision=1, name="M1", datum=datetime.date(2026, 12, 1)
    )
    assert m.revision == 1 and Ereignis.objects.count() == vorher


@pytest.mark.parametrize(
    ("felder", "text"),
    [
        ({"name": "  "}, "Name"),
        ({"name": "x" * 161}, "zu lang"),
        ({"name": "M", "datum": "morgen"}, "ungültig"),
        ({"name": "M", "datum": "2026-13-40"}, "ungültig"),
    ],
)
def test_ungueltige_eingaben_abgewiesen(client, bindung, felder, text):
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_meilenstein_anlegen", **felder)
    assert fehler and text in daten["fehler"]
    assert not Meilenstein.objects.exists()


def test_veraltete_revision_abgewiesen(client, akteur, projekt, bindung):
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1")
    akte.meilenstein_bearbeiten(akteur, m, erwartete_revision=1, name="M2")
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_meilenstein_aendern",
        meilenstein=m.nummer,
        revision=1,
        name="Alt",
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]
    m.refresh_from_db()
    assert m.name == "M2"


def test_aendern_braucht_gueltige_angaben(client, akteur, projekt, bindung):
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1")
    for argumente in (
        {"meilenstein": "M-x", "revision": 1},
        {"meilenstein": m.nummer, "revision": "x"},
        {"meilenstein": m.nummer},
    ):
        fehler, _ = _werkzeug(client, bindung.klartext, "kipm_meilenstein_aendern", **argumente)
        assert fehler, argumente


def test_wiederholung_mit_gleicher_kennung_wirkt_einmal(client, bindung):
    for _ in range(2):
        fehler, _daten = _werkzeug(
            client,
            bindung.klartext,
            "kipm_meilenstein_anlegen",
            name="Einmal",
            anfrage_kennung="m-1",
        )
        assert not fehler
    assert Meilenstein.objects.count() == 1


def test_fred_legt_von_hand_an_und_bearbeitet_im_ueberblick(owner_client, akteur, projekt):
    seite = _ueberblick(owner_client, projekt)
    assert "+ Meilenstein" in seite and "Noch keine Meilensteine" in seite
    neu = reverse("meilenstein_neu", kwargs={"kennung": "synth-a"})
    antwort = owner_client.post(neu, {"m-name": "Von Hand", "m-datum": "2026-12-01"})
    assert antwort.status_code == 302 and antwort["Location"].endswith("#meilensteine")
    m = Meilenstein.objects.get()
    assert m.name == "Von Hand" and m.termin == datetime.date(2026, 12, 1)

    seite = _ueberblick(owner_client, projekt)
    assert "bis 01.12.2026" in seite
    # Das Datumsfeld des Browsers versteht nur ISO; sonst bliebe es leer und ginge verloren.
    assert 'value="2026-12-01"' in seite

    url = reverse("meilenstein_bearbeiten", kwargs={"kennung": "synth-a", "pk": m.pk})
    owner_client.post(
        url, {f"m{m.pk}-revision": 1, f"m{m.pk}-name": "Geändert", f"m{m.pk}-datum": ""}
    )
    m.refresh_from_db()
    assert (m.name, m.termin, m.revision) == ("Geändert", None, 2)

    # veraltete Revision, leerer Name und ungültiges Datum ändern nichts
    for felder in (
        {f"m{m.pk}-revision": 1, f"m{m.pk}-name": "Alt"},
        {f"m{m.pk}-revision": 2, f"m{m.pk}-name": ""},
        {f"m{m.pk}-revision": 2, f"m{m.pk}-name": "X", f"m{m.pk}-datum": "kein Datum"},
    ):
        owner_client.post(url, felder)
        m.refresh_from_db()
        assert m.name == "Geändert"


def test_von_hand_nur_der_owner(client, akteur, projekt):
    get_user_model().objects.create_user(username="zweiter", password=PASSWORT)
    assert client.login(username="zweiter", password=PASSWORT)
    client.post(reverse("meilenstein_neu", kwargs={"kennung": "synth-a"}), {"m-name": "X"})
    assert not Meilenstein.objects.exists()
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1")
    client.post(
        reverse("meilenstein_bearbeiten", kwargs={"kennung": "synth-a", "pk": m.pk}),
        {f"m{m.pk}-name": "Fremd"},
    )
    m.refresh_from_db()
    assert m.name == "M1"


def test_fremder_meilenstein_nicht_bearbeitbar(
    client, owner_client, akteur, projekt, projekt_b, bindung
):
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_meilenstein_aendern",
        meilenstein=fremd.nummer,
        revision=1,
        name="Übernommen",
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."
    antwort = owner_client.post(
        reverse("meilenstein_bearbeiten", kwargs={"kennung": "synth-a", "pk": fremd.pk}),
        {f"m{fremd.pk}-name": "Übernommen"},
    )
    assert antwort.status_code == 404
    fremd.refresh_from_db()
    assert fremd.name == "Fremd"


def test_projektschluessel_sieht_und_aendert_nur_das_eigene_projekt(
    client, akteur, projekt, projekt_b, bindung
):
    akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_meilensteine", projekt="synth-b")
    assert fehler or [x["name"] for x in daten["meilensteine"]] == ["Eigen"]
    fehler, _ = _werkzeug(
        client, bindung.klartext, "kipm_meilenstein_anlegen", name="Hinein", projekt="synth-b"
    )
    assert fehler or not projekt_b.meilensteine.filter(name="Hinein").exists()
    with pytest.raises(dienst.KeinZugriff):
        akte.meilenstein_anlegen(
            dienst.Akteur.aus_bindung(bindung, projekt=projekt), projekt_b, name="Hinein"
        )


def test_meilenstein_steht_im_ueberblick(owner_client, akteur, projekt):
    akte.meilenstein_anlegen(akteur, projekt, name="Nur im Überblick")
    url = reverse("projekt_detail", kwargs={"kennung": "synth-a"})
    assert "Nur im Überblick" in owner_client.get(url).content.decode()


# --- Zuordnung beim Anlegen und Planen -------------------------------------------------------


def test_anlegen_nimmt_meilenstein_des_eigenen_projekts(
    client, akteur, projekt, bindung, pc_zugang
):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Erste Version")
    # Der Chat des Projekts wie der Assistent, jeweils mit und ohne „M-“.
    for schluessel, zusatz, wert, titel in (
        (bindung.klartext, {}, m.nummer, "Vom Projektchat"),
        (pc_zugang.klartext, {"projekt": "synth-a"}, str(m.pk), "Vom Assistenten"),
    ):
        fehler, daten = _werkzeug(
            client,
            schluessel,
            "kipm_auftrag_anlegen",
            titel=titel,
            text="Beschreibung",
            meilenstein=wert,
            **zusatz,
        )
        assert not fehler, daten
        assert daten["auftrag"]["meilenstein"] == {"nummer": m.nummer, "name": "Erste Version"}
    assert m.auftraege.count() == 2
    ereignis = Ereignis.objects.get(objekt_kennung=m.auftraege.first().nummer)
    assert m.nummer in ereignis.anlass


def test_anlegen_weist_fremden_meilenstein_ab(
    client, akteur, projekt, projekt_b, pc_zugang, bindung
):
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    for schluessel, zusatz in (
        (bindung.klartext, {}),
        (pc_zugang.klartext, {"projekt": "synth-a"}),
    ):
        fehler, daten = _werkzeug(
            client,
            schluessel,
            "kipm_auftrag_anlegen",
            titel="Mit fremdem Meilenstein",
            text="x",
            meilenstein=fremd.nummer,
            **zusatz,
        )
        assert fehler and daten["fehler"] == (
            f"Meilenstein „{fremd.nummer}“ gibt es in diesem Projekt nicht."
        )
    assert not Auftrag.objects.filter(titel="Mit fremdem Meilenstein").exists()


@pytest.mark.parametrize("wert", ["M-999999", "M-x", "²", "M-", " "])
def test_unbekannter_meilenstein_abgewiesen(client, bindung, wert):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_auftrag_anlegen", titel="T", text="x", meilenstein=wert
    )
    assert fehler and "gibt es in diesem Projekt nicht" in daten["fehler"]
    assert not Auftrag.objects.exists()


def test_dienst_weist_fremden_meilenstein_ab(akteur, projekt, projekt_b):
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    with pytest.raises(dienst.DienstFehler, match="gehört nicht"):
        dienst.wunsch_erfassen(akteur, projekt, titel="T", beschreibung="", meilenstein=fremd)
    with pytest.raises(dienst.DienstFehler, match="gehört nicht"):
        dienst.auftrag_anlegen(akteur, projekt, titel="T", meilenstein=fremd)
    assert not Auftrag.objects.exists()
    auftrag = dienst.wunsch_erfassen(akteur, projekt, titel="T", beschreibung="")
    with pytest.raises(dienst.DienstFehler, match="gehört nicht"):
        dienst.meilenstein_zuordnen(akteur, auftrag, fremd)
    auftrag.refresh_from_db()
    assert auftrag.meilenstein is None


def test_planen_ordnet_zu_und_loest(client, akteur, projekt, pc_zugang):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Erste Version")
    a = arbeitspaket(akteur, projekt)
    schluessel = pc_zugang.klartext
    fehler, daten = _werkzeug(
        client, schluessel, "kipm_planen", auftrag=a.nummer, meilenstein=m.nummer
    )
    assert not fehler, daten
    assert daten["auftrag"]["meilenstein"]["nummer"] == m.nummer
    a.refresh_from_db()
    assert a.meilenstein == m
    assert Ereignis.objects.filter(
        objekt_kennung=a.nummer, ereignis=f"Meilenstein {m.nummer} zugeordnet"
    ).exists()

    # Mit weiteren Feldern entsteht wie gewohnt eine neue Revision.
    fehler, daten = _werkzeug(client, schluessel, "kipm_planen", auftrag=a.nummer, ziel="Neu")
    assert not fehler and daten["auftrag"]["meilenstein"]["nummer"] == m.nummer

    fehler, daten = _werkzeug(client, schluessel, "kipm_planen", auftrag=a.nummer, meilenstein="")
    assert not fehler and daten["auftrag"]["meilenstein"] is None
    assert Ereignis.objects.filter(objekt_kennung=a.nummer, ereignis="Meilenstein gelöst").exists()


def test_planen_weist_fremden_meilenstein_ab(client, akteur, projekt, projekt_b, pc_zugang):
    eigen = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    a = arbeitspaket(akteur, projekt)
    _zuordnen(akteur, a, eigen)
    revision = a.revision_nr
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        auftrag=a.nummer,
        meilenstein=fremd.nummer,
        ziel="Darf nicht ankommen",
    )
    assert fehler and "gibt es in diesem Projekt nicht" in daten["fehler"]
    a.refresh_from_db()
    assert a.meilenstein == eigen and a.revision_nr == revision
    assert a.aktuelle_revision.ziel != "Darf nicht ankommen"


def test_planen_scheitert_ganz_wenn_freigabe_scheitert(client, akteur, projekt, pc_zugang):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Unfertig", beschreibung="")
    fehler, _ = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_planen",
        auftrag=a.nummer,
        meilenstein=m.nummer,
        freigeben=True,
    )
    assert fehler
    a.refresh_from_db()
    assert a.meilenstein is None and a.zustand == Auftrag.ENTWURF


def test_nur_meilenstein_laesst_revision_und_freigabe_unberuehrt(
    client, akteur, projekt, freigegeben, pc_zugang
):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    revision = freigegeben.revision_nr
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_planen", auftrag=freigegeben.nummer, meilenstein=m.nummer
    )
    assert not fehler, daten
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.FREIGEGEBEN and freigegeben.revision_nr == revision

    dienst.auftrag_uebernehmen(_chat(akteur, freigegeben), freigegeben, revision)
    fehler, _ = _werkzeug(
        client, pc_zugang.klartext, "kipm_planen", auftrag=freigegeben.nummer, meilenstein=""
    )
    assert not fehler
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT and freigegeben.meilenstein is None


def test_erledigter_auftrag_laesst_sich_zuordnen_ohne_neue_erledigtzeit(
    client, akteur, projekt, pc_zugang
):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    a = arbeitspaket(akteur, projekt)
    _erledigen(akteur, a)
    zeit = a.aktualisiert_am
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_planen", auftrag=a.nummer, meilenstein=m.nummer
    )
    assert not fehler, daten
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT and a.meilenstein == m and a.aktualisiert_am == zeit


def test_projektschluessel_plant_keine_zuordnung(client, akteur, projekt, bindung):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    a = arbeitspaket(akteur, projekt)
    fehler, _ = _werkzeug(
        client, bindung.klartext, "kipm_planen", auftrag=a.nummer, meilenstein=m.nummer
    )
    assert fehler
    a.refresh_from_db()
    assert a.meilenstein is None


def test_gleiche_zuordnung_schreibt_nichts(akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    a = dienst.wunsch_erfassen(akteur, projekt, titel="T", beschreibung="", meilenstein=m)
    vorher = Ereignis.objects.count()
    dienst.meilenstein_zuordnen(akteur, a, m)
    assert Ereignis.objects.count() == vorher


# --- Zählung und Anzeige ---------------------------------------------------------------------


def test_zaehlung_abgebrochene_zaehlen_nicht(owner_client, akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Erste Version")
    leer = akte.meilenstein_anlegen(akteur, projekt, name="Später")
    a1 = _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="Eins", umfang="a/**"), m)
    a2 = _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="Zwei", umfang="b/**"), m)
    _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="Offen", umfang="c/**"), m)
    weg = _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="Weg", umfang="d/**"), m)
    dienst.wunsch_erfassen(
        akteur, projekt, titel="Issue", beschreibung="", typ=Auftrag.TYP_ISSUE, meilenstein=m
    )
    arbeitspaket(akteur, projekt, titel="Ohne Meilenstein", umfang="e/**")
    _erledigen(akteur, a1)
    _erledigen(akteur, a2)
    dienst.auftrag_abbrechen(akteur, weg, "Nicht mehr nötig.")

    zahlen = {
        x.name: (x.erledigt_zahl, x.gesamt_zahl) for x in akte.meilensteine_des_projekts(projekt)
    }
    assert zahlen == {"Erste Version": (2, 4), "Später": (0, 0)}
    seite = _ueberblick(owner_client, projekt)
    assert "2 von 4 erledigt" in seite and "noch keine Aufträge" in seite
    assert "%" not in seite.split('id="meilensteine"')[1].split("</section>")[0]
    assert leer.auftraege.count() == 0


def test_zaehlung_folgt_dem_zustand(owner_client, akteur, projekt):
    """Blockiert zählt als offen; nur Erledigt zählt als erledigt."""
    m = akte.meilenstein_anlegen(akteur, projekt, name="M")
    a = _zuordnen(akteur, arbeitspaket(akteur, projekt), m)
    assert "0 von 1 erledigt" in _ueberblick(owner_client, projekt)
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    chat = _chat(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.rueckfrage_melden(chat, a, a.revision_nr, "Frage?")
    assert "0 von 1 erledigt" in _ueberblick(owner_client, projekt)
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT


def test_zaehlung_ueber_mcp(client, akteur, projekt, bindung):
    m = akte.meilenstein_anlegen(akteur, projekt, name="M")
    a = _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="A", umfang="a/**"), m)
    _zuordnen(akteur, arbeitspaket(akteur, projekt, titel="B", umfang="b/**"), m)
    _erledigen(akteur, a)
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_meilensteine")
    assert not fehler
    eintrag = daten["meilensteine"][0]
    assert (eintrag["erledigt"], eintrag["gesamt"], eintrag["stand"]) == (1, 2, "1 von 2 erledigt")


def test_reihenfolge_nach_datum_ohne_datum_zuletzt(akteur, projekt):
    for name, datum in (("Ohne", None), ("Spät", "2027-06-01"), ("Früh", "2026-12-01")):
        akte.meilenstein_anlegen(akteur, projekt, name=name, datum=datum)
    assert [m.name for m in akte.meilensteine_des_projekts(projekt)] == ["Früh", "Spät", "Ohne"]


def test_zaehlung_trennt_projekte(owner_client, akteur, projekt, projekt_b):
    akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Geheim beta")
    dienst.wunsch_erfassen(akteur, projekt_b, titel="B", beschreibung="", meilenstein=fremd)
    seite = _ueberblick(owner_client, projekt)
    assert "Eigen" in seite and "Geheim beta" not in seite
    text = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert "Eigen" in text and "Geheim beta" not in text


def test_auftragskopf_zeigt_meilenstein(owner_client, akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Erste <Version>")
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Mit", beschreibung="", meilenstein=m)
    ohne = dienst.wunsch_erfassen(akteur, projekt, titel="Ohne", beschreibung="")

    def kopf(auftrag):
        url = reverse("auftrag_detail", kwargs={"auftrag_id": auftrag.pk})
        return owner_client.get(url).content.decode().split("auftragskopf")[1].split("</header>")[0]

    assert "Meilenstein Erste &lt;Version&gt;" in kopf(a) and "kein Meilenstein" not in kopf(a)
    assert "kein Meilenstein" in kopf(ohne)


def test_ueberblick_maskiert_den_namen(owner_client, akteur, projekt):
    akte.meilenstein_anlegen(akteur, projekt, name="<script>x</script>")
    seite = _ueberblick(owner_client, projekt)
    assert "<script>x</script>" not in seite and "&lt;script&gt;x" in seite


def test_kontext_nennt_meilensteine_mit_stand(akteur, projekt):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Erste Version", datum="2026-12-01")
    a = _zuordnen(akteur, arbeitspaket(akteur, projekt), m)
    text = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert f"{m.nummer} Erste Version bis 01.12.2026: 0 von 1 erledigt" in text
    assert f"{a.nummer} " in text and f"Meilenstein {m.nummer}" in text


# --- Von Hand zuordnen beim Planen -----------------------------------------------------------


def _planformular(a) -> dict:
    rev = a.aktuelle_revision
    return {
        "titel": a.titel,
        "prioritaet": a.prioritaet,
        "beschreibung": rev.beschreibung,
        "ziel": rev.ziel,
        "nicht_ziele": rev.nicht_ziele,
        "schreibumfang": rev.schreibumfang,
        "abnahmekriterien": rev.abnahmekriterien,
    }


def test_planformular_ordnet_von_hand_zu(owner_client, akteur, projekt, projekt_b):
    m = akte.meilenstein_anlegen(akteur, projekt, name="Eigen")
    fremd = akte.meilenstein_anlegen(akteur, projekt_b, name="Fremd")
    a = arbeitspaket(akteur, projekt)
    url = reverse("auftrag_bearbeiten", kwargs={"auftrag_id": a.pk})
    seite = owner_client.get(url).content.decode()
    assert "Eigen" in seite and "Fremd" not in seite and "kein Meilenstein" in seite

    owner_client.post(url, {**_planformular(a), "meilenstein": fremd.pk})
    a.refresh_from_db()
    assert a.meilenstein is None

    owner_client.post(url, {**_planformular(a), "meilenstein": m.pk})
    a.refresh_from_db()
    assert a.meilenstein == m

    owner_client.post(url, {**_planformular(a), "meilenstein": ""})
    a.refresh_from_db()
    assert a.meilenstein is None


def test_planformular_ohne_meilensteine_ohne_feld(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    url = reverse("auftrag_bearbeiten", kwargs={"auftrag_id": a.pk})
    assert 'name="meilenstein"' not in owner_client.get(url).content.decode()

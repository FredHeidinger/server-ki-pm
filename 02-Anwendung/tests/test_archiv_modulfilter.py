"""A-216: Projekte archivieren und den Sitzungsstart nach dem Modul des Arbeitsorts filtern."""

import pytest
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Auftrag, Ereignis
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug
from tests.test_sitzungsstart import ANTWORTFELDER, _frei, _start

pytestmark = pytest.mark.django_db


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def _archivieren(client, projekt, aktiv="0"):
    return client.post(
        reverse("projekt_archivieren", kwargs={"kennung": projekt.kennung}), {"aktiv": aktiv}
    )


def _hauptordner(akteur, projekt):
    return dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="haupt", name="Haupt", pfad=r"C:\Synth\Haupt"
    )


# --- Archivieren -----------------------------------------------------------------------------


def test_archivieren_blendet_aus_und_zurueckholen_stellt_her(
    owner_client, akteur, projekt, pc_zugang
):
    _hauptordner(akteur, projekt)
    eintrag = _frei(akteur, projekt, "bleibt")
    vorher = Auftrag.objects.values().get(pk=eintrag.pk)
    start = reverse("projekte")
    karte = f'projektwahl-name">{projekt.name}'
    assert karte in owner_client.get(start).content.decode()

    antwort = _archivieren(owner_client, projekt)
    assert antwort.status_code == 302 and antwort["Location"] == start

    seite = owner_client.get(start).content.decode()
    assert karte not in seite and "Archiv: 1 Projekt" in seite
    assert projekt.name in owner_client.get(reverse("projekt_archiv")).content.decode()
    # Chats und Sitzungsstart sehen es nicht, gelöscht ist nichts.
    assert (
        _start(pc_zugang.klartext, **{"X-KIPM-Projekt": projekt.kennung}).json()["projekt"] is None
    )
    assert (
        _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"C:\Synth\Haupt"}).json()["projekt"] is None
    )
    assert Auftrag.objects.values().get(pk=eintrag.pk) == vorher
    assert (
        "archiviert"
        in owner_client.get(
            reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
        ).content.decode()
    )

    assert _archivieren(owner_client, projekt, aktiv="1").status_code == 302
    assert karte in owner_client.get(start).content.decode()
    assert "Archiv:" not in owner_client.get(start).content.decode()
    assert Auftrag.objects.values().get(pk=eintrag.pk) == vorher
    assert set(
        Ereignis.objects.filter(projekt=projekt, objekt_typ="Projekt").values_list(
            "ereignis", flat=True
        )
    ) >= {"Projekt archiviert", "Projekt zurückgeholt"}


def test_eintrag_in_arbeit_verhindert_das_archivieren(owner_client, akteur, projekt, bindung):
    laufend = _frei(akteur, projekt, "laufend")
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), laufend, laufend.revision_nr)

    seite = owner_client.get(
        reverse("projekt_einstellungen", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert "Archivieren – nicht möglich" in seite and laufend.nummer in seite

    _archivieren(owner_client, projekt)
    projekt.refresh_from_db()
    assert projekt.aktiv is True


def test_nur_der_owner_oder_sein_assistent(client, zweiter_mensch, projekt, bindung):
    client.force_login(zweiter_mensch)
    _archivieren(client, projekt)
    projekt.refresh_from_db()
    assert projekt.aktiv is True
    seite = client.get(
        reverse("projekt_einstellungen", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert "Archivieren – nicht möglich" in seite and "nur der Owner" in seite

    client.logout()
    fehler, _ = _werkzeug(client, bindung.klartext, "kipm_projekt_archivieren")
    projekt.refresh_from_db()
    assert fehler and projekt.aktiv is True


def test_assistent_archiviert_nur_mit_freds_worten(client, projekt, pc_zugang):
    fehler, _ = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_projekt_archivieren",
        projekt=projekt.kennung,
        fred_sagte=None,
    )
    projekt.refresh_from_db()
    assert fehler and projekt.aktiv is True

    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_projekt_archivieren", projekt=projekt.kennung
    )
    projekt.refresh_from_db()
    assert not fehler and daten["aktiv"] is False and projekt.aktiv is False
    fehler, daten = _werkzeug(client, pc_zugang.klartext, "kipm_projekte", fred_sagte=None)
    assert not fehler and projekt.kennung not in [p["kennung"] for p in daten["projekte"]]


def test_archiv_sperrt_uebernahme_und_gesamtliste(client, akteur, projekt, projekt_b, pc_zugang):
    frei = _frei(akteur, projekt, "frei")
    anderer = _frei(akteur, projekt_b, "anderer")
    dienst.projekt_archivieren(akteur, projekt)

    fehler, daten = _werkzeug(client, pc_zugang.klartext, "kipm_auftraege", fred_sagte=None)
    assert not fehler
    assert [a["nummer"] for a in daten["auftraege"]] == [anderer.nummer]

    with pytest.raises(dienst.Konflikt):
        dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(pc_zugang), frei, frei.revision_nr)
    frei.refresh_from_db()
    assert frei.zustand == Auftrag.FREIGEGEBEN and not frei.hat_bearbeiter


# --- Modulfilter -----------------------------------------------------------------------------


@pytest.fixture
def modulprojekt(akteur, projekt):
    """Kern mit Untermodul, ein zweites Modul, dazu ein Eintrag je Modul und einer ohne."""
    kern = projekt.module.get(kennung="kern")
    unter = dienst.modul_anlegen(akteur, projekt, kennung="unter", name="Unter", eltern=kern)
    anderes = dienst.modul_anlegen(akteur, projekt, kennung="anderes", name="Anderes")
    eintraege = {n: _frei(akteur, projekt, n) for n in ("im-kern", "im-unter", "fremd", "ohne")}
    Auftrag.objects.filter(pk=eintraege["im-kern"].pk).update(modul=kern)
    Auftrag.objects.filter(pk=eintraege["im-unter"].pk).update(modul=unter)
    Auftrag.objects.filter(pk=eintraege["fremd"].pk).update(modul=anderes, prioritaet="hoch")
    Auftrag.objects.filter(pk=eintraege["ohne"].pk).update(modul=None)
    dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="kern-ort", name="Kernordner", pfad=r"C:\Synth\Kern", modul=kern
    )
    _hauptordner(akteur, projekt)
    return eintraege


def test_ordner_eines_moduls_zeigt_nur_dieses_samt_untermodulen(pc_zugang, modulprojekt):
    d = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"C:\Synth\Kern\src"}).json()
    assert set(d) == ANTWORTFELDER and d["modul"] == "kern"
    assert {e["titel"] for e in d["offen"]} == {"im-kern", "im-unter"}
    assert {e["modul"] for e in d["offen"]} == {"Kern", "Unter"}
    # „fremd“ steht auf hoch und wäre sonst dran: „weiter“ bleibt im Modul.
    assert d["naechster"]["titel"] == "im-kern"
    assert d["ausgeblendet"] == 2


def test_hauptordner_bleibt_wie_bisher(pc_zugang, modulprojekt):
    d = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"C:\Synth\Haupt"}).json()
    assert d["modul"] is None and d["ausgeblendet"] == 0
    assert {e["titel"] for e in d["offen"]} == {"im-kern", "im-unter", "fremd", "ohne"}
    assert d["naechster"]["titel"] == "fremd"


def test_wartet_filtert_auftraege_aber_nicht_entscheidungen(
    akteur, projekt, pc_zugang, modulprojekt
):
    kern = projekt.module.get(kennung="kern")
    anderes = projekt.module.get(kennung="anderes")
    imkern = arbeitspaket(akteur, projekt, titel="Entwurf im Kern", umfang="a/**")
    fremd = arbeitspaket(akteur, projekt, titel="Entwurf fremd", umfang="b/**")
    Auftrag.objects.filter(pk=imkern.pk).update(modul=kern)
    Auftrag.objects.filter(pk=fremd.pk).update(modul=anderes)
    frage = akte.entscheidung_fragen(akteur, projekt, frage="Frage?", warum="Warum")

    d = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"C:\Synth\Kern"}).json()
    nummern = {w["nummer"] for w in d["wartet"]}
    assert {imkern.nummer, frage.nummer} <= nummern and fremd.nummer not in nummern


def test_archiviertes_modul_filtert_nicht(akteur, projekt, pc_zugang, modulprojekt):
    ort = projekt.arbeitsorte.get(kennung="kern-ort")
    unter = projekt.module.get(kennung="unter")
    dienst.modul_archivieren(akteur, unter, aktiv=False, erwartete_revision=unter.revision)
    kern = projekt.module.get(kennung="kern")
    dienst.modul_archivieren(akteur, kern, aktiv=False, erwartete_revision=kern.revision)
    ort.refresh_from_db()
    assert ort.modul == kern

    d = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"C:\Synth\Kern"}).json()
    assert d["modul"] is None and d["ausgeblendet"] == 0 and len(d["offen"]) == 4


def test_fremdes_oder_archiviertes_modul_wird_abgewiesen(akteur, projekt, projekt_b):
    fremd = dienst.modul_anlegen(akteur, projekt_b, kennung="fremd", name="Fremd")
    with pytest.raises(dienst.DienstFehler):
        dienst.arbeitsort_anlegen(akteur, projekt, kennung="x", name="X", modul=fremd)
    ort = projekt.arbeitsorte.first()
    with pytest.raises(dienst.DienstFehler):
        dienst.arbeitsort_aendern(akteur, ort, erwartete_revision=ort.revision, modul=fremd)
    alt = dienst.modul_anlegen(akteur, projekt, kennung="alt", name="Alt")
    dienst.modul_archivieren(akteur, alt, aktiv=False, erwartete_revision=alt.revision)
    alt.refresh_from_db()
    with pytest.raises(dienst.DienstFehler):
        dienst.arbeitsort_aendern(akteur, ort, erwartete_revision=ort.revision, modul=alt)


def test_modul_wird_in_den_einstellungen_gesetzt_und_geloest(owner_client, projekt):
    ort = projekt.arbeitsorte.first()
    kern = projekt.module.get(kennung="kern")
    url = reverse("projekt_einstellungen", kwargs={"kennung": projekt.kennung})
    assert f'name="ort{ort.pk}-modul"' in owner_client.get(url).content.decode()

    pflegen = reverse(
        "arbeitsort_pflegen", kwargs={"kennung": projekt.kennung, "pk": ort.pk, "wahl": "aendern"}
    )
    felder = {
        f"ort{ort.pk}-name": ort.name,
        "revision": ort.revision,
        f"ort{ort.pk}-modul": kern.pk,
    }
    owner_client.post(pflegen, felder)
    ort.refresh_from_db()
    assert ort.modul == kern
    assert "Modul Kern" in owner_client.get(url).content.decode()

    felder.update({"revision": ort.revision, f"ort{ort.pk}-modul": ""})
    owner_client.post(pflegen, felder)
    ort.refresh_from_db()
    assert ort.modul is None


def test_ohne_module_gibt_es_kein_modulfeld(owner_client, projekt_b):
    url = reverse("projekt_einstellungen", kwargs={"kennung": projekt_b.kennung})
    seite = owner_client.get(url).content.decode()
    ort = projekt_b.arbeitsorte.first()
    assert f'name="ort{ort.pk}-modul"' not in seite and 'name="ort-modul"' not in seite
    # Speichern ohne das Feld geht weiter.
    pflegen = reverse(
        "arbeitsort_pflegen", kwargs={"kennung": projekt_b.kennung, "pk": ort.pk, "wahl": "aendern"}
    )
    owner_client.post(pflegen, {f"ort{ort.pk}-name": "Neu", "revision": ort.revision})
    ort.refresh_from_db()
    assert ort.name == "Neu"


def test_chat_setzt_modul_ohne_die_bestaetigung_zu_verlieren(client, akteur, projekt, pc_zugang):
    ort = dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="ort-a", name="Ort A", pfad=r"C:\Synth\OrtA"
    )
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_arbeitsort_aendern",
        projekt=projekt.kennung,
        arbeitsort=ort.kennung,
        revision=ort.revision,
        modul="kern",
    )
    assert not fehler and daten["arbeitsort"]["modul"] == "kern"
    ort.refresh_from_db()
    assert ort.bestaetigt is True and ort.modul.kennung == "kern"

    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_arbeitsort_aendern",
        projekt=projekt.kennung,
        arbeitsort=ort.kennung,
        revision=ort.revision,
        modul="",
    )
    assert not fehler and daten["arbeitsort"]["modul"] is None

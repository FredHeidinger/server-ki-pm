"""Der Stand bleibt sichtbar, wie auch immer ein Chat endet: verstummte Aufträge und Ende-Vermerk."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Auftrag, Ereignis, Meldung
from tests.conftest import arbeitspaket
from tests.test_klickwege import _projekt_url

pytestmark = pytest.mark.django_db

STAND = "Fachkern fertig, offen: Bericht, nächster Schritt: Gegenprüfung."


@pytest.fixture
def chat(bindung):
    return dienst.Akteur.aus_bindung(bindung)


@pytest.fixture
def laufend(akteur, projekt, chat):
    a = arbeitspaket(akteur, projekt, titel="Läuft", umfang="laeuft/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    a.refresh_from_db()
    return a


def _altern(a, stunden):
    """Übernahme und letzte Meldung liegen ``stunden`` zurück."""
    vorher = timezone.now() - timedelta(hours=stunden)
    a.meldungen.all().update(zeitpunkt=vorher)
    Auftrag.objects.filter(pk=a.pk).update(
        uebernommen_am=vorher, zuletzt_gemeldet_am=vorher if a.zuletzt_gemeldet_am else None
    )
    a.refresh_from_db()


def _ende(client, schluessel, a, kennung="ende-1"):
    return client.post(
        reverse("api_sitzung_beendet", kwargs={"auftrag_id": a.pk}),
        data={"anfrage_kennung": kennung},
        content_type="application/json",
        headers={"Authorization": f"Bearer {schluessel}"},
    )


# --- Wann ein Auftrag als verstummt gilt ------------------------------------------------------


def test_frisch_uebernommen_ist_nicht_verstummt(laufend, projekt):
    assert dienst.verstummt(laufend) is None
    assert laufend not in dienst.wartet_auf_dich(projekt)


def test_frist_knapp_nicht_erreicht(laufend, chat, projekt):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN - 0.1)
    assert dienst.verstummt(laufend) is None


def test_frist_ueberschritten_zeigt_letzten_stand(laufend, chat, projekt):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 0.1)
    v = dienst.verstummt(laufend)
    assert v["stand"].text == STAND and not v["beendet"]
    assert laufend in dienst.wartet_auf_dich(projekt)


def test_noch_nie_gemeldet_zaehlt_ab_uebernahme(laufend, projekt):
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    v = dienst.verstummt(laufend)
    assert v["stand"] is None and laufend in dienst.wartet_auf_dich(projekt)


def test_neue_meldung_beendet_das_schweigen(laufend, chat, projekt):
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, "Weiter.")
    laufend.refresh_from_db()
    assert dienst.verstummt(laufend) is None
    assert laufend not in dienst.wartet_auf_dich(projekt)


def test_nur_in_arbeit_gilt(laufend, chat, projekt):
    dienst.rueckfrage_melden(chat, laufend, laufend.revision_nr, "Welche Sprache?")
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    assert laufend.zustand == Auftrag.BLOCKIERT and dienst.verstummt(laufend) is None


def test_antwort_gilt_nicht_als_stand_und_nicht_als_lebenszeichen(laufend, chat, akteur):
    dienst.rueckfrage_melden(chat, laufend, laufend.revision_nr, "Welche Sprache?")
    dienst.rueckfrage_beantworten(akteur, laufend, "Deutsch")
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    v = dienst.verstummt(laufend)
    assert v["stand"].text == "Welche Sprache?"


def test_steht_nach_blockierten_und_vor_entwuerfen(akteur, projekt, chat, laufend):
    blockiert = arbeitspaket(akteur, projekt, titel="Blockiert", umfang="block/**")
    dienst.auftrag_freigeben(akteur, blockiert)
    blockiert.refresh_from_db()
    dienst.auftrag_uebernehmen(chat, blockiert, blockiert.revision_nr)
    dienst.rueckfrage_melden(chat, blockiert, blockiert.revision_nr, "Frage?")
    entwurf = arbeitspaket(akteur, projekt, titel="Entwurf", umfang="entwurf/**")
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    assert dienst.wartet_auf_dich(projekt) == [blockiert, laufend, entwurf]


# --- Vermerk beim Schließen -------------------------------------------------------------------


def test_ende_zeigt_sofort_und_laesst_den_stand_stehen(client, laufend, chat, bindung, projekt):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    laufend.refresh_from_db()
    gemeldet = laufend.zuletzt_gemeldet_am
    antwort = _ende(client, bindung.klartext, laufend)
    assert antwort.status_code == 200 and antwort.json()["vermerkt"] is True
    laufend.refresh_from_db()
    assert laufend.zustand == Auftrag.IN_ARBEIT and laufend.zuletzt_gemeldet_am == gemeldet
    v = dienst.verstummt(laufend)
    assert v["beendet"] and v["stand"].text == STAND
    assert laufend in dienst.wartet_auf_dich(projekt)
    assert Ereignis.objects.filter(ereignis="Chat beendet, Ergebnis fehlt").count() == 1


def test_ende_zweimal_ergibt_einen_vermerk(client, laufend, bindung):
    _ende(client, bindung.klartext, laufend, "ende-1")
    antwort = _ende(client, bindung.klartext, laufend, "ende-2")
    assert antwort.json()["vermerkt"] is False
    assert laufend.meldungen.filter(art=Meldung.SITZUNGSENDE).count() == 1


def test_meldung_nach_dem_ende_hebt_es_auf(client, laufend, chat, bindung):
    _ende(client, bindung.klartext, laufend)
    laufend.refresh_from_db()
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, "Weiter im neuen Chat.")
    laufend.refresh_from_db()
    assert dienst.verstummt(laufend) is None


def test_ende_nach_ergebnis_vermerkt_nichts(client, laufend, chat, bindung):
    dienst.ergebnis_einreichen(
        chat,
        laufend,
        laufend.revision_nr,
        zusammenfassung="Fertig",
        dateien="a.py@abc1234",
        pruefungen="pytest: bestanden",
    )
    antwort = _ende(client, bindung.klartext, laufend)
    assert antwort.status_code == 200 and antwort.json()["vermerkt"] is False
    assert not laufend.meldungen.filter(art=Meldung.SITZUNGSENDE).exists()


def test_fremder_zugang_vermerkt_nichts(client, laufend, akteur):
    _, fremd = dienst.bindung_anlegen(akteur, None, name="Anderes Programm")
    antwort = _ende(client, fremd, laufend)
    assert antwort.status_code == 200 and antwort.json()["vermerkt"] is False
    assert not laufend.meldungen.filter(art=Meldung.SITZUNGSENDE).exists()


def test_pc_zugang_ohne_ordner_vermerkt(client, akteur, projekt):
    """So ruft der Hook: PC-Zugang, ohne Projekt- oder Ordnerangabe."""
    pc, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    a = arbeitspaket(akteur, projekt, titel="Am PC", umfang="pc/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(pc, projekt=projekt), a, a.revision_nr)
    antwort = _ende(client, schluessel, a)
    assert antwort.status_code == 200 and antwort.json()["vermerkt"] is True
    a.refresh_from_db()
    assert dienst.verstummt(a)["beendet"]


def test_anderer_chat_desselben_zugangs_meldet_weiter(akteur, projekt):
    """Ohne Übergabeknopf: Der neue Chat meldet einfach; der Zugang ist derselbe."""
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    alter_chat = dienst.Akteur.aus_bindung(pc, projekt=projekt)
    a = arbeitspaket(akteur, projekt, titel="Am PC", umfang="pc/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    dienst.auftrag_uebernehmen(alter_chat, a, a.revision_nr)
    a.refresh_from_db()
    dienst.sitzungsende_melden(alter_chat, a)
    neuer_chat = dienst.Akteur.aus_bindung(pc, projekt=projekt)
    meldung = dienst.fortschritt_melden(neuer_chat, a, a.revision_nr, "Weiter im neuen Chat.")
    a.refresh_from_db()
    assert not meldung.historisch and dienst.verstummt(a) is None


def test_frische_antwort_startet_die_frist_neu(laufend, chat, akteur, projekt):
    """Die Wartezeit lag bei Fred; direkt nach seiner Antwort gilt der Chat nicht als stumm."""
    dienst.rueckfrage_melden(chat, laufend, laufend.revision_nr, "Welche Sprache?")
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 2)
    dienst.rueckfrage_beantworten(akteur, laufend, "Deutsch")
    laufend.refresh_from_db()
    assert laufend.zustand == Auftrag.IN_ARBEIT and dienst.verstummt(laufend) is None
    assert laufend not in dienst.wartet_auf_dich(projekt)


def test_mit_termin_fehlt_der_stand_nicht(owner_client, laufend, chat, projekt, bindung):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    Auftrag.objects.filter(pk=laufend.pk).update(faellig=timezone.localdate())
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    eintrag = next(
        w for w in c.get("/api/sitzungsstart/").json()["wartet"] if w["nummer"] == laufend.nummer
    )
    assert eintrag["art"] == "still" and STAND in eintrag["text"] and eintrag["faellig"]
    seite = owner_client.get(_projekt_url(projekt)).content.decode("utf-8")
    assert "Letzter Stand" in seite and STAND in seite
    assert "Der Termin ist da" in seite and f"Mach bei {laufend.nummer} weiter" in seite


def test_mit_termin_ohne_schweigen_bleibt_es_faellig(laufend, bindung):
    Auftrag.objects.filter(pk=laufend.pk).update(faellig=timezone.localdate())
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    eintrag = next(
        w for w in c.get("/api/sitzungsstart/").json()["wartet"] if w["nummer"] == laufend.nummer
    )
    assert eintrag["art"] == "faellig" and eintrag["text"] == ""


def test_vermerk_nach_neuer_uebernahme(client, laufend, akteur, chat, bindung):
    _ende(client, bindung.klartext, laufend, "ende-1")
    dienst.bearbeitung_unklar(akteur, laufend, "Chat weg")
    laufend.refresh_from_db()
    dienst.auftrag_freigeben(akteur, laufend)
    laufend.refresh_from_db()
    dienst.auftrag_uebernehmen(chat, laufend, laufend.revision_nr)
    laufend.refresh_from_db()
    assert dienst.verstummt(laufend) is None  # der alte Vermerk zählt nicht mehr
    antwort = _ende(client, bindung.klartext, laufend, "ende-2")
    assert antwort.json()["vermerkt"] is True
    laufend.refresh_from_db()
    assert dienst.verstummt(laufend)["beendet"]


def test_fred_selbst_bearbeitet_gilt_nicht_als_verstummt(akteur, projekt):
    a = arbeitspaket(akteur, projekt, titel="Mache ich selbst", umfang="selbst/**")
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    dienst.auftrag_uebernehmen(akteur, a, a.revision_nr)
    a.refresh_from_db()
    _altern(a, dienst.STILL_STUNDEN + 1)
    assert a.zustand == Auftrag.IN_ARBEIT and dienst.verstummt(a) is None
    assert a not in dienst.wartet_auf_dich(projekt)


def test_blockiert_geschlossen_dann_beantwortet_zeigt_sofort_beendet(
    client, laufend, chat, akteur, bindung, projekt
):
    dienst.rueckfrage_melden(chat, laufend, laufend.revision_nr, "Welche Sprache?")
    laufend.refresh_from_db()
    antwort = _ende(client, bindung.klartext, laufend)
    assert antwort.json()["vermerkt"] is True
    laufend.refresh_from_db()
    assert laufend.zustand == Auftrag.BLOCKIERT and dienst.verstummt(laufend) is None
    dienst.rueckfrage_beantworten(akteur, laufend, "Deutsch")
    laufend.refresh_from_db()
    v = dienst.verstummt(laufend)
    assert laufend.zustand == Auftrag.IN_ARBEIT and v["beendet"]
    assert v["stand"].text == "Welche Sprache?"
    assert laufend in dienst.wartet_auf_dich(projekt)


def test_ende_ist_kein_mcp_werkzeug():
    from steuerung import mcp

    assert not any("beendet" in w[0] or "sitzung" in w[0] for w in mcp.WERKZEUGE)


# --- Was Fred und der nächste Chat sehen ------------------------------------------------------


def test_sitzungsstart_nennt_letzten_stand(laufend, chat, projekt, bindung):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    eintrag = next(
        w for w in c.get("/api/sitzungsstart/").json()["wartet"] if w["nummer"] == laufend.nummer
    )
    assert eintrag["art"] == "still"
    assert "keine Meldung" in eintrag["text"] and STAND in eintrag["text"]


def test_sitzungsstart_entfernt_geheimnisse_aus_dem_stand(laufend, chat, bindung):
    geheim = "kipm_" + "0" * 12 + "_" + "a" * 20
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, f"Schlüssel {geheim} gesetzt")
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    assert geheim not in c.get("/api/sitzungsstart/").content.decode("utf-8")


def test_seite_zeigt_stand_ohne_knopf(owner_client, laufend, chat, projekt):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, STAND)
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    seite = owner_client.get(_projekt_url(projekt)).content.decode("utf-8")
    assert "Keine Meldung" in seite and "Letzter Stand" in seite and STAND in seite
    assert f"Mach bei {laufend.nummer} weiter" in seite
    block = seite.split('class="eintrag eintrag-still"')[1].split("</li>")[0]
    assert "<form" not in block and "<button" not in block


def test_seite_nach_ende(owner_client, client, laufend, bindung, projekt):
    _ende(client, bindung.klartext, laufend)
    seite = owner_client.get(_projekt_url(projekt)).content.decode("utf-8")
    assert "Chat beendet" in seite and "Der Chat hat noch nichts gemeldet" in seite


def test_stand_wird_in_der_vorlage_maskiert(owner_client, laufend, chat, projekt):
    dienst.fortschritt_melden(chat, laufend, laufend.revision_nr, "<b>fett</b>")
    laufend.refresh_from_db()
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    seite = owner_client.get(_projekt_url(projekt)).content.decode("utf-8")
    assert "<b>fett</b>" not in seite and "&lt;b&gt;fett&lt;/b&gt;" in seite


def test_wartet_liste_zaehlt_ihn(laufend, projekt):
    _altern(laufend, dienst.STILL_STUNDEN + 1)
    assert laufend in akte.wartet_liste(projekt)

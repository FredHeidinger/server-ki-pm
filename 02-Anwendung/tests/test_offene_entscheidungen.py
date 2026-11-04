"""A-13: Der Chat stellt eine offene Frage, Fred antwortet in „Wartet auf dich“."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Entscheidung, Ereignis
from tests.conftest import arbeitspaket
from tests.test_klickwege import _Klicker, _projekt_url
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db

FRAGE = "Soll die Sicherung nachts laufen?"


def _frage(akteur, projekt, frage=FRAGE, warum="Tagsüber stört sie."):
    return akte.entscheidung_fragen(akteur, projekt, frage=frage, warum=warum)


def _antwort_url(projekt, e):
    return reverse("entscheidung_antworten", kwargs={"kennung": projekt.kennung, "nummer": e.pk})


def _wartet(client, projekt) -> str:
    html = client.get(_projekt_url(projekt)).content.decode()
    return html.split('id="h-wartet"')[1].split("</section>")[0]


# --- Eintragen durch den Chat ----------------------------------------------------------------


def test_chat_stellt_frage_ohne_fred_sagte(client, projekt, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_fragen",
        frage=FRAGE,
        warum="W",
        fred_sagte=None,
    )
    assert not fehler, daten
    e = Entscheidung.objects.get(pk=daten["entscheidung"]["nummer"].removeprefix("E-"))
    assert e.zustand == Entscheidung.VORSCHLAG and e.entschieden_am is None
    assert daten["entscheidung"]["zustand"] == "VORSCHLAG" and daten["entscheidung"]["seit"] is None
    assert list(e.betroffene_projekte.all()) == [projekt]
    ereignis = Ereignis.objects.filter(objekt_kennung=e.nummer).order_by("-id").first()
    assert ereignis.ereignis == "Offene Entscheidung eingetragen" and ereignis.version is not None


def test_projektschluessel_nur_fuer_das_eigene_projekt(client, projekt, projekt_b, bindung):
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_entscheidung_fragen",
        projekt=projekt_b.kennung,
        frage=FRAGE,
        fred_sagte=None,
    )
    assert not fehler, daten
    e = Entscheidung.objects.get(pk=daten["entscheidung"]["nummer"].removeprefix("E-"))
    assert list(e.betroffene_projekte.all()) == [projekt]
    assert akte.offene_entscheidungen(projekt_b) == []


def test_pc_zugang_nennt_das_projekt(client, akteur, projekt_b):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="PC")
    fehler, daten = _werkzeug(
        client,
        schluessel,
        "kipm_entscheidung_fragen",
        projekt=projekt_b.kennung,
        frage=FRAGE,
        fred_sagte=None,
    )
    assert not fehler, daten
    assert [e.text for e in akte.offene_entscheidungen(projekt_b)] == [FRAGE]


def test_leere_frage_wird_abgewiesen(client, projekt, bindung):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_entscheidung_fragen", frage="   ", fred_sagte=None
    )
    assert fehler and "leer" in str(daten)
    assert not Entscheidung.objects.exists()


def test_frage_legt_nichts_fest(client, akteur, projekt, bindung):
    _frage(akteur, projekt)
    fehler, daten = _werkzeug(client, bindung.klartext, "kipm_entscheidungen", fred_sagte=None)
    assert not fehler and daten["entscheidungen"][0]["zustand"] == "VORSCHLAG"
    text = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert "nur Vorschlag" in text


def test_chat_kann_nicht_selbst_antworten(akteur, projekt, bindung):
    e = _frage(akteur, projekt)
    with pytest.raises(dienst.KeinZugriff):
        akte.entscheidung_beantworten(
            dienst.Akteur.aus_bindung(bindung), projekt, e, erwarteter_stand=0, antwort="Ja"
        )
    e.refresh_from_db()
    assert e.zustand == Entscheidung.VORSCHLAG


# --- Sichtbar im Überblick, im Wissen und auf der Startseite ---------------------------------


def test_steht_in_wartet_auf_dich_mit_frage_und_warum(owner_client, akteur, projekt):
    e = _frage(akteur, projekt)
    wartet = _wartet(owner_client, projekt)
    assert "Offene Entscheidung" in wartet and FRAGE in wartet and "Tagsüber stört sie." in wartet
    assert e.nummer in wartet and ">Antworten</button>" in wartet


def test_steht_im_wissen_mit_weg_zur_antwort(owner_client, akteur, projekt):
    _frage(akteur, projekt)
    html = owner_client.get(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))
    html = html.content.decode()
    assert FRAGE in html and "Tagsüber stört sie." in html and "nur Vorschlag" in html
    assert f'{_projekt_url(projekt)}#h-wartet">Antworten</a>' in html


def test_startseite_zaehlt_sie_mit(owner_client, akteur, projekt):
    _frage(akteur, projekt)
    html = owner_client.get(reverse("start")).content.decode()
    assert '<span class="wartet">1 wartet auf dich</span>' in html


def test_reihenfolge_faelliges_dann_fragen_dann_der_rest(owner_client, akteur, projekt):
    entwurf = arbeitspaket(akteur, projekt, titel="Geplanter Entwurf", umfang="x/**")
    jetzt = timezone.localdate() - datetime.timedelta(days=1)
    dienst.wunsch_erfassen(akteur, projekt, titel="Längst fällig", beschreibung="b", faellig=jetzt)
    _frage(akteur, projekt)
    wartet = _wartet(owner_client, projekt)
    assert wartet.index("Längst fällig") < wartet.index(FRAGE) < wartet.index(entwurf.titel)
    assert wartet.count('<li class="eintrag') == 3


def test_anderes_projekt_sieht_sie_nicht(owner_client, akteur, projekt, projekt_b):
    _frage(akteur, projekt)
    assert FRAGE not in _wartet(owner_client, projekt_b)


def test_text_wird_nicht_als_html_ausgegeben(owner_client, akteur, projekt):
    _frage(akteur, projekt, frage="<script>alert(1)</script>", warum="<b>fett</b>")
    wartet = _wartet(owner_client, projekt)
    assert "<script>" not in wartet and "<b>fett" not in wartet


# --- Antworten -------------------------------------------------------------------------------


def test_klickweg_antwort_in_zwei_klicks(owner_client, akteur, projekt):
    e = _frage(akteur, projekt)
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    f.knopf(_antwort_url(projekt, e), "Antworten", antwort="Ja, nachts.")
    e.refresh_from_db()
    assert e.zustand == Entscheidung.ENTSCHIEDEN and f.klicks == 2
    assert e.text == f"{FRAGE}\nAntwort: Ja, nachts."
    assert FRAGE not in f.html.split('id="h-wartet"')[1].split("</section>")[0]


def test_ohne_antworttext_gilt_der_text_wie_er_dasteht(owner_client, akteur, projekt):
    e = _frage(akteur, projekt, frage="Es gilt Version 2.")
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    f.knopf(_antwort_url(projekt, e), "Antworten")
    e.refresh_from_db()
    assert e.zustand == Entscheidung.ENTSCHIEDEN and e.text == "Es gilt Version 2."


def test_danach_entscheidung_mit_seit_verlauf_und_kontext(owner_client, owner, akteur, projekt):
    e = _frage(akteur, projekt)
    owner_client.post(_antwort_url(projekt, e), {"stand": 0, "antwort": "Ja"})
    e.refresh_from_db()
    assert e.entschieden_am is not None and e.entschieden_von == owner and e.stand == 1
    ereignis = Ereignis.objects.filter(objekt_kennung=e.nummer).order_by("-id").first()
    assert ereignis.ereignis == "Offene Entscheidung beantwortet" and ereignis.version is not None
    text = akte.kontext_erzeugen(akteur, projekt).vollstaendig_text
    assert "nur Vorschlag" not in text and "Antwort: Ja" in text
    assert f"{e.entschieden_am:%d.%m.%Y}" in text
    assert akte.offene_entscheidungen(projekt) == []
    wissen = owner_client.get(reverse("akte_wissen", kwargs={"kennung": projekt.kennung}))
    assert "nur Vorschlag" not in wissen.content.decode()


def test_zweite_antwort_wird_abgewiesen(owner_client, akteur, projekt):
    e = _frage(akteur, projekt)
    owner_client.post(_antwort_url(projekt, e), {"stand": 0, "antwort": "Ja"})
    antwort = owner_client.post(
        _antwort_url(projekt, e), {"stand": 1, "antwort": "Doch nicht"}, follow=True
    )
    assert "schon beantwortet" in antwort.content.decode()
    e.refresh_from_db()
    assert "Doch nicht" not in e.text


def test_veralteter_stand_wird_abgewiesen(owner_client, akteur, projekt):
    """Hat sich die Frage geändert, beantwortet Fred nicht versehentlich die falsche."""
    e = _frage(akteur, projekt)
    Entscheidung.objects.filter(pk=e.pk).update(stand=3, text="Andere Frage")
    owner_client.post(_antwort_url(projekt, e), {"stand": 0, "antwort": "Ja"})
    e.refresh_from_db()
    assert e.zustand == Entscheidung.VORSCHLAG


def test_antwort_im_fremden_projekt_gilt_als_nicht_vorhanden(
    owner_client, akteur, projekt, projekt_b
):
    e = _frage(akteur, projekt)
    antwort = owner_client.post(_antwort_url(projekt_b, e), {"stand": 0, "antwort": "Ja"})
    assert antwort.status_code == 404
    e.refresh_from_db()
    assert e.zustand == Entscheidung.VORSCHLAG


def test_antwort_nur_per_post_und_angemeldet(client, owner_client, akteur, projekt):
    e = _frage(akteur, projekt)
    assert owner_client.get(_antwort_url(projekt, e)).status_code == 405
    client.logout()
    assert client.post(_antwort_url(projekt, e), {"stand": 0}).status_code == 302
    e.refresh_from_db()
    assert e.zustand == Entscheidung.VORSCHLAG


def test_zurueckgezogene_frage_wartet_nicht_mehr(owner_client, akteur, projekt):
    e = _frage(akteur, projekt)
    akte.entscheidung_aendern(akteur, projekt, e, erwarteter_stand=0, zurueckziehen=True)
    assert FRAGE not in _wartet(owner_client, projekt)

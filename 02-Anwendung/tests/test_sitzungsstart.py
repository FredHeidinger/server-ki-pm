"""A-14: „weiter“-Reihenfolge, „Als Nächstes“ im Überblick, GET /api/sitzungsstart/."""

import datetime

import pytest
from django.apps import apps
from django.core import serializers
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Auftrag, ChatBindung, OrdnerAnfrage
from tests.conftest import arbeitspaket
from tests.test_klickwege import _blockiert, _projekt_url

pytestmark = pytest.mark.django_db

PFAD = "/api/sitzungsstart/"
ANTWORTFELDER = {
    "ok",
    "vertrag",
    "stand",
    "projekt",
    "modul",
    "naechster",
    "offen",
    "weitere",
    "ausgeblendet",
    "wartet",
}
EINTRAGSFELDER = {
    "nummer",
    "titel",
    "modul",
    "art",
    "zustand",
    "prioritaet",
    "faellig",
    "uebernehmbar",
    "bearbeiter",
}
WARTETFELDER = {"art", "nummer", "titel", "text", "faellig"}


def _frei(akteur, projekt, titel, prioritaet="mittel"):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel}/**")
    Auftrag.objects.filter(pk=a.pk).update(prioritaet=prioritaet)
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    return a


def _start(schluessel, **kopfzeilen):
    c = Client(headers={"Authorization": f"Bearer {schluessel}", **kopfzeilen})
    return c.get(PFAD)


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


# --- Die Regel für „weiter“ ------------------------------------------------------------------


def test_prioritaet_dann_aelteste_nummer(akteur, projekt):
    mittel_alt = _frei(akteur, projekt, "mittel-alt")
    niedrig = _frei(akteur, projekt, "niedrig", "niedrig")
    mittel_neu = _frei(akteur, projekt, "mittel-neu")
    hoch = _frei(akteur, projekt, "hoch", "hoch")
    assert dienst.weiter_reihenfolge(projekt) == [hoch, mittel_alt, mittel_neu, niedrig]


def test_vorziehen_heisst_auf_hoch_setzen(akteur, projekt):
    erster = _frei(akteur, projekt, "erster")
    zweiter = _frei(akteur, projekt, "zweiter")
    Auftrag.objects.filter(pk=zweiter.pk).update(prioritaet="hoch")
    assert dienst.weiter_reihenfolge(projekt)[0].pk == zweiter.pk
    assert dienst.weiter_reihenfolge(projekt)[1].pk == erster.pk


def test_nur_freigegebene_ohne_bearbeiter(akteur, projekt, bindung):
    arbeitspaket(akteur, projekt, titel="Nur Entwurf", umfang="e/**")
    laufend = _frei(akteur, projekt, "laufend")
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), laufend, laufend.revision_nr)
    frei = _frei(akteur, projekt, "frei")
    assert dienst.weiter_reihenfolge(projekt) == [frei]


def test_issues_zaehlen_wie_auftraege(akteur, projekt):
    a = _frei(akteur, projekt, "issue")
    Auftrag.objects.filter(pk=a.pk).update(typ=Auftrag.TYP_ISSUE)
    assert dienst.weiter_reihenfolge(projekt) == [a]


def test_offener_vorgaenger_haelt_zurueck(akteur, projekt):
    vorgaenger = _frei(akteur, projekt, "vorgaenger", "niedrig")
    nachfolger = _frei(akteur, projekt, "nachfolger", "hoch")
    dienst.vorgaenger_eintragen(akteur, nachfolger, vorgaenger)
    assert dienst.weiter_reihenfolge(projekt) == [vorgaenger]
    Auftrag.objects.filter(pk=vorgaenger.pk).update(zustand=Auftrag.ERLEDIGT)
    assert dienst.weiter_reihenfolge(projekt) == [nachfolger]


def test_abgebrochener_vorgaenger_haelt_nicht_zurueck(akteur, projekt):
    vorgaenger = _frei(akteur, projekt, "vorgaenger")
    nachfolger = _frei(akteur, projekt, "nachfolger")
    dienst.vorgaenger_eintragen(akteur, nachfolger, vorgaenger)
    Auftrag.objects.filter(pk=vorgaenger.pk).update(zustand=Auftrag.ABGEBROCHEN)
    assert dienst.weiter_reihenfolge(projekt) == [nachfolger]


def test_anderes_projekt_zaehlt_nicht(akteur, projekt, projekt_b):
    _frei(akteur, projekt_b, "fremd")
    assert dienst.weiter_reihenfolge(projekt) == []


# --- „Als Nächstes“ im Überblick -------------------------------------------------------------


def _abschnitt(client, projekt) -> str:
    html = client.get(_projekt_url(projekt)).content.decode()
    return html.split('id="h-naechstes"')[1].split("</section>")[0]


def test_ueberblick_zeigt_die_naechsten_fuenf_in_reihenfolge(owner_client, akteur, projekt):
    alle = [_frei(akteur, projekt, f"nr{i}") for i in range(7)]
    Auftrag.objects.filter(pk=alle[6].pk).update(prioritaet="hoch")
    teil = _abschnitt(owner_client, projekt)
    assert teil.count("<li>") == 5
    stellen = [teil.index(t) for t in ("nr6", "nr0", "nr1", "nr2", "nr3")]
    assert stellen == sorted(stellen)
    assert "nr4" not in teil and "nr5" not in teil


def test_steht_direkt_unter_wartet_auf_dich(owner_client, projekt):
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    stellen = [html.index(f">{t}") for t in ("Wartet auf dich", "Als Nächstes", "Version")]
    assert stellen == sorted(stellen)


def test_ohne_freigegebenes_sagt_es_was_passiert(owner_client, projekt):
    assert "welchen Entwurf er nehmen soll" in _abschnitt(owner_client, projekt)


def test_klickweg_von_der_startseite_ein_klick(owner_client, akteur, projekt):
    from tests.test_klickwege import _Klicker

    a = _frei(akteur, projekt, "ansehen")
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    assert f.klicks == 1 and reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}) in f.html
    assert "ansehen" in f.html.split('id="h-naechstes"')[1]


# --- Der Vertrag ------------------------------------------------------------------------------


def test_antwort_entspricht_dem_vertrag(akteur, projekt, bindung):
    a = _frei(akteur, projekt, "frei", "hoch")
    Auftrag.objects.filter(pk=a.pk).update(faellig=datetime.date(2099, 1, 2))
    antwort = _start(bindung.klartext)
    assert antwort.status_code == 200
    d = antwort.json()
    assert set(d) == ANTWORTFELDER
    assert d["ok"] is True and d["vertrag"] == 1 and d["projekt"] == projekt.kennung
    assert datetime.datetime.fromisoformat(d["stand"]).tzinfo is not None
    assert set(d["naechster"]) == EINTRAGSFELDER
    assert d["naechster"] == {
        "nummer": a.nummer,
        "titel": "frei",
        "modul": "Kern",
        "art": "auftrag",
        "zustand": "FREIGEGEBEN",
        "prioritaet": "hoch",
        "faellig": "2099-01-02",
        "uebernehmbar": True,
        "bearbeiter": None,
    }
    assert d["offen"] == [d["naechster"]] and d["weitere"] == 0 and d["wartet"] == []


def test_ohne_freigegebenes_ist_naechster_null(akteur, projekt, bindung):
    arbeitspaket(akteur, projekt)
    d = _start(bindung.klartext).json()
    assert d["naechster"] is None and len(d["offen"]) == 1
    assert d["offen"][0]["uebernehmbar"] is False


def test_offen_reihenfolge_laut_vertrag(akteur, projekt, bindung):
    entwurf = arbeitspaket(akteur, projekt, titel="Entwurf", umfang="e/**")
    laufend = _frei(akteur, projekt, "laufend")
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), laufend, laufend.revision_nr)
    blockiert = _blockiert(akteur, projekt, bindung)
    frei = _frei(akteur, projekt, "frei")
    d = _start(bindung.klartext).json()
    nummern = [e["nummer"] for e in d["offen"]]
    assert nummern[0] == frei.nummer and nummern[-1] == entwurf.nummer
    assert set(nummern[1:3]) == {laufend.nummer, blockiert.nummer}
    laufend_json = next(e for e in d["offen"] if e["nummer"] == laufend.nummer)
    assert laufend_json["bearbeiter"] == bindung.name and laufend_json["uebernehmbar"] is False


def test_freigegeben_mit_offenem_vorgaenger_ist_nicht_uebernehmbar(akteur, projekt, bindung):
    vorgaenger = _frei(akteur, projekt, "vorgaenger")
    nachfolger = _frei(akteur, projekt, "nachfolger")
    dienst.vorgaenger_eintragen(akteur, nachfolger, vorgaenger)
    d = _start(bindung.klartext).json()
    assert d["naechster"]["nummer"] == vorgaenger.nummer
    assert [(e["nummer"], e["uebernehmbar"]) for e in d["offen"]] == [
        (vorgaenger.nummer, True),
        (nachfolger.nummer, False),
    ]


def test_hoechstens_fuenfzig_und_weitere_zaehlt_den_rest(akteur, projekt, bindung):
    for i in range(53):
        dienst.wunsch_erfassen(akteur, projekt, titel=f"Idee {i}", beschreibung="b")
    d = _start(bindung.klartext).json()
    assert len(d["offen"]) == 50 and d["weitere"] == 3


def test_wartet_kennt_alle_arten(akteur, projekt, bindung):
    b, _ = dienst.bindung_anlegen(akteur, projekt, name="Zweiter Chat")
    arbeitspaket(akteur, projekt, titel="Zum Freigeben", umfang="f/**")
    rueckfrage = _blockiert(akteur, projekt, bindung, frage="Welche Sprache?")
    hindernis = _frei(akteur, projekt, "hindernis")
    chat = dienst.Akteur.aus_bindung(b)
    dienst.auftrag_uebernehmen(chat, hindernis, hindernis.revision_nr)
    dienst.hindernis_melden(chat, hindernis, hindernis.revision_nr, "Server fehlt")
    fehler = _frei(akteur, projekt, "fehler")
    b2, _ = dienst.bindung_anlegen(akteur, projekt, name="Dritter Chat")
    chat2 = dienst.Akteur.aus_bindung(b2)
    dienst.auftrag_uebernehmen(chat2, fehler, fehler.revision_nr)
    dienst.fehler_melden(chat2, fehler, fehler.revision_nr, "Abbruch")
    heute = timezone.localdate()
    dienst.wunsch_erfassen(akteur, projekt, titel="Fällig", beschreibung="b", faellig=heute)
    frage = akte.entscheidung_fragen(akteur, projekt, frage="Nachts sichern?", warum="W")
    d = _start(bindung.klartext).json()
    arten = {e["art"]: e for e in d["wartet"]}
    assert set(arten) == {
        "faellig",
        "entscheidung",
        "rueckfrage",
        "hindernis",
        "fehler",
        "freigabe",
    }
    for e in d["wartet"]:
        assert set(e) == WARTETFELDER
    assert d["wartet"][0]["art"] == "faellig" and d["wartet"][1]["art"] == "entscheidung"
    assert arten["faellig"]["faellig"] == heute.isoformat()
    assert arten["entscheidung"]["nummer"] == frage.nummer
    assert arten["entscheidung"]["text"] == "Nachts sichern?"
    assert arten["rueckfrage"]["nummer"] == rueckfrage.nummer
    assert arten["rueckfrage"]["text"] == "Welche Sprache?"
    assert arten["hindernis"]["text"] == "Server fehlt" and arten["fehler"]["text"] == "Abbruch"


# --- Anmeldung und Zuordnung ------------------------------------------------------------------


def test_ungueltiger_oder_fehlender_schluessel_ist_401(projekt, bindung):
    assert Client().get(PFAD).status_code == 401
    assert _start("kipm_falsch_falsch").status_code == 401
    assert _start(bindung.klartext + "x").status_code == 401
    bindung.widerrufen_am = timezone.now()
    bindung.save()
    assert _start(bindung.klartext).status_code == 401


def test_nur_lesen_erlaubt(bindung):
    c = Client(headers={"Authorization": f"Bearer {bindung.klartext}"})
    assert c.post(PFAD, data="{}", content_type="application/json").status_code == 405


def test_pc_zugang_nach_projekt_und_nach_ordner(akteur, projekt, pc_zugang):
    a = _frei(akteur, projekt, "frei")
    d = _start(pc_zugang.klartext, **{"X-KIPM-Projekt": projekt.kennung}).json()
    assert d["projekt"] == projekt.kennung and d["naechster"]["nummer"] == a.nummer
    ort = projekt.arbeitsorte.first()
    ort.pfad = r"W:\Synth"
    ort.save()
    d = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"W:\Synth\unter"}).json()
    assert d["projekt"] == projekt.kennung and d["naechster"]["nummer"] == a.nummer


def test_unbekannter_ordner_legt_nichts_an(akteur, projekt, pc_zugang):
    _frei(akteur, projekt, "frei")
    antwort = _start(pc_zugang.klartext, **{"X-KIPM-Ordner": r"W:\Fremd"})
    assert antwort.status_code == 200
    d = antwort.json()
    assert set(d) == ANTWORTFELDER
    assert d["projekt"] is None and d["naechster"] is None
    assert d["offen"] == [] and d["wartet"] == [] and d["weitere"] == 0
    assert not OrdnerAnfrage.objects.exists()


def test_pc_zugang_ohne_angabe_und_unbekanntes_projekt(akteur, projekt, pc_zugang):
    assert _start(pc_zugang.klartext).json()["projekt"] is None
    antwort = _start(pc_zugang.klartext, **{"X-KIPM-Projekt": "gibt-es-nicht"})
    assert antwort.status_code == 200 and antwort.json()["projekt"] is None


# --- Schreibt nichts, verrät nichts -----------------------------------------------------------


@pytest.mark.parametrize(
    "kopf", [{}, {"X-KIPM-Ordner": r"W:\Fremd"}, {"X-KIPM-Projekt": "synth-a"}]
)
def test_schreibt_nichts(akteur, projekt, bindung, pc_zugang, kopf):
    arbeitspaket(akteur, projekt)
    _blockiert(akteur, projekt, bindung)

    def abbild():
        return {
            m._meta.label: serializers.serialize("json", m.objects.all())
            for m in apps.get_app_config("steuerung").get_models()
        }

    vorher = abbild()
    assert _start(pc_zugang.klartext, **kopf).status_code == 200
    assert _start(bindung.klartext, **kopf).status_code == 200
    assert abbild() == vorher
    assert ChatBindung.objects.get(pk=pc_zugang.pk).zuletzt_aktiv is None


def test_projektschluessel_sieht_nie_ein_fremdes_projekt(akteur, projekt, projekt_b, bindung):
    fremd = _frei(akteur, projekt_b, "fremdes-geheimnis")
    akte.entscheidung_fragen(akteur, projekt_b, frage="Fremde Frage?")
    for kopf in ({}, {"X-KIPM-Projekt": projekt_b.kennung}):
        antwort = _start(bindung.klartext, **kopf)
        d = antwort.json()
        assert d["projekt"] == projekt.kennung
        text = antwort.content.decode()
        assert fremd.nummer not in text and "fremdes-geheimnis" not in text
        assert "Fremde" not in text


def test_schluessel_erscheinen_nie(akteur, projekt, bindung):
    token = "ghp_" + "a" * 30
    a = _frei(akteur, projekt, f"Titel {token}")
    akte.entscheidung_fragen(akteur, projekt, frage=f"Frage {token}")
    blockiert = _blockiert(akteur, projekt, bindung, frage=f"Rückfrage {token}")
    antwort = _start(bindung.klartext)
    text = antwort.content.decode()
    assert token not in text and bindung.klartext not in text
    assert a.nummer in text and blockiert.nummer in text


def test_kein_zugriff_mit_sitzung(owner_client, projekt):
    assert owner_client.get(PFAD).status_code == 401

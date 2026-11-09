"""Werkbank-Anbindung WP6: menschliche Aktionen, Bearbeitung ändern, Abbrechen, Kontextänderung
und Halt (Auftrag 4.7 und 7.3 bis 7.6)."""

import uuid

import pytest
from django.urls import reverse

from steuerung import akte, dienst, werkbank, werkbank_vertrag
from steuerung.models import (
    Auftrag,
    Ereignis,
    WerkbankAktion,
    WerkbankAusfuehrung,
    WerkbankOperation,
    WerkbankZuweisung,
)
from tests.werkbank_hilfen import (
    BASIS,
    ereignis_nachricht,
    kopf,
    schritt_nachricht,
    senden,
    uebernahme_nachricht,
    vor_quelle,
)

pytestmark = pytest.mark.django_db


def ereignis(client, ausf, instanz, art, daten=None, **kw):
    kw.setdefault("sequenz", ausf.ereignisse.count() + 1)
    antwort = senden(
        client, instanz, "ereignisse/", ereignis_nachricht(ausf, instanz, art, daten, **kw)
    )
    assert antwort.status_code == 200, antwort.content
    ausf.refresh_from_db()
    return antwort.json()


def frage(client, ausf, instanz, frage_id="q1"):
    ereignis(
        client,
        ausf,
        instanz,
        "rueckfrage",
        {"frage_id": frage_id, "paket": "root", "text": "A oder B?"},
    )


def freigabe(client, ausf, instanz, art="start", freigabe_id="f1"):
    ereignis(
        client,
        ausf,
        instanz,
        "freigabe_noetig",
        {"freigabe_id": freigabe_id, "art": art, "grund": "x"},
    )


def abgleich(client, instanz):
    antwort = client.get(BASIS + "abgleich/", **kopf(instanz))
    assert antwort.status_code == 200, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("abgleich_antwort", daten) == []
    return daten


# --- Antworten und Freigaben -----------------------------------------------------------------


def test_antwort_braucht_eine_offene_frage(akteur, w_ausfuehrung):
    with pytest.raises(dienst.DienstFehler):
        werkbank.aktion_erstellen(akteur, w_ausfuehrung, "antworten", frage_id="q1", text="A")


def test_nur_der_owner_erzeugt_aktionen(client, akteur, bindung, w_ausfuehrung, w_instanz):
    frage(client, w_ausfuehrung, w_instanz)
    pc, _ = dienst.bindung_anlegen(akteur, None, name="PC")
    for fremd in (dienst.Akteur.aus_bindung(bindung), dienst.Akteur.aus_bindung(pc)):
        with pytest.raises(dienst.KeinZugriff):
            werkbank.aktion_erstellen(fremd, w_ausfuehrung, "antworten", frage_id="q1", text="A")
    assert not WerkbankAktion.objects.exists()


def test_antwort_bleibt_ausstehend_bis_die_werkbank_sie_anwendet(
    client, akteur, w_ausfuehrung, w_instanz
):
    frage(client, w_ausfuehrung, w_instanz)
    aktion = werkbank.aktion_erstellen(
        akteur, w_ausfuehrung, "antworten", frage_id="q1", text="Variante A",
        erwartet_zustand="wartet_antwort",
    )  # fmt: skip
    assert aktion.zustand == "ausstehend" and aktion.owner == akteur.benutzer
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.zustand == "wartet_antwort"  # noch nicht angewendet
    eintrag = abgleich(client, w_instanz)["aktionen"][0]
    assert eintrag["aktion_id"] == str(aktion.aktion_id) and eintrag["art"] == "antworten"
    assert eintrag["bezug"]["frage_id"] == "q1" and eintrag["antworttext"] == "Variante A"
    assert eintrag["zustand"] == "ausstehend"
    ereignis(
        client, w_ausfuehrung, w_instanz, "antwort_angewendet", {"aktion_id": str(aktion.aktion_id)}
    )
    aktion.refresh_from_db()
    assert aktion.zustand == "ausgefuehrt"
    assert w_ausfuehrung.zustand == "laeuft"
    assert not any(b["offen"] for b in w_ausfuehrung.bedarf)
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT
    assert abgleich(client, w_instanz)["aktionen"] == []


def test_dieselbe_frage_nicht_zweimal_beantworten(client, akteur, w_ausfuehrung, w_instanz):
    frage(client, w_ausfuehrung, w_instanz)
    werkbank.aktion_erstellen(akteur, w_ausfuehrung, "antworten", frage_id="q1", text="A")
    with pytest.raises(dienst.Konflikt):
        werkbank.aktion_erstellen(akteur, w_ausfuehrung, "antworten", frage_id="q1", text="B")
    assert WerkbankAktion.objects.count() == 1


def test_veraltete_oberflaeche_wird_abgewiesen(client, akteur, w_ausfuehrung, w_instanz):
    frage(client, w_ausfuehrung, w_instanz)
    with pytest.raises(dienst.Konflikt):
        werkbank.aktion_erstellen(
            akteur, w_ausfuehrung, "antworten", frage_id="q1", text="A", erwartet_zustand="laeuft"
        )


def test_freigaben_passen_zu_ihrer_art(client, akteur, w_ausfuehrung, w_instanz):
    freigabe(client, w_ausfuehrung, w_instanz, art="start")
    with pytest.raises(dienst.DienstFehler):
        werkbank.aktion_erstellen(akteur, w_ausfuehrung, "uebernahme_freigeben", freigabe_id="f1")
    with pytest.raises(dienst.DienstFehler):
        werkbank.aktion_erstellen(
            akteur, w_ausfuehrung, "start_freigeben", freigabe_id="gibtsnicht"
        )
    aktion = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "start_freigeben", freigabe_id="f1")
    ereignis(
        client, w_ausfuehrung, w_instanz, "aktion_angewendet",
        {"aktion_id": str(aktion.aktion_id), "ergebnis": "angewendet"},
    )  # fmt: skip
    aktion.refresh_from_db()
    assert aktion.zustand == "ausgefuehrt"
    assert w_ausfuehrung.bedarf[0]["offen"] is False


def test_verworfene_aktion_gilt_als_veraltet(client, akteur, w_ausfuehrung, w_instanz):
    freigabe(client, w_ausfuehrung, w_instanz, art="uebernahme")
    aktion = werkbank.aktion_erstellen(
        akteur, w_ausfuehrung, "uebernahme_freigeben", freigabe_id="f1"
    )
    ereignis(
        client, w_ausfuehrung, w_instanz, "aktion_angewendet",
        {"aktion_id": str(aktion.aktion_id), "ergebnis": "verworfen", "grund": "zu spät"},
    )  # fmt: skip
    aktion.refresh_from_db()
    assert aktion.zustand == "veraltet" and w_ausfuehrung.bedarf[0]["offen"] is True


def test_unbekannte_aktion_in_meldung_wird_abgelehnt_quittiert(client, w_ausfuehrung, w_instanz):
    body = ereignis(
        client, w_ausfuehrung, w_instanz, "antwort_angewendet", {"aktion_id": str(uuid.uuid4())}
    )
    assert body["ergebnis"] == "historisch_quittiert"


def test_weiterbauen_loest_den_unklaren_ausgang(client, akteur, w_ausfuehrung, w_instanz):
    n = schritt_nachricht(w_ausfuehrung, w_instanz)
    assert senden(client, w_instanz, "schritt/", n).json()["erlaubt"]
    ereignis(
        client, w_ausfuehrung, w_instanz, "fehler",
        {"code": "KAPPUT", "text": "Ausgang unbekannt", "ausgang_unklar": True},
    )  # fmt: skip
    assert w_ausfuehrung.zustand == "unklar"
    aktion = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "weiterbauen", paket="root")
    ereignis(
        client, w_ausfuehrung, w_instanz, "aktion_angewendet",
        {"aktion_id": str(aktion.aktion_id), "ergebnis": "angewendet"},
    )  # fmt: skip
    assert w_ausfuehrung.zustand == "laeuft"
    assert WerkbankOperation.objects.get(art="schritt").klaerung == "geklaert"
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT
    nxt = schritt_nachricht(w_ausfuehrung, w_instanz, versuch=2)
    assert senden(client, w_instanz, "schritt/", nxt).json()["erlaubt"]


# --- Abbrechen -------------------------------------------------------------------------------


def test_abbruch_angefordert_ist_nicht_abbruch_bestaetigt(client, akteur, w_ausfuehrung, w_instanz):
    aktion = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "abbrechen")
    w_ausfuehrung.refresh_from_db()
    assert (
        w_ausfuehrung.halt_zweck == "abbrechen" and w_ausfuehrung.zustand == "abbruch_angefordert"
    )
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT  # noch nicht abgebrochen
    assert abgleich(client, w_instanz)["halte"][0]["zweck"] == "abbrechen"
    antwort = senden(client, w_instanz, "schritt/", schritt_nachricht(w_ausfuehrung, w_instanz))
    assert antwort.status_code == 409 and antwort.json()["fehler"]["code"] == "HALT_ANGEFORDERT"
    ereignis(
        client, w_ausfuehrung, w_instanz, "abbruch_bestaetigt", {"commits_bleiben": ["abc1234"]}
    )
    assert w_ausfuehrung.zustand == "abgebrochen"
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    assert a.zustand == Auftrag.ABGEBROCHEN and not a.hat_bearbeiter
    aktion.refresh_from_db()
    assert aktion.zustand == "ausgefuehrt"
    assert Ereignis.objects.filter(objekt_kennung=a.nummer, ereignis__startswith="Abbruch").exists()


def test_abbruch_bestaetigt_ohne_anforderung_wird_abgelehnt(client, w_ausfuehrung, w_instanz):
    n = ereignis_nachricht(w_ausfuehrung, w_instanz, "abbruch_bestaetigt", {}, sequenz=1)
    antwort = senden(client, w_instanz, "ereignisse/", n)
    assert antwort.status_code == 409
    w_ausfuehrung.auftrag.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT


# --- Bearbeitung ändern ----------------------------------------------------------------------


def test_bearbeitung_aendern_fuehrt_erst_nach_dem_stillstand_in_den_entwurf(
    client, akteur, w_ausfuehrung, w_instanz
):
    frage(client, w_ausfuehrung, w_instanz)
    offene = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "antworten", frage_id="q1", text="A")
    aktion = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "bearbeitung_beenden")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == "bearbeitung_aendern"
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    assert a.zustand != Auftrag.ENTWURF and a.hat_bearbeiter  # erst nach bestätigtem Stillstand
    ereignis(client, w_ausfuehrung, w_instanz, "halt_bestaetigt", {"zweck": "bearbeitung_aendern"})
    a.refresh_from_db()
    z = WerkbankZuweisung.objects.get(auftrag=a)
    assert a.zustand == Auftrag.ENTWURF and a.freigegeben_am is None and not a.hat_bearbeiter
    assert a.letzter_bearbeiter_name and z.version == 2 and z.kontext_sha256 == ""
    assert w_ausfuehrung.zustand == "beendet"
    offene.refresh_from_db()
    aktion.refresh_from_db()
    assert offene.zustand == "veraltet" and aktion.zustand == "ausgefuehrt"


def test_neue_ausfuehrung_erst_nach_neuer_freigabe(
    client, akteur, projekt, w_ausfuehrung, w_instanz, w_pbindung
):
    werkbank.aktion_erstellen(akteur, w_ausfuehrung, "bearbeitung_beenden")
    ereignis(client, w_ausfuehrung, w_instanz, "halt_bestaetigt", {"zweck": "bearbeitung_aendern"})
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    alt = uebernahme_nachricht(vor_quelle(w_instanz, w_pbindung, a))
    assert senden(client, w_instanz, "uebernehmen/", alt).status_code == 409  # nicht freigegeben
    werkbank.zuweisen(akteur, a, w_pbindung, freigeben=True)
    a.refresh_from_db()
    neu = uebernahme_nachricht(vor_quelle(w_instanz, w_pbindung, a))
    antwort = senden(client, w_instanz, "uebernehmen/", neu)
    assert antwort.status_code == 200, antwort.content
    assert WerkbankAusfuehrung.objects.filter(auftrag=a).count() == 2
    assert WerkbankAusfuehrung.objects.filter(auftrag=a, zustand="plant").count() == 1


def test_halt_mit_falschem_zweck_wird_abgelehnt(client, akteur, w_ausfuehrung, w_instanz):
    werkbank.aktion_erstellen(akteur, w_ausfuehrung, "bearbeitung_beenden")
    n = ereignis_nachricht(
        w_ausfuehrung, w_instanz, "halt_bestaetigt", {"zweck": "abbrechen"}, sequenz=1
    )
    assert senden(client, w_instanz, "ereignisse/", n).status_code == 409
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.zustand == "halt_angefordert"


def test_halt_nach_gespeichertem_abschluss_wird_abgelehnt(akteur, w_ausfuehrung):
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(zustand="abschluss_gemeldet")
    w_ausfuehrung.refresh_from_db()
    for art in ("abbrechen", "bearbeitung_beenden"):
        with pytest.raises(dienst.Konflikt) as fehler:
            werkbank.aktion_erstellen(akteur, w_ausfuehrung, art)
        assert "Abschluss" in str(fehler.value)
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == ""


# --- Kontextänderung -------------------------------------------------------------------------


def test_kontextaenderung_sperrt_neue_schritte_bis_zur_neuen_freigabe(
    client, akteur, projekt, w_ausfuehrung, w_instanz, w_pbindung
):
    alt_hash = w_ausfuehrung.snapshot_sha256
    offene = None
    frage(client, w_ausfuehrung, w_instanz)
    offene = werkbank.aktion_erstellen(akteur, w_ausfuehrung, "antworten", frage_id="q1", text="A")
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Neuer Kurs", warum="Darum")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == "kontext_veraltet"
    assert w_ausfuehrung.zustand == "halt_angefordert"
    assert any(b["typ"] == "kontext_veraltet" for b in w_ausfuehrung.bedarf)
    antwort = senden(client, w_instanz, "schritt/", schritt_nachricht(w_ausfuehrung, w_instanz))
    assert antwort.status_code == 409 and antwort.json()["fehler"]["code"] == "HALT_ANGEFORDERT"
    ereignis(client, w_ausfuehrung, w_instanz, "halt_bestaetigt", {"zweck": "kontext_veraltet"})
    a = w_ausfuehrung.auftrag
    a.refresh_from_db()
    z = WerkbankZuweisung.objects.get(auftrag=a)
    assert a.zustand == Auftrag.ENTWURF and not a.hat_bearbeiter and z.version == 2
    offene.refresh_from_db()
    assert offene.zustand == "veraltet"
    werkbank.zuweisen(akteur, a, w_pbindung, freigeben=True)
    a.refresh_from_db()
    neu = uebernahme_nachricht(vor_quelle(w_instanz, w_pbindung, a))
    body = senden(client, w_instanz, "uebernehmen/", neu).json()
    assert body["quelle"]["snapshot_sha256"] != alt_hash  # neuer Snapshot


def test_normale_fortschritte_termine_und_meilensteine_loesen_keinen_halt_aus(
    akteur, projekt, w_ausfuehrung
):
    anderer = arbeitspaket_anderer(akteur, projekt)
    dienst.auftrag_freigeben(akteur, anderer)
    akte.meilenstein_anlegen(akteur, projekt, name="M-Neu")
    Auftrag.objects.filter(pk=w_ausfuehrung.auftrag_id).update(faellig="2030-01-01")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == "" and w_ausfuehrung.zustand == "plant"


def arbeitspaket_anderer(akteur, projekt):
    from tests.conftest import arbeitspaket

    return arbeitspaket(akteur, projekt, titel="Anderer", umfang="docs/**")


def test_text_und_profil_aenderung_loest_halt_aus(akteur, projekt, w_ausfuehrung):
    akte.text_anlegen(akteur, projekt, titel="Notiz", text="Neu")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == "kontext_veraltet"


def test_werkbank_meldet_kontext_veraltet(client, w_ausfuehrung, w_instanz):
    ereignis(client, w_ausfuehrung, w_instanz, "kontext_veraltet", {"grund": "Prüfprofil geändert"})
    assert w_ausfuehrung.halt_zweck == "kontext_veraltet"
    assert w_ausfuehrung.zustand == "halt_angefordert"


def test_ein_vorhandener_halt_wird_nicht_ueberschrieben(client, akteur, projekt, w_ausfuehrung):
    werkbank.aktion_erstellen(akteur, w_ausfuehrung, "abbrechen")
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Neu", warum="x")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.halt_zweck == "abbrechen"


@pytest.mark.parametrize(
    "modul",
    ["werkbank_aktionen", "werkbank_lauf", "werkbank_abschluss", "views_werkbank", "werkbank"],
)
def test_jedes_teilmodul_laesst_sich_zuerst_importieren(modul):
    """Kein Importzyklus, egal welches Teilmodul als erstes geladen wird (frischer Prozess)."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "kipm.settings", "PYTHONIOENCODING": "utf-8"}
    code = f"import django; django.setup(); import steuerung.{modul}"
    ergebnis = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, encoding="utf-8"
    )
    assert ergebnis.returncode == 0, ergebnis.stderr[-500:]


def test_ktl1_uebernahmefreigabe_ohne_kandidaten_steht_im_abgleich(
    client, owner_client, akteur, w_ausfuehrung, w_instanz
):
    """Testlauf KTL-1, B7: nach „Weiterbauen“ bittet die Werkbank um eine Übernahmefreigabe ohne
    Kandidaten; Freds Aktion muss im Abgleich als „ausstehend“ mit passender Quelle stehen."""
    ausf = w_ausfuehrung
    ereignis(
        client, ausf, w_instanz, "plan_bestaetigt",
        {"planfassung": "c" * 64, "pakete": [
            {"paket": "fahrenheit-umrechnung", "titel": "Umrechnung", "zustand": "offen"}]},
    )  # fmt: skip
    ereignis(
        client, ausf, w_instanz, "fehler",
        {"code": "ABSCHLUSS_UNVOLLSTAENDIG", "text": "Unvollständig.", "ausgang_unklar": False},
    )  # fmt: skip
    weiter = werkbank.aktion_erstellen(akteur, ausf, "weiterbauen", erwartet_zustand=ausf.zustand)
    ereignis(
        client, ausf, w_instanz, "aktion_angewendet",
        {"aktion_id": str(weiter.aktion_id), "ergebnis": "angewendet"},
    )  # fmt: skip
    paket = "fahrenheit-umrechnung-A-20261109183356-5d6490-fahrenheit-umrechn"
    ereignis(
        client, ausf, w_instanz, "freigabe_noetig",
        {"freigabe_id": "F-7a405350cdc9", "art": "uebernahme", "pakete": [paket],
         "grund": "Übernahme ABGELEHNT KIPM_ENTSCHEIDUNG_FEHLT"},
    )  # fmt: skip
    antwort = owner_client.post(
        reverse("ausfuehrung_aktion", kwargs={"ausfuehrung_id": ausf.pk}),
        {"art": "uebernahme_freigeben", "freigabe_id": "F-7a405350cdc9", "erwartet": ausf.zustand},
    )
    assert antwort.status_code in (200, 302)
    aktion = WerkbankAktion.objects.get(art="uebernahme_freigeben")
    assert aktion.zustand == "ausstehend"
    daten = abgleich(client, w_instanz)
    eintrag = next(a for a in daten["aktionen"] if a["aktion_id"] == str(aktion.aktion_id))
    assert eintrag["zustand"] == "ausstehend"
    assert eintrag["bezug"] == {"freigabe_id": "F-7a405350cdc9"}
    assert eintrag["erwarteter_zustand"] == ausf.zustand
    assert eintrag["quelle"]["ausfuehrung_id"] == str(ausf.ausfuehrung_id)

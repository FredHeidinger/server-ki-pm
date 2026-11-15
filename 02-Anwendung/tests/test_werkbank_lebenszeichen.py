"""KIPM-02 (KI-PM-Auftrag KIPM-27): Lebenszeichen nicht unbegrenzt speichern, liegengebliebene
Ausführungen in „Wartet auf dich“, Abweichungen zwischen Auftrag und Ausführung mit Grund.

Paket: W:\\AIProgramming\\pakete\\KIPM-PAKETE-20261115.md. Vertrag ``kipm-werkbank/1`` bleibt
unverändert: Jede Antwort und der Operationsabruf des jüngsten Lebenszeichens bleiben wie bisher.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import werkbank_lauf
from steuerung.models import Auftrag, WerkbankAusfuehrung, WerkbankEreignis, WerkbankOperation
from tests.werkbank_hilfen import BASIS, ereignis_nachricht, kopf, senden

pytestmark = pytest.mark.django_db


def _senden(client, ausf, instanz, art, daten=None, *, sequenz):
    n = ereignis_nachricht(ausf, instanz, art, daten, sequenz=sequenz)
    antwort = senden(client, instanz, "ereignisse/", n)
    assert antwort.status_code == 200, antwort.content
    return n, antwort.json()


def _zurueckdatieren(ausf, stunden: float) -> None:
    """Alles an der Ausführung um ``stunden`` in die Vergangenheit legen."""
    alt = timezone.now() - timedelta(hours=stunden)
    WerkbankAusfuehrung.objects.filter(pk=ausf.pk).update(angelegt_am=alt)
    WerkbankEreignis.objects.filter(ausfuehrung=ausf).update(empfangen_am=alt)


def _wartet(client, projekt) -> str:
    html = client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    return html.content.decode().split('id="wartet"')[1].split("</section>")[0]


# --- Speicherung begrenzen ------------------------------------------------------------------


def test_hundert_lebenszeichen_ergeben_eine_zeile(client, w_ausfuehrung, w_instanz):
    letzte = None
    for nr in range(1, 101):
        letzte, body = _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=nr)
        assert body["ergebnis"] == "verarbeitet"
    zeilen = WerkbankEreignis.objects.filter(ausfuehrung=w_ausfuehrung, art="lebenszeichen")
    assert zeilen.count() == 1
    zeile = zeilen.get()
    assert zeile.sequenz == 100 and str(zeile.ereignis_id) == letzte["nutzlast"]["ereignis_id"]
    # Auch die Operationsnachweise wachsen nicht: nur Übernahme und das jüngste Lebenszeichen.
    assert WerkbankOperation.objects.filter(art="ereignis").count() == 1


def test_juengstes_lebenszeichen_bleibt_abrufbar(client, w_ausfuehrung, w_instanz):
    _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=1)
    n, body = _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=2)
    abruf = client.get(f"{BASIS}operationen/{n['operation_id']}/", **kopf(w_instanz)).json()
    assert abruf["status"] == "gespeichert" and abruf["ausgang"]["antwort"] == body
    # Eine identische Wiederholung wirkt nicht zweimal und legt nichts an.
    wieder = senden(client, w_instanz, "ereignisse/", n)
    # Vertrag: eine identische Wiederholung heißt „wiederholt“ und wirkt nicht zweimal.
    assert wieder.status_code == 200 and wieder.json()["ergebnis"] == "wiederholt"
    assert WerkbankEreignis.objects.filter(art="lebenszeichen").count() == 1


def test_verspaetetes_aelteres_lebenszeichen_ueberschreibt_nicht(client, w_ausfuehrung, w_instanz):
    _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=5)
    _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=3)
    zeile = WerkbankEreignis.objects.get(ausfuehrung=w_ausfuehrung, art="lebenszeichen")
    assert zeile.sequenz == 5


def test_andere_ereignisse_bleiben_vollstaendig(client, w_ausfuehrung, w_instanz):
    _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=1)
    for nr, zustand in ((2, "laeuft"), (3, "laeuft"), (4, "erledigt")):
        _senden(
            client, w_ausfuehrung, w_instanz, "paket_status",
            {"paket": "root", "zustand": zustand}, sequenz=nr,
        )  # fmt: skip
        _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=nr + 10)
    assert WerkbankEreignis.objects.filter(art="paket_status").count() == 3
    assert WerkbankEreignis.objects.filter(art="lebenszeichen").count() == 1


# --- Kein Fortschritt seit N Stunden --------------------------------------------------------


def test_ohne_fortschritt_steht_in_wartet_auf_dich(
    owner_client, client, w_ausfuehrung, w_instanz, projekt
):
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    # Lebenszeichen kommen weiter, zählen aber nicht als Fortschritt.
    _senden(client, w_ausfuehrung, w_instanz, "lebenszeichen", sequenz=1)
    wartet = _wartet(owner_client, projekt)
    assert "Werkbank ohne Fortschritt seit" in wartet
    assert w_ausfuehrung.auftrag.nummer in wartet
    assert 'name="art" value="abbrechen"' in wartet
    assert 'name="art" value="weiterbauen"' not in wartet  # kein Fehler offen


def test_weiterbauen_nur_bei_offenem_fehler(
    owner_client, client, w_ausfuehrung, w_instanz, projekt
):
    _senden(
        client, w_ausfuehrung, w_instanz, "fehler",
        {"paket": "root", "code": "TEST", "text": "Prüfung rot"}, sequenz=1,
    )  # fmt: skip
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    wartet = _wartet(owner_client, projekt)
    assert "Werkbank ohne Fortschritt seit" in wartet
    assert 'name="art" value="weiterbauen"' in wartet
    assert wartet.count(w_ausfuehrung.auftrag.nummer) == 1  # ein Eintrag, nicht zwei


def test_fortschritt_setzt_die_uhr_zurueck(owner_client, client, w_ausfuehrung, w_instanz, projekt):
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    _senden(
        client, w_ausfuehrung, w_instanz, "paket_status",
        {"paket": "root", "zustand": "laeuft"}, sequenz=1,
    )  # fmt: skip
    assert werkbank_lauf.ohne_fortschritt(w_ausfuehrung) is None
    assert "Werkbank ohne Fortschritt" not in _wartet(owner_client, projekt)


def test_noch_innerhalb_der_frist_kein_eintrag(owner_client, w_ausfuehrung, projekt):
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN - 0.5)
    assert "Werkbank ohne Fortschritt" not in _wartet(owner_client, projekt)


@pytest.mark.parametrize("ende", ["abgebrochen", "abgeschlossen"])
def test_nach_abbruch_oder_abschluss_verschwindet_der_eintrag(
    owner_client, w_ausfuehrung, projekt, ende
):
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    assert "Werkbank ohne Fortschritt" in _wartet(owner_client, projekt)
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(zustand=ende)
    assert "Werkbank ohne Fortschritt" not in _wartet(owner_client, projekt)


def test_nach_angefordertem_abbruch_keine_knoepfe_mehr(owner_client, w_ausfuehrung, projekt):
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    antwort = owner_client.post(
        reverse("ausfuehrung_aktion", kwargs={"ausfuehrung_id": w_ausfuehrung.pk}),
        {"art": "abbrechen", "erwartet": w_ausfuehrung.zustand},
    )
    assert antwort.status_code == 302
    wartet = _wartet(owner_client, projekt)
    assert "Abbruch angefordert am" in wartet and "wartet auf die Werkbank" in wartet
    assert 'name="art" value="abbrechen"' not in wartet


def test_projektkarte_zaehlt_den_eintrag(owner_client, w_ausfuehrung, projekt):
    vorher = owner_client.get(reverse("projekte")).content.decode()
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    nachher = owner_client.get(reverse("projekte")).content.decode()
    zahl = lambda html: int(html.split(" wartet auf dich")[0].rsplit(">", 1)[-1].split()[-1])  # noqa: E731
    assert zahl(nachher) == zahl(vorher) + 1


def test_sitzungsstart_der_chats_bleibt_unveraendert(client, bindung, w_ausfuehrung, projekt):
    vorher = client.get("/api/sitzungsstart/", HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}")
    _zurueckdatieren(w_ausfuehrung, werkbank_lauf.KEIN_FORTSCHRITT_STUNDEN + 1)
    nachher = client.get("/api/sitzungsstart/", HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}")
    assert vorher.json()["wartet"] == nachher.json()["wartet"]


# --- Übereinstimmung Auftrag ↔ Ausführung (BR-005) ------------------------------------------


def test_abweichung_aktive_ausfuehrung_bei_abgeschlossenem_auftrag(owner_client, w_ausfuehrung):
    Auftrag.objects.filter(pk=w_ausfuehrung.auftrag_id).update(zustand=Auftrag.ABGEBROCHEN)
    gruende = werkbank_lauf.abweichungen(w_ausfuehrung)
    assert gruende and "noch als aktiv" in gruende[0]
    html = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": w_ausfuehrung.auftrag_id})
    ).content.decode()
    assert "Abweichung:" in html and "noch als aktiv" in html


def test_abweichung_beendete_ausfuehrung_bei_auftrag_in_arbeit(w_ausfuehrung):
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(zustand="abgebrochen")
    w_ausfuehrung.refresh_from_db()
    assert w_ausfuehrung.auftrag.zustand == Auftrag.IN_ARBEIT
    assert any("noch in Arbeit" in g for g in werkbank_lauf.abweichungen(w_ausfuehrung))


def test_abweichung_halt_lange_unbestaetigt(w_ausfuehrung):
    WerkbankAusfuehrung.objects.filter(pk=w_ausfuehrung.pk).update(
        halt_zweck="abbrechen",
        halt_angefordert_am=timezone.now() - timedelta(hours=3),
    )
    w_ausfuehrung.refresh_from_db()
    assert any("nicht bestätigt" in g for g in werkbank_lauf.abweichungen(w_ausfuehrung))


def test_keine_abweichung_im_normalfall(w_ausfuehrung):
    assert werkbank_lauf.abweichungen(w_ausfuehrung) == []

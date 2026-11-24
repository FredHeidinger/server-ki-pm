"""KIPM-28 (Paket KIPM-04): Nachweis je Kriterium am Werkbank-Auftrag und beobachtete
Selbstständigkeit der Werkbank je Projektbindung.

Erwartungen aus dem Auftrag, vor dem Code festgelegt:
- Nach einem angenommenen Abschlussbericht zeigt der Auftrag „Kriterium · Test · Ergebnis“ für
  jedes Kriterium des Snapshots; fehlt im Bericht ein Nachweis, steht „Zuordnung fehlt“.
- Der Kasten „Abschluss“ zeigt Integrationen und Gesamtprüfung aus dem angenommenen Bericht.
- Die Verwaltung nennt je Projektbindung, ob die Werkbank Start und Übernahme ohne Freds Klick
  macht, aus den beobachteten Läufen und als beobachtet gekennzeichnet; ohne Lauf „noch nicht
  beobachtet“.
"""

import pytest
from django.urls import reverse

from steuerung import werkbank_abschluss
from steuerung.models import WerkbankEreignis
from tests.werkbank_hilfen import ereignis_nachricht, schritt_nachricht, senden

pytestmark = pytest.mark.django_db

PLAN = "d" * 64
COMMIT = "abc1234"


def ereignis(client, ausf, instanz, art, daten=None):
    n = ereignis_nachricht(ausf, instanz, art, daten, sequenz=ausf.ereignisse.count() + 1)
    antwort = senden(client, instanz, "ereignisse/", n)
    assert antwort.status_code == 200, antwort.content
    ausf.refresh_from_db()


def plan(client, ausf, instanz, zustand="offen"):
    ereignis(
        client,
        ausf,
        instanz,
        "plan_bestaetigt",
        {"planfassung": PLAN, "pakete": [{"paket": "p1", "titel": "Paket", "zustand": zustand}]},
    )


def abschliessen(client, ausf, instanz, nachweise=None):
    """Plan, Paket erledigt, Abschlussbericht mit je einem Nachweis pro Kriterium."""
    plan(client, ausf, instanz)
    ereignis(client, ausf, instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"})
    ids = [k["id"] for k in ausf.snapshot["abnahmekriterien"]]
    nachweise = nachweise or {}
    ereignis(
        client,
        ausf,
        instanz,
        "abschluss_bericht",
        {
            "planfassung": PLAN,
            "kriterien": [
                {
                    "kriterium_id": i,
                    "behandlung": "erfuellt",
                    "nachweis": nachweise.get(i, f"tests/test_ak.py::test_{i.lower()}"),
                    "commit": COMMIT,
                }
                for i in ids
            ],
            "paketplan": [{"paket": "p1", "titel": "Paket", "zustand": "erledigt"}],
            "integrationen": [{"paket": "p1", "commit": COMMIT}],
            "pruefungen": [
                {"paket": "p1", "art": "test", "ergebnis": "bestanden", "beschreibung": "pytest"}
            ],
            "gesamtpruefung": {
                "paket": "root",
                "art": "gesamt",
                "ergebnis": "bestanden",
                "beschreibung": "Gesamtlauf grün",
            },
            "offene_arbeiten": [],
            "ungeklaerte_wirkungen": [],
        },
    )
    return ausf


def auftragsseite(client, ausf):
    antwort = client.get(reverse("auftrag_detail", kwargs={"auftrag_id": ausf.auftrag.pk}))
    assert antwort.status_code == 200
    return antwort.content.decode()


def verwaltung(client):
    antwort = client.get(reverse("verbindungen"))
    assert antwort.status_code == 200
    return antwort.content.decode()


# --- Nachweis je Kriterium -------------------------------------------------------------------


def test_abschluss_zeigt_je_kriterium_test_und_ergebnis(
    client, owner_client, w_ausfuehrung, w_instanz
):
    ausf = abschliessen(client, w_ausfuehrung, w_instanz)
    tabelle = werkbank_abschluss.kriterien_nachweis(ausf)
    snapshot = ausf.snapshot["abnahmekriterien"]
    assert [z["id"] for z in tabelle] == [k["id"] for k in snapshot]
    assert all(z["zuordnung"] and z["ergebnis"] == "erfüllt" for z in tabelle)
    html = auftragsseite(owner_client, ausf)
    assert "Kriterium" in html and "Test" in html and "Ergebnis" in html
    for k in snapshot:
        assert k["id"] in html and f"tests/test_ak.py::test_{k['id'].lower()}" in html
    assert COMMIT in html


def test_fehlender_nachweis_heisst_zuordnung_fehlt(client, owner_client, w_ausfuehrung, w_instanz):
    """Ein gespeicherter Bericht, der ein Kriterium nicht nennt (etwa ein älterer), zeigt die
    Lücke an statt sie zu verschweigen."""
    ausf = abschliessen(client, w_ausfuehrung, w_instanz)
    e = WerkbankEreignis.objects.get(ausfuehrung=ausf, art="abschluss_bericht")
    e.rueckmeldung["ereignis"]["daten"]["kriterien"] = e.rueckmeldung["ereignis"]["daten"][
        "kriterien"
    ][1:]
    e.save()
    tabelle = werkbank_abschluss.kriterien_nachweis(ausf)
    assert tabelle[0]["zuordnung"] is False and tabelle[0]["ergebnis"] == "nicht gemeldet"
    assert "Zuordnung fehlt" in auftragsseite(owner_client, ausf)


def test_abschlusskasten_zeigt_integration_und_gesamtpruefung(
    client, owner_client, w_ausfuehrung, w_instanz
):
    """Der Kasten „Abschluss“ las bisher eine nie befüllte Liste und blieb leer."""
    ausf = abschliessen(client, w_ausfuehrung, w_instanz)
    html = auftragsseite(owner_client, ausf)
    assert "Integriert: p1" in html and "Gesamtlauf grün" in html


def test_ohne_abschluss_keine_tabelle(owner_client, w_ausfuehrung):
    assert werkbank_abschluss.kriterien_nachweis(w_ausfuehrung) is None
    assert "Zuordnung fehlt" not in auftragsseite(owner_client, w_ausfuehrung)


def test_nachweistext_wird_maskiert(client, owner_client, w_ausfuehrung, w_instanz):
    ausf = abschliessen(
        client, w_ausfuehrung, w_instanz, nachweise={"AK-1": "<script>alert(1)</script>"}
    )
    html = auftragsseite(owner_client, ausf)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# --- Beobachtete Selbstständigkeit der Werkbank -----------------------------------------------


def test_ohne_lauf_noch_nicht_beobachtet(owner_client, w_pbindung):
    v = werkbank_abschluss.vollmacht(w_pbindung)
    assert v["start"]["stand"] == "unbekannt" and v["uebernahme"]["stand"] == "unbekannt"
    html = verwaltung(owner_client)
    assert "noch nicht beobachtet" in html


def test_werkbank_die_fragt(client, owner_client, w_ausfuehrung, w_instanz, w_pbindung):
    ausf = abschliessen(client, w_ausfuehrung, w_instanz)
    ausf.bedarf = [
        {"typ": "freigabe", "freigabe_id": "s1", "art": "start", "offen": False},
        {"typ": "freigabe", "freigabe_id": "u1", "art": "uebernahme", "offen": False},
    ]
    ausf.save()
    v = werkbank_abschluss.vollmacht(w_pbindung)
    assert v["start"]["stand"] == "fragt" and v["uebernahme"]["stand"] == "fragt"
    assert v["start"]["auftrag"] == ausf.auftrag.nummer
    html = verwaltung(owner_client)
    assert "Start fragt hier" in html and "Übernahme fragt hier" in html
    assert "beobachtet" in html


def test_werkbank_die_selbst_baut_und_uebernimmt(
    client, owner_client, w_ausfuehrung, w_instanz, w_pbindung
):
    plan(client, w_ausfuehrung, w_instanz)
    antwort = senden(
        client,
        w_instanz,
        "schritt/",
        schritt_nachricht(w_ausfuehrung, w_instanz, paket="p1", planfassung=PLAN),
    )
    assert antwort.status_code == 200, antwort.content
    w_ausfuehrung.refresh_from_db()
    ereignis(
        client, w_ausfuehrung, w_instanz, "paket_status", {"paket": "p1", "zustand": "erledigt"}
    )
    w_ausfuehrung.refresh_from_db()
    w_ausfuehrung.zustand = "abschluss_gemeldet"
    w_ausfuehrung.save()
    v = werkbank_abschluss.vollmacht(w_pbindung)
    assert v["start"]["stand"] == "selbst" and v["uebernahme"]["stand"] == "selbst"
    html = verwaltung(owner_client)
    assert "startet ohne Frage" in html and "übernimmt ohne Frage" in html


def test_nur_vorplanung_ist_noch_kein_start(client, w_ausfuehrung, w_instanz, w_pbindung):
    """Schritte der Vorplanung (Paket root) zeigen noch nicht, ob der Bau ohne Frage beginnt."""
    antwort = senden(client, w_instanz, "schritt/", schritt_nachricht(w_ausfuehrung, w_instanz))
    assert antwort.status_code == 200, antwort.content
    v = werkbank_abschluss.vollmacht(w_pbindung)
    assert v["start"]["stand"] == "unbekannt"

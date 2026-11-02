import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Ereignis, Modul, Projekt

pytestmark = pytest.mark.django_db


def test_dienst_registriert_projekt_mit_modulen_und_ereignis(owner):
    akteur = dienst.Akteur.aus_benutzer(owner)
    projekt = dienst.projekt_registrieren(
        akteur,
        kennung="Synth A",
        name="Synthetisches A",
        module=[("kern", "Kern"), ("doku", "Doku")],
    )
    assert projekt.kennung == "synth-a"
    assert list(Modul.objects.filter(projekt=projekt).values_list("kennung", flat=True)) == [
        "doku",
        "kern",
    ]
    ereignis = Ereignis.objects.get()
    assert ereignis.objekt_kennung == "synth-a" and ereignis.ereignis == "Projekt registriert"
    assert ereignis.akteur == owner and ereignis.akteur_name == "owner-test"


def test_doppelte_kennung_wird_abgelehnt_ohne_halbe_daten(owner):
    akteur = dienst.Akteur.aus_benutzer(owner)
    dienst.projekt_registrieren(akteur, kennung="synth-a", name="Erstes")
    with pytest.raises(dienst.DienstFehler, match="bereits vergeben"):
        dienst.projekt_registrieren(akteur, kennung="synth-a", name="Zweites", module=[("x", "X")])
    assert Projekt.objects.count() == 1
    assert Modul.objects.count() == 0
    assert Ereignis.objects.count() == 1


def test_nur_owner_darf_registrieren(db):
    normal = get_user_model().objects.create_user(username="gast", password="nur-fuer-tests-123456")
    with pytest.raises(dienst.DienstFehler, match="nur der Owner"):
        dienst.projekt_registrieren(dienst.Akteur.aus_benutzer(normal), kennung="x", name="X")
    assert Projekt.objects.count() == 0


def test_module_aus_text():
    assert dienst.module_aus_text("Kern\n\nkern-2: Zweiter Kern\n") == [
        ("kern", "Kern"),
        ("kern-2", "Zweiter Kern"),
    ]
    with pytest.raises(dienst.DienstFehler, match="doppelt"):
        dienst.module_aus_text("Kern\nkern")


def test_formular_registriert_und_zeigt_projekt(owner_client):
    antwort = owner_client.post(
        reverse("projekt_neu"),
        {
            "kennung": "synth-a",
            "name": "Synthetisches A",
            "beschreibung": "Testprojekt",
            "module": "Kern\nDoku",
        },
    )
    assert antwort.status_code == 302
    seite = owner_client.get(reverse("uebersicht")).content.decode()
    assert "Synthetisches A" in seite and "Kern" in seite and "Doku" in seite
    detail = owner_client.get(antwort["Location"]).content.decode()
    assert "synth-a" in detail and "Chat-Zugänge" in detail
    verlauf = owner_client.get(reverse("verlauf")).content.decode()
    assert "Projekt registriert" in verlauf


def test_formular_zeigt_fehler_bei_doppelter_kennung(owner_client):
    owner_client.post(reverse("projekt_neu"), {"kennung": "synth-a", "name": "A"})
    antwort = owner_client.post(reverse("projekt_neu"), {"kennung": "synth-a", "name": "B"})
    assert antwort.status_code == 200
    assert "bereits vergeben" in antwort.content.decode()
    assert Projekt.objects.count() == 1


def test_projektfilter_wirkt_auf_uebersicht_und_verlauf(owner_client, owner):
    akteur = dienst.Akteur.aus_benutzer(owner)
    dienst.projekt_registrieren(akteur, kennung="synth-a", name="Synthetisches A")
    dienst.projekt_registrieren(akteur, kennung="synth-b", name="Synthetisches B")
    owner_client.post(
        reverse("projekt_filter"), {"projekt": "synth-b", "zurueck": reverse("verlauf")}
    )
    uebersicht = owner_client.get(reverse("uebersicht")).content.decode()
    assert "Synthetisches B" in uebersicht and '<th scope="row">Synthetisches A' not in uebersicht
    verlauf = owner_client.get(reverse("verlauf")).content.decode()
    assert "synth-b" in verlauf and "<code>synth-a</code>" not in verlauf
    owner_client.post(reverse("projekt_filter"), {"projekt": "", "zurueck": "/"})
    assert "Synthetisches A" in owner_client.get(reverse("uebersicht")).content.decode()


def test_projektfilter_lehnt_fremde_rueckziele_ab(owner_client):
    antwort = owner_client.post(
        reverse("projekt_filter"), {"projekt": "", "zurueck": "https://boese.example/"}
    )
    assert antwort["Location"] == "/"


def test_nutzertext_wird_nicht_als_html_ausgefuehrt(owner_client):
    owner_client.post(reverse("projekt_neu"), {"kennung": "x", "name": "<script>alert(1)</script>"})
    seite = owner_client.get(reverse("uebersicht")).content.decode()
    assert "<script>alert(1)</script>" not in seite and "&lt;script&gt;" in seite

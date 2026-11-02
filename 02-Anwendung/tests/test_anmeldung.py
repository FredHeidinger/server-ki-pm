import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name",
    [
        "posteingang",
        "meine",
        "auftraege",
        "pruefung",
        "entscheidungen",
        "verlauf",
        "projekte",
        "projekt_neu",
        "verbindungen",
    ],
)
def test_ohne_anmeldung_wird_umgeleitet(client, name):
    antwort = client.get(reverse(name))
    assert antwort.status_code == 302
    assert antwort["Location"].startswith(reverse("anmelden"))


def test_anmeldung_und_abmeldung(client, owner):
    antwort = client.post(
        reverse("anmelden"), {"username": "owner-test", "password": "nur-fuer-tests-123456"}
    )
    assert antwort.status_code == 302 and antwort["Location"] == reverse("posteingang")
    seite = client.get(reverse("posteingang"))
    assert seite.status_code == 200
    assert "Angemeldet" in seite.content.decode()
    assert client.post(reverse("abmelden")).status_code == 302
    assert client.get(reverse("posteingang")).status_code == 302


def test_falsches_passwort_bleibt_draussen(client, owner):
    antwort = client.post(reverse("anmelden"), {"username": "owner-test", "password": "falsch"})
    assert antwort.status_code == 200
    assert client.get(reverse("posteingang")).status_code == 302


def test_abmelden_nur_per_post(owner_client):
    assert owner_client.get(reverse("abmelden")).status_code == 405

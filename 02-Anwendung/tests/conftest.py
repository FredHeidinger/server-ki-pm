import pytest
from django.contrib.auth import get_user_model

from steuerung import dienst
from steuerung.models import Auftrag

PASSWORT = "nur-fuer-tests-123456"


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_superuser(
        username="owner-test", password=PASSWORT, email=""
    )


@pytest.fixture
def zweiter_mensch(db):
    """Zweites menschliches Konto als Prüfer (normales Konto, kein Owner)."""
    return get_user_model().objects.create_user(username="pruefer-test", password=PASSWORT)


@pytest.fixture
def owner_client(client, owner):
    client.force_login(owner)
    return client


@pytest.fixture
def akteur(owner):
    return dienst.Akteur.aus_benutzer(owner)


@pytest.fixture
def projekt(akteur):
    p = dienst.projekt_registrieren(
        akteur, kennung="synth-a", name="Synthetisches A", module=[("kern", "Kern")]
    )
    dienst.arbeitsort_anlegen(akteur, p, kennung="repo", name="Repository synth-a", rechner="PC")
    return p


@pytest.fixture
def projekt_b(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="synth-b", name="Synthetisches B")
    dienst.arbeitsort_anlegen(akteur, p, kennung="repo", name="Repository synth-b")
    return p


@pytest.fixture
def bindung(akteur, projekt):
    b, schluessel = dienst.bindung_anlegen(akteur, projekt, name="Chat A")
    b.klartext = schluessel
    return b


@pytest.fixture
def bindung_b(akteur, projekt_b):
    b, schluessel = dienst.bindung_anlegen(akteur, projekt_b, name="Chat B")
    b.klartext = schluessel
    return b


def arbeitspaket(
    akteur, projekt, titel="Anmeldefehler verständlich melden", umfang="src/auth/**", pruefer=None
):
    """Wunsch anlegen und zum freigabefähigen Arbeitspaket konkretisieren."""
    auftrag = dienst.wunsch_erfassen(
        akteur, projekt, titel=titel, beschreibung="Bitte verständlicher."
    )
    dienst.auftrag_bearbeiten(
        akteur,
        auftrag,
        titel=titel,
        beschreibung="Bitte verständlicher.",
        ziel="Falsches Passwort zeigt verständlichen Hinweis.",
        nicht_ziele="keine",
        arbeitsort=projekt.arbeitsorte.first(),
        schreibumfang=umfang,
        quellen="",
        abnahmekriterien="Hinweis nennt nächsten Schritt\nKein Fehlercode sichtbar",
        prioritaet="hoch",
        modul=projekt.module.first(),
    )
    dienst.pruefer_festlegen(akteur, auftrag, pruefer or akteur.benutzer)
    auftrag.refresh_from_db()
    return auftrag


@pytest.fixture
def freigegeben(akteur, projekt):
    auftrag = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, auftrag)
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.FREIGEGEBEN
    return auftrag

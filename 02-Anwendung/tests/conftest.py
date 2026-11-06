import pytest
from django.contrib.auth import get_user_model

from steuerung import dienst
from steuerung.models import Auftrag

PASSWORT = "nur-fuer-tests-123456"


@pytest.fixture(autouse=True)
def _kein_echtes_github(settings):
    """Tests rufen GitHub nie an: ohne Attrappe oder Nachbildung läuft der Abruf ins Leere."""
    settings.KIPM_GITHUB_API = "http://127.0.0.1:9"
    settings.KIPM_GITHUB_ZEIT = 1


@pytest.fixture
def owner(db):
    return get_user_model().objects.create_superuser(
        username="owner-test", password=PASSWORT, email=""
    )


@pytest.fixture
def zweiter_mensch(db):
    """Zweites menschliches Konto (normales Konto, kein Owner)."""
    return get_user_model().objects.create_user(username="zweiter-test", password=PASSWORT)


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


def arbeitspaket(akteur, projekt, titel="Anmeldefehler verständlich melden", umfang="src/auth/**"):
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
    auftrag.refresh_from_db()
    return auftrag


@pytest.fixture
def freigegeben(akteur, projekt):
    auftrag = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, auftrag)
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.FREIGEGEBEN
    return auftrag


# --- Werkbank (gemeinsame Bausteine der Werkbank-Tests) -------------------------------------


@pytest.fixture
def w_instanz(akteur, projekt):
    from steuerung import werkbank

    inst, schluessel = werkbank.instanz_anlegen(akteur, name="Werkbank PC", projekte=[projekt])
    inst.klartext = schluessel
    return inst


@pytest.fixture
def w_pbindung(w_instanz, projekt):
    from tests.werkbank_hilfen import bindung_sichern

    return bindung_sichern(
        instanz=w_instanz, projekt=projekt, arbeitsort=projekt.arbeitsorte.first()
    )


@pytest.fixture
def w_ausfuehrung(akteur, projekt, w_instanz, w_pbindung):
    """Ein freigegebener, zugewiesener und von der Werkbank übernommener Auftrag."""
    from steuerung import werkbank
    from steuerung.models import WerkbankAusfuehrung
    from tests import werkbank_hilfen as h

    auftrag = arbeitspaket(akteur, projekt)
    werkbank.zuweisen(akteur, auftrag, w_pbindung, freigeben=True)
    auftrag.refresh_from_db()
    n = h.uebernahme_nachricht(h.vor_quelle(w_instanz, w_pbindung, auftrag))
    werkbank.operation_ausfuehren(w_instanz, "uebernehmen", n)
    return WerkbankAusfuehrung.objects.get(auftrag=auftrag)

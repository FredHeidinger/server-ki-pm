"""Projektakte: projektbezogene Bedienung, getrennte Zustände, Konflikte, Betrieb, Verlauf.

Synthetische Projekte Alpha und Beta; keine echten Projektdaten. Die Kennungen P01 … beziehen
sich auf 01-Planung/ERWEITERUNG-PROJEKTAKTE.md, Abschnitt 11.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import Auftrag, Ereignis, Funktion, Installation, Nachweis
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


@pytest.fixture
def alpha(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="alpha", name="Alpha")
    dienst.arbeitsort_anlegen(akteur, p, kennung="repo", name="Repo Alpha")
    return p


@pytest.fixture
def beta(akteur):
    p = dienst.projekt_registrieren(akteur, kennung="beta", name="Beta")
    dienst.arbeitsort_anlegen(akteur, p, kennung="repo", name="Repo Beta")
    return p


def _url(name, projekt, **weitere):
    return reverse(name, kwargs={"kennung": projekt.kennung, **weitere})


BEREICHE = (
    "projekt_detail",
    "akte_funktionen",
    "projekt_auftraege",
    "akte_betrieb",
    "akte_wissen",
    "projekt_verlauf",
)


def test_p01_sechs_bereiche_und_projektgrenzen(owner_client, akteur, alpha, beta):
    akte.funktion_anlegen(akteur, alpha, titel="Alpha-Export")
    akte.funktion_anlegen(akteur, alpha, titel="Alpha-Import")
    akte.funktion_anlegen(akteur, beta, titel="Beta-Anzeige")
    dienst.wunsch_erfassen(akteur, alpha, titel="Alpha-Auftrag", beschreibung="A")
    akte.text_anlegen(akteur, alpha, titel="Alpha-Konzept", text="geheim alpha")

    auswahl = owner_client.get(reverse("start")).content.decode()
    assert "Alpha" in auswahl and "Beta" in auswahl
    for name in BEREICHE:
        seite = owner_client.get(_url(name, beta))
        assert seite.status_code == 200, name
        text = seite.content.decode()
        for bereich in (
            "Überblick",
            "Funktionen &amp; Planung",
            "Arbeitsaufträge",
            "Versionen &amp; Betrieb",
            "Wissen &amp; Entscheidungen",
            "Verlauf",
        ):
            assert bereich in text, (name, bereich)
        assert "Alpha-Export" not in text and "Alpha-Auftrag" not in text, name
        assert "Alpha-Konzept" not in text and "Projekt wechseln" in text
    suche = owner_client.get(_url("akte_suche", beta) + "?q=Alpha").content.decode()
    assert "Nichts gefunden" in suche
    assert (
        "Alpha-Export" in owner_client.get(_url("akte_suche", alpha) + "?q=Export").content.decode()
    )
    kontext = owner_client.get(_url("akte_kontext", beta)).content.decode()
    assert "Beta-Anzeige" in kontext and "Alpha" not in kontext.split("Teil 1 von")[1]
    # Manipulierte Nummern: Beta hat nur F-1; F-2 von Alpha ist dort nicht vorhanden.
    assert owner_client.get(_url("funktion_detail", beta, nummer=2)).status_code == 404
    assert (
        "Beta-Anzeige" in owner_client.get(_url("funktion_detail", beta, nummer=1)).content.decode()
    )
    auftrag = Auftrag.objects.get(titel="Alpha-Auftrag")
    assert (
        owner_client.get(_url("akte_kontext", beta) + f"?auftrag={auftrag.pk}").status_code == 404
    )


def test_p02_ohne_module_bedienbar_und_filter_nach_nachtrag(owner_client, akteur, alpha):
    a = dienst.wunsch_erfassen(akteur, alpha, titel="Erster Auftrag", beschreibung="B")
    akte.funktion_anlegen(akteur, alpha, titel="Erste Funktion")
    for name in (*BEREICHE, "projekt_einstellungen", "funktion_neu", "unterlage_neu"):
        seite = owner_client.get(_url(name, alpha)).content.decode()
        assert "Nach Modul filtern" not in seite, name
    dienst.modul_anlegen(akteur, alpha, kennung="kern", name="Kern")
    board = owner_client.get(_url("projekt_auftraege", alpha)).content.decode()
    assert "Nach Modul filtern" in board and "Erster Auftrag" in board
    funktionen = owner_client.get(_url("akte_funktionen", alpha)).content.decode()
    assert "Gesamtes Projekt" in funktionen and "Erste Funktion" in funktionen
    a.refresh_from_db()
    assert a.modul is None


def test_p03_alte_links_fuehren_zum_richtigen_projekt(owner_client, akteur, alpha, beta):
    a = dienst.wunsch_erfassen(akteur, beta, titel="Beta-Auftrag", beschreibung="B")
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})
    ).content.decode()
    assert "Beta-Auftrag" in seite and '<p class="projektname-nav bereichslink">Beta</p>' in seite
    weiter = owner_client.get(reverse("auftraege"), follow=True).content.decode()
    assert "im jeweiligen Projekt" in weiter and "Projekt wählen" in weiter
    alt = owner_client.get(_url("projekt_entscheidungen", beta))
    assert alt.status_code == 302 and alt["Location"].startswith(_url("akte_wissen", beta))


def test_p04_erledigter_auftrag_macht_funktion_nicht_fertig(akteur, alpha, zweiter_mensch):
    f = akte.funktion_anlegen(akteur, alpha, titel="Unterlagen zuordnen", planung="vorgesehen")
    eins = arbeitspaket(akteur, alpha, titel="Teil eins", umfang="a/**", pruefer=zweiter_mensch)
    zwei = arbeitspaket(akteur, alpha, titel="Teil zwei", umfang="b/**", pruefer=zweiter_mensch)
    for a in (eins, zwei):
        akte.funktion_auftrag(akteur, f, a, verbinden=True)
        dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(akteur, eins, eins.revision_nr)
    eins.refresh_from_db()
    ergebnis = dienst.ergebnis_einreichen(
        akteur,
        eins,
        eins.revision_nr,
        zusammenfassung="fertig",
        dateien="a/x.py@1111111",
        pruefungen="pytest: 3 bestanden",
    )
    eins.refresh_from_db()
    assert eins.zustand == Auftrag.ERLEDIGT
    pruefer = dienst.Akteur.aus_benutzer(zweiter_mensch)
    stand = akte.funktionsstand(f)
    assert stand["abnahme"] == "offen" and not stand["nutzbar"] and stand["version"] == ""
    assert not Installation.objects.exists() and not Nachweis.objects.exists()
    # Erst die ausdrückliche Übernahme macht das Ergebnis zum Umsetzungsnachweis.
    akte.ergebnis_als_nachweis(akteur, ergebnis, f)
    akte.nachweis_melden(
        akteur,
        alpha,
        art="test",
        ergebnis="bestanden",
        version="1111111",
        quelle="pytest",
        umfang="Zuordnen",
        funktion=f,
    )
    stand = akte.funktionsstand(f)
    assert stand["version"] == "1111111" and stand["pruefungen"] and stand["abnahme"] == "offen"
    assert "noch nicht auf Server geprüft" in stand["satz"]
    # Neuer Commit: alte grüne Prüfung gilt nicht.
    akte.nachweis_melden(
        akteur,
        alpha,
        art="umsetzung",
        ergebnis="gemeldet",
        version="2222222",
        quelle="Commit 2222222",
        funktion=f,
    )
    stand = akte.funktionsstand(f)
    assert stand["version"] == "2222222" and stand["pruefungen"] == []
    assert "nicht geprüft" in stand["satz"] and len(stand["aeltere"]) == 1
    # Abnahme nur durch einen Menschen, der die Umsetzung nicht gemeldet hat.
    with pytest.raises(dienst.KeinZugriff, match="Selbstabnahme"):
        akte.funktion_abnehmen(akteur, f, version="2222222", angenommen=True, kriterien="alles")
    akte.funktion_abnehmen(pruefer, f, version="2222222", angenommen=True, kriterien="alles")
    assert akte.funktionsstand(f)["abnahme"] == "angenommen"


def test_p05_zweite_speicherung_derselben_revision_erhaelt_konflikt(owner_client, akteur, alpha):
    f = akte.funktion_anlegen(akteur, alpha, titel="Original")
    akte.funktion_bearbeiten(akteur, f, erwartete_revision=1, titel="Erster Browser")
    with pytest.raises(dienst.Konflikt, match="inzwischen geändert"):
        akte.funktion_bearbeiten(akteur, f, erwartete_revision=1, titel="Zweiter Browser")
    f.refresh_from_db()
    assert f.titel == "Erster Browser" and f.revision == 2
    antwort = owner_client.post(
        _url("funktion_aktion", alpha, nummer=f.nummer, aktion="bearbeiten"),
        {
            "f-revision": 1,
            "f-titel": "Dritter",
            "f-planung": "idee",
            "f-nutzen": "",
            "f-kriterien": "",
        },
        follow=True,
    )
    assert "inzwischen geändert" in antwort.content.decode()
    f.refresh_from_db()
    assert f.titel == "Erster Browser"
    akte.profil_bearbeiten(akteur, alpha, erwartete_revision=0, zweck="Erster Zweck")
    antwort = owner_client.post(
        _url("akte_profil", alpha),
        {
            "revision": 0,
            "zweck": "Überschrieben?",
            "zielgruppe": "",
            "nicht_ziele": "",
            "phase": "",
            "entwicklungsstufe": "",
        },
    )
    assert "inzwischen geändert" in antwort.content.decode()
    alpha.refresh_from_db()
    assert alpha.zweck == "Erster Zweck"


def test_ki_schlaegt_nur_vor(akteur, alpha, bindung_alpha):
    ki = dienst.Akteur.aus_bindung(bindung_alpha)
    f = akte.funktion_anlegen(ki, alpha, titel="KI-Idee", planung="vorgesehen")
    assert f.planung == Funktion.IDEE
    with pytest.raises(dienst.KeinZugriff, match="Mensch"):
        akte.funktion_bearbeiten(ki, f, erwartete_revision=1, planung="vorgesehen")
    with pytest.raises(dienst.KeinZugriff):
        akte.profil_bearbeiten(ki, alpha, erwartete_revision=0, zweck="KI-Zweck")
    u = akte.text_anlegen(ki, alpha, titel="KI-Entwurf", text="Vorschlag", rolle="grundlage")
    v = u.versionen.get()
    assert v.rolle == "entwurf" and not u.ki_freigegeben
    with pytest.raises(dienst.KeinZugriff):
        akte.rolle_setzen(ki, v, "grundlage")


@pytest.fixture
def bindung_alpha(akteur, alpha):
    b, schluessel = dienst.bindung_anlegen(akteur, alpha, name="Chat Alpha")
    b.klartext = schluessel
    return b


def test_p16_umgebungen_komponenten_teilupdate_und_http(owner_client, akteur, alpha):
    test = akte.umgebung_anlegen(akteur, alpha, name="Testserver", art="test")
    betrieb = akte.umgebung_anlegen(akteur, alpha, name="Betrieb", art="produktion")
    with pytest.raises(dienst.DienstFehler, match="Zugangsdaten"):
        akte.umgebung_anlegen(akteur, alpha, name="X", art="test", url="https://u:p@host/")
    for u in (test, betrieb):
        for komponente in ("Anwendung", "Worker"):
            akte.installation_melden(
                akteur,
                alpha,
                umgebung=u,
                komponente=komponente,
                version="v1",
                herkunft="Aktualisierungszeile",
            )
    akte.installation_melden(
        akteur,
        alpha,
        umgebung=test,
        komponente="Anwendung",
        version="v2",
        herkunft="Aktualisierungszeile",
    )
    stand = {
        (z["i"].umgebung.name, z["i"].komponente): z["i"].version
        for z in akte.installationen_aktuell(alpha)
    }
    assert stand == {
        ("Testserver", "Anwendung"): "v2",
        ("Testserver", "Worker"): "v1",
        ("Betrieb", "Anwendung"): "v1",
        ("Betrieb", "Worker"): "v1",
    }
    ersetzt = Installation.objects.get(umgebung=test, komponente="Anwendung", version="v1")
    assert ersetzt.zustand == Installation.ERSETZT
    assert Installation.objects.get(version="v2").vorgaenger == ersetzt
    # Zurückrollen ist ein neuer Vorgang mit Rückbezug.
    zurueck = akte.installation_melden(
        akteur, alpha, umgebung=test, komponente="Anwendung", version="v1", herkunft="Rücknahme"
    )
    assert zurueck.vorgaenger.version == "v2" and Installation.objects.count() == 6
    f = akte.funktion_anlegen(akteur, alpha, titel="Anmeldung")
    akte.nachweis_melden(
        akteur,
        alpha,
        art="umsetzung",
        ergebnis="gemeldet",
        version="v1",
        quelle="Commit",
        funktion=f,
    )
    akte.nachweis_melden(
        akteur,
        alpha,
        art="betrieb",
        ergebnis="erreichbar",
        version="v1",
        quelle="curl 200",
        umfang="HTTP",
        umgebung=betrieb,
        funktion=f,
    )
    stand = akte.funktionsstand(f)
    assert not stand["nutzbar"] and "nur erreichbar" in stand["satz"]
    seite = owner_client.get(_url("akte_betrieb", alpha)).content.decode()
    assert "Testserver" in seite and "Worker" in seite and "gemeldet" in seite


def test_p17_rueckdatiert_tabs_und_historie(
    owner_client, owner, akteur, alpha, bindung_alpha, client
):
    test = akte.umgebung_anlegen(akteur, alpha, name="Testserver", art="test")
    gestern = timezone.now() - timedelta(days=1)
    akte.installation_melden(
        akteur,
        alpha,
        umgebung=test,
        komponente="App",
        version="v1",
        installiert_am=gestern,
        herkunft="nachgetragen",
    )
    e = Ereignis.objects.filter(projekt=alpha, objekt_typ="Installation").first()
    assert e.beobachtet_am == gestern and e.zeitpunkt > gestern
    verlauf = owner_client.get(_url("projekt_verlauf", alpha)).content.decode()
    assert "geschehen" in verlauf

    seite = owner_client.get(_url("projekt_detail", alpha)).content.decode()
    grenze_tab1 = akte.obere_grenze(alpha)
    assert f'name="grenze" value="{grenze_tab1}"' in seite
    f = akte.funktion_anlegen(akteur, alpha, titel="Neu während Tab offen")
    owner_client.post(_url("akte_gesehen", alpha), {"grenze": grenze_tab1})
    neu = akte.aenderungen_seit_besuch(owner, alpha, akte.obere_grenze(alpha))
    assert neu["anzahl"] == 1 and neu["ereignisse"][0].objekt_kennung == f.kennung
    # Ein zweiter Tab mit älterer Grenze senkt die Marke nicht.
    owner_client.post(_url("akte_gesehen", alpha), {"grenze": grenze_tab1 - 3})
    assert akte.aenderungen_seit_besuch(owner, alpha, akte.obere_grenze(alpha))["anzahl"] == 1
    # Lesen durch einen Chat ändert weder Marke noch Inhaltsrevision.
    alpha.refresh_from_db()
    revision = alpha.inhalt_revision
    from tests.test_mcp import _werkzeug

    fehler, _ = _werkzeug(client, bindung_alpha.klartext, "kipm_projektakte")
    assert not fehler
    alpha.refresh_from_db()
    assert alpha.inhalt_revision == revision
    assert akte.aenderungen_seit_besuch(owner, alpha, akte.obere_grenze(alpha))["anzahl"] == 1

    akte.funktion_bearbeiten(akteur, f, erwartete_revision=1, titel="Umbenannt")
    alt = Ereignis.objects.filter(objekt_kennung=f.kennung).order_by("id").first()
    stand = owner_client.get(_url("verlauf_stand", alpha, ereignis_id=alt.pk)).content.decode()
    assert "Neu während Tab offen" in stand and "Umbenannt" not in stand
    neu_e = Ereignis.objects.filter(objekt_kennung=f.kennung).order_by("id").last()
    stand = owner_client.get(_url("verlauf_stand", alpha, ereignis_id=neu_e.pk)).content.decode()
    assert "Umbenannt" in stand and "Neu während Tab offen" in stand


def test_inhaltsrevision_steigt_nur_bei_fachlicher_aenderung(owner, akteur, alpha):
    alpha.refresh_from_db()
    vorher = alpha.inhalt_revision
    akte.suche(alpha, "x")
    akte.als_gesehen_markieren(owner, alpha, akte.obere_grenze(alpha))
    alpha.refresh_from_db()
    assert alpha.inhalt_revision == vorher
    akte.funktion_anlegen(akteur, alpha, titel="Etwas")
    alpha.refresh_from_db()
    assert alpha.inhalt_revision == vorher + 1


def test_funktion_ueber_oberflaeche_anlegen_verknuepfen_nachweisen(owner_client, akteur, alpha):
    antwort = owner_client.post(
        _url("funktion_neu", alpha),
        {
            "titel": "Bericht drucken",
            "nutzen": "Für Papier",
            "kriterien_eintrag": ["A4", ""],
            "planung": "vorgesehen",
        },
    )
    assert antwort.status_code == 302
    f = Funktion.objects.get(projekt=alpha)
    assert f.kriterien == "A4" and f.planung == "vorgesehen"
    a = dienst.wunsch_erfassen(akteur, alpha, titel="Druckfunktion", beschreibung="B")
    owner_client.post(
        _url("funktion_aktion", alpha, nummer=f.nummer, aktion="verknuepfen"), {"auftrag": a.pk}
    )
    assert list(f.auftraege.all()) == [a]
    fremd = dienst.projekt_registrieren(akteur, kennung="gamma", name="Gamma")
    fremd_auftrag = dienst.wunsch_erfassen(akteur, fremd, titel="Fremd", beschreibung="B")
    owner_client.post(
        _url("funktion_aktion", alpha, nummer=f.nummer, aktion="verknuepfen"),
        {"auftrag": fremd_auftrag.pk},
    )
    assert list(f.auftraege.all()) == [a]
    with pytest.raises(dienst.KeinZugriff):
        akte.funktion_auftrag(akteur, f, fremd_auftrag, verbinden=True)
    antwort = owner_client.post(
        _url("nachweis_neu", alpha),
        {
            "n-art": "test",
            "n-ergebnis": "bestanden",
            "n-version": "abc1234",
            "n-quelle": "pytest",
            "n-umfang": "Drucken",
            "n-funktion": f.pk,
            "zurueck": _url("funktion_detail", alpha, nummer=f.nummer),
        },
    )
    assert antwort["Location"].endswith(f"/funktionen/{f.nummer}/")
    seite = owner_client.get(_url("funktion_detail", alpha, nummer=f.nummer)).content.decode()
    assert "abc1234" in seite and "Bericht drucken" in seite


def test_p19_migration_erhaelt_bestand(transactional_db):
    """Migration 0004 → 0005 auf einem Datenstand: IDs, Revisionen, Akteure, Übernahmen,
    Ergebnisse, Abnahmen und Ereignisse bleiben unverändert (Test-DB, keine Laufdaten)."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("steuerung", "0004_auftrag_typ")])
    alt = executor.loader.project_state([("steuerung", "0004_auftrag_typ")]).apps
    User = alt.get_model("auth", "User")
    Projekt = alt.get_model("steuerung", "Projekt")
    AuftragAlt = alt.get_model("steuerung", "Auftrag")
    Revision = alt.get_model("steuerung", "Revision")
    ErgebnisAlt = alt.get_model("steuerung", "Ergebnis")
    PruefungAlt = alt.get_model("steuerung", "Pruefung")
    EreignisAlt = alt.get_model("steuerung", "Ereignis")
    mensch = User.objects.create(username="pruefer-alt", is_active=True)
    p = Projekt.objects.create(kennung="bestand", name="Bestand")
    a = AuftragAlt.objects.create(
        projekt=p,
        titel="Alt",
        zustand="ABGENOMMEN",
        revision_nr=3,
        bearbeiter_name="",
        uebernommene_revision=3,
        abgenommene_revision=3,
        letzter_bearbeiter_name="Chat X",
    )
    Revision.objects.create(auftrag=a, nummer=3, ziel="Z", erstellt_von_name="fred")
    e = ErgebnisAlt.objects.create(
        auftrag=a, revision_nr=3, zusammenfassung="S", dateien="a@1", eingereicht_von_name="Chat X"
    )
    pr = PruefungAlt.objects.create(ergebnis=e, pruefer=mensch, entscheidung="ABGENOMMEN")
    ev = EreignisAlt.objects.create(
        akteur_name="fred",
        projekt=p,
        objekt_typ="Auftrag",
        objekt_kennung=f"A-{a.pk}",
        ereignis="Abgenommen",
        revision="3",
    )
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    from steuerung.models import Auftrag as AuftragNeu
    from steuerung.models import Ereignis as EreignisNeu
    from steuerung.models import Projekt as ProjektNeu
    from steuerung.models import Pruefung as PruefungNeu

    neu = AuftragNeu.objects.get(pk=a.pk)
    assert (
        neu.titel,
        neu.zustand,
        neu.revision_nr,
        neu.abgenommene_revision,
        neu.letzter_bearbeiter_name,
    ) == ("Alt", "ERLEDIGT", 3, 3, "Chat X")  # 0008 überführt Abgenommen in Erledigt
    assert (
        neu.ergebnisse.get().pk == e.pk
        and PruefungNeu.objects.get(pk=pr.pk).pruefer_id == mensch.pk
    )
    ereignis = EreignisNeu.objects.get(pk=ev.pk)
    assert (
        ereignis.ereignis == "Abgenommen"
        and ereignis.beobachtet_am is None
        and ereignis.version is None
    )
    projekt = ProjektNeu.objects.get(pk=p.pk)
    # Inhaltsrevision 1: Migration 0008 hält die Umstellung Abgenommen → Erledigt im Verlauf fest.
    assert projekt.zweck == "" and projekt.inhalt_revision == 1 and not projekt.funktionen.exists()

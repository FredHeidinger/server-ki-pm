"""Projektbezogene Bedienung und Bestand über Migrationen.

Synthetische Projekte Alpha und Beta; keine echten Projektdaten.
"""

import pytest
from django.urls import reverse

from steuerung import akte, dienst

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
    "projekt_auftraege",
    "akte_wissen",
    "projekt_verlauf",
)


def test_p01_vier_bereiche_und_projektgrenzen(owner_client, akteur, alpha, beta):
    dienst.wunsch_erfassen(akteur, alpha, titel="Alpha-Auftrag", beschreibung="A")
    akte.text_anlegen(akteur, alpha, titel="Alpha-Konzept", text="geheim alpha")

    auswahl = owner_client.get(reverse("start")).content.decode()
    assert "Alpha" in auswahl and "Beta" in auswahl
    for name in BEREICHE:
        seite = owner_client.get(_url(name, beta))
        assert seite.status_code == 200, name
        text = seite.content.decode()
        leiste = text.split('<nav class="seitenleiste"')[1].split("</nav>")[0]
        for bereich in ("Überblick", "Aufträge", "Wissen", "Verlauf"):
            assert f"{bereich}</a>" in leiste, (name, bereich)
        for frueher in ("Funktionen &amp; Planung", "Versionen &amp; Betrieb", "Arbeitsaufträge"):
            assert frueher not in leiste, (name, frueher)
        assert "Alpha-Auftrag" not in text, name
        assert "Alpha-Konzept" not in text and "Projekt wechseln" in text
    # Manipulierte Nummern: Beta hat keinen U-1; der Text von Alpha ist dort nicht vorhanden.
    assert owner_client.get(_url("unterlage_detail", beta, nummer=1)).status_code == 404


@pytest.fixture
def bindung_alpha(akteur, alpha):
    b, schluessel = dienst.bindung_anlegen(akteur, alpha, name="Chat Alpha")
    b.klartext = schluessel
    return b


def test_inhaltsrevision_steigt_nur_bei_fachlicher_aenderung(owner_client, akteur, alpha):
    alpha.refresh_from_db()
    vorher = alpha.inhalt_revision
    owner_client.get(_url("projekt_detail", alpha))
    akte.kontext_erzeugen(akteur, alpha)
    alpha.refresh_from_db()
    assert alpha.inhalt_revision == vorher
    akte.text_anlegen(akteur, alpha, titel="Etwas", text="x")
    alpha.refresh_from_db()
    assert alpha.inhalt_revision == vorher + 1


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

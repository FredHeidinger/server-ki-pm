"""Schutz vor dem Fehler „Spalte fehlt auf dem Server“: Modelle und Migrationen müssen übereinstimmen."""

from pathlib import Path

import pytest
from django.core.management import call_command

MIGRATIONEN = Path(__file__).resolve().parents[1] / "steuerung" / "migrations"


@pytest.mark.django_db
def test_keine_fehlende_migration():
    # Wirft SystemExit, wenn Modelländerungen ohne Migration vorliegen.
    call_command("makemigrations", "steuerung", "--check", "--dry-run", verbosity=0)


def test_anfangsmigration_ist_nicht_neu_erzeugt():
    """0001 ist auf dem Server angewendet; Änderungen gehören in Folgemigrationen."""
    anfang = (MIGRATIONEN / "0001_initial.py").read_text(encoding="utf-8")
    assert "repository" not in anfang and "letzter_ordner" not in anfang
    assert (MIGRATIONEN / "0002_zugaenge_verbindungen.py").exists()


def test_migration_nummern_je_projekt(transactional_db):
    """0014 → Folgestand: Festgelegte Kürzel greifen, übrige werden gebildet und sind eindeutig;
    jedes Projekt zählt in der bisherigen Reihenfolge lückenlos, die Zählung läuft danach weiter."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("steuerung", "0014_meldung_sitzungsende")])
    alt = executor.loader.project_state([("steuerung", "0014_meldung_sitzungsende")]).apps
    Projekt = alt.get_model("steuerung", "Projekt")
    Auftrag = alt.get_model("steuerung", "Auftrag")
    nexus = Projekt.objects.create(kennung="nexus", name="Nexus")
    kipm = Projekt.objects.create(kennung="server-ki-pm", name="Server-KI-PM")
    ein = Projekt.objects.create(kennung="nextstep", name="Nextstep", aktiv=False)
    zwei = Projekt.objects.create(kennung="next-level", name="Next Level")
    ohne = Projekt.objects.create(kennung="leer", name="Leer")
    a1 = Auftrag.objects.create(projekt=nexus, titel="n1", revision_nr=1)
    b1 = Auftrag.objects.create(projekt=kipm, titel="k1", revision_nr=1)
    a2 = Auftrag.objects.create(projekt=nexus, titel="n2", revision_nr=1)
    c1 = Auftrag.objects.create(projekt=ein, titel="e1", revision_nr=1)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    from steuerung import dienst
    from steuerung.models import Auftrag as AuftragNeu
    from steuerung.models import Projekt as ProjektNeu

    assert ProjektNeu.objects.get(pk=nexus.pk).kuerzel == "NEX"
    assert ProjektNeu.objects.get(pk=kipm.pk).kuerzel == "KIPM"
    kuerzel = [p.kuerzel for p in ProjektNeu.objects.all()]
    assert all(kuerzel) and len(set(kuerzel)) == len(kuerzel)
    assert AuftragNeu.objects.get(pk=a1.pk).nummer == "NEX-1"
    assert AuftragNeu.objects.get(pk=a2.pk).nummer == "NEX-2"
    assert AuftragNeu.objects.get(pk=b1.pk).nummer == "KIPM-1"
    assert AuftragNeu.objects.get(pk=c1.pk).projekt_nummer == 1
    assert dienst.auftrag_id_aus_nummer("NEX-2") == a2.pk
    assert dienst.auftrag_id_aus_nummer(f"A-{a2.pk}") == a2.pk
    assert ProjektNeu.objects.get(pk=nexus.pk).naechste_nummer == 3
    assert ProjektNeu.objects.get(pk=ohne.pk).naechste_nummer == 1
    assert ProjektNeu.objects.get(pk=zwei.pk).kuerzel


def test_migration_0006_erhaelt_bestand_und_bestaetigte_orte(transactional_db):
    """0005 → 0006: vorhandene Arbeitsorte und Umgebungen bleiben bestätigt (sonst ordnete der
    Server nach dem Update keinen Ordner mehr zu), Module hängen am Projekt, IDs bleiben."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("steuerung", "0005_projektakte")])
    alt = executor.loader.project_state([("steuerung", "0005_projektakte")]).apps
    Projekt = alt.get_model("steuerung", "Projekt")
    p = Projekt.objects.create(kennung="bestand", name="Bestand", profil_revision=2)
    m = alt.get_model("steuerung", "Modul").objects.create(projekt=p, kennung="kern", name="Kern")
    ort = alt.get_model("steuerung", "Arbeitsort").objects.create(
        projekt=p, kennung="repo", name="Repo", pfad=r"W:\Bestand"
    )
    u = alt.get_model("steuerung", "Umgebung").objects.create(
        projekt=p, kennung="betrieb", name="Betrieb", art="produktion"
    )
    r = alt.get_model("steuerung", "Release").objects.create(projekt=p, kennung="1.0")
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    from steuerung import dienst
    from steuerung.models import Arbeitsort, Modul, Release, Umgebung
    from steuerung.models import Projekt as ProjektNeu

    assert ProjektNeu.objects.get(pk=p.pk).profil_revision == 2
    assert ProjektNeu.objects.get(pk=p.pk).profil_entwurf == {}
    neu_ort = Arbeitsort.objects.get(pk=ort.pk)
    assert (neu_ort.bestaetigt, neu_ort.revision) == (True, 1)
    assert dienst.projekt_fuer_ordner(r"W:\Bestand\src")[1] == neu_ort
    assert Umgebung.objects.get(pk=u.pk).bestaetigt is True
    assert Modul.objects.get(pk=m.pk).eltern is None
    assert Release.objects.get(pk=r.pk).archiviert is False

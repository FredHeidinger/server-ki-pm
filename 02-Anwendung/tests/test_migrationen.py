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

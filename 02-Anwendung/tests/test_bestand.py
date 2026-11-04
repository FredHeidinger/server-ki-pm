"""Probemigration in installieren.sh: Bestand darf nicht verloren gehen, der Verlauf darf wachsen."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

SKRIPT = Path(__file__).resolve().parents[2] / "03-Server" / "skripte" / "bestand.py"
_spec = importlib.util.spec_from_file_location("bestand", SKRIPT)
assert _spec is not None and _spec.loader is not None
bestand = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bestand)


def _datenbank(pfad: Path, ereignisse: int) -> Path:
    with sqlite3.connect(pfad) as z:
        for t in bestand.TABELLEN:
            z.execute(f"create table steuerung_{t} (id integer primary key)")
        z.executemany("insert into steuerung_auftrag (id) values (?)", [(1,), (2,)])
        z.executemany(
            "insert into steuerung_ereignis (id) values (?)", [(i,) for i in range(ereignisse)]
        )
    return pfad


def test_zaehlen_in_fester_reihenfolge(tmp_path):
    db = _datenbank(tmp_path / "k.sqlite3", 56)
    zahlen = bestand.zaehlen(db)
    assert zahlen[bestand.TABELLEN.index("auftrag")] == 2
    assert zahlen[bestand.TABELLEN.index("ereignis")] == 56


def _zahlen(**werte):
    return [werte.get(t, 3) for t in bestand.TABELLEN]


def test_verlauf_darf_wachsen_sonst_nichts():
    # Fall vom Server-Update mit Migration 0008: zwei Umstellungen im Verlauf.
    vorher = _zahlen(ereignis=56, auftrag=9)
    assert bestand.ohne_verlust(vorher, _zahlen(ereignis=58, auftrag=9))
    assert bestand.ohne_verlust(vorher, vorher)
    assert not bestand.ohne_verlust(vorher, _zahlen(ereignis=55, auftrag=9))
    assert not bestand.ohne_verlust(vorher, _zahlen(ereignis=58, auftrag=8))
    assert not bestand.ohne_verlust(vorher, _zahlen(ereignis=56, auftrag=10))


def test_tabellen_frueherer_stufen_und_texte_zaehlen_mit():
    # A-8: Texte und Daten ohne Code dürfen bei einem Update ebenso wenig verschwinden.
    vorher = _zahlen()
    for tabelle in ("unterlage", "unterlagenversion", "funktion", "nachweis", "externeaktion"):
        assert not bestand.ohne_verlust(vorher, _zahlen(**{tabelle: 2})), tabelle


def test_alle_gezaehlten_tabellen_gibt_es_im_modell():
    from django.apps import apps

    tabellen = {m._meta.db_table for m in apps.get_app_config("steuerung").get_models()}
    assert {f"steuerung_{t}" for t in bestand.TABELLEN} <= tabellen


def test_fehlende_tabelle_zaehlt_null(tmp_path):
    db = tmp_path / "alt.sqlite3"
    with sqlite3.connect(db) as z:
        z.execute("create table steuerung_auftrag (id integer primary key)")
        z.execute("insert into steuerung_auftrag (id) values (1)")
    zahlen = bestand.zaehlen(db)
    assert zahlen[bestand.TABELLEN.index("auftrag")] == 1
    assert zahlen[bestand.TABELLEN.index("funktion")] == 0


def test_kommandozeile_wie_in_installieren_sh(tmp_path):
    vorher = tmp_path / "vorher.sqlite3"
    nachher = tmp_path / "nachher.sqlite3"
    _datenbank(vorher, 56)
    _datenbank(nachher, 58)

    def rufe(*args):
        # Wie auf dem Server mit UTF-8; eine Windows-Konsole (cp1252) kann „→“ nicht ausgeben.
        return subprocess.run(
            [sys.executable, str(SKRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )

    v = rufe("zaehlen", str(vorher)).stdout.strip()
    n = rufe("zaehlen", str(nachher)).stdout.strip()
    assert rufe("vergleichen", v, n).returncode == 0
    weniger = rufe("vergleichen", n, v)
    assert weniger.returncode == 4 and "ereignis" in weniger.stdout
    skript = (SKRIPT.parent / "installieren.sh").read_text(encoding="utf-8")
    assert '"$SKRIPTE/bestand.py" vergleichen' in skript

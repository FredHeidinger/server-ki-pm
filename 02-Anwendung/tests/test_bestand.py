"""Probemigration in installieren.sh: Bestand darf nicht verloren gehen, der Verlauf darf wachsen."""

import importlib.util
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


def test_verlauf_darf_wachsen_sonst_nichts():
    # Fall vom Server-Update mit Migration 0008: zwei Umstellungen im Verlauf.
    assert bestand.ohne_verlust([3, 9, 21, 2, 2, 56, 4], [3, 9, 21, 2, 2, 58, 4])
    assert bestand.ohne_verlust([3, 9, 21, 2, 2, 56, 4], [3, 9, 21, 2, 2, 56, 4])
    assert not bestand.ohne_verlust([3, 9, 21, 2, 2, 56, 4], [3, 9, 21, 2, 2, 55, 4])
    assert not bestand.ohne_verlust([3, 9, 21, 2, 2, 56, 4], [3, 8, 21, 2, 2, 58, 4])
    assert not bestand.ohne_verlust([3, 9, 21, 2, 2, 56, 4], [3, 10, 21, 2, 2, 56, 4])


def test_kommandozeile_wie_in_installieren_sh(tmp_path):
    vorher = tmp_path / "vorher.sqlite3"
    nachher = tmp_path / "nachher.sqlite3"
    _datenbank(vorher, 56)
    _datenbank(nachher, 58)

    def rufe(*args):
        return subprocess.run(
            [sys.executable, str(SKRIPT), *args], capture_output=True, text=True, check=False
        )

    v = rufe("zaehlen", str(vorher)).stdout.strip()
    n = rufe("zaehlen", str(nachher)).stdout.strip()
    assert rufe("vergleichen", v, n).returncode == 0
    weniger = rufe("vergleichen", n, v)
    assert weniger.returncode == 4 and "ereignis" in weniger.stdout
    skript = (SKRIPT.parent / "installieren.sh").read_text(encoding="utf-8")
    assert '"$SKRIPTE/bestand.py" vergleichen' in skript

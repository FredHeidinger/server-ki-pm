"""Spiegelerzeugung: nur freigegebene Bereiche, keine Heimnetzadressen oder Schlüssel."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SKRIPT = Path(__file__).resolve().parents[2] / "03-Server" / "skripte" / "spiegel.py"
_spec = importlib.util.spec_from_file_location("spiegel", SKRIPT)
assert _spec is not None and _spec.loader is not None
spiegel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spiegel)

# Testwerte zusammengesetzt, damit diese Datei die Spiegelprüfung selbst besteht.
FREMDE_ADRESSE = ".".join(["192", "168", "1", "20"])
CGNAT_ADRESSE = ".".join(["100", "101", "7", "3"])
MAC = ":".join(["aa", "bb", "cc", "dd", "ee", "0f"])
SCHLUESSEL = "kipm_" + "abc123" + "_" + "x" * 32


def _datei(text: str) -> tuple[str, bytes]:
    return ("100644", text.encode("utf-8"))


def test_pruefen_meldet_lecks_und_laesst_serveradresse_zu():
    dateien = {
        "02-Anwendung/ok.py": _datei(
            "url = https://192.168.254.157/\nhost = 127.0.0.1\nv = 1.2.3.4"
        ),
        "02-Anwendung/heim.py": _datei(f"a = '{FREMDE_ADRESSE}'"),
        "02-Anwendung/vpn.py": _datei(f"a = '{CGNAT_ADRESSE}'"),
        "02-Anwendung/mac.txt": _datei(f"Karte {MAC}"),
        "02-Anwendung/key.txt": _datei(f"Bearer {SCHLUESSEL}"),
        "02-Anwendung/daten/kipm.sqlite3": _datei(""),
        "02-Anwendung/daten/.secret_key": _datei(""),
    }
    befunde = "\n".join(spiegel.pruefen(dateien))
    assert "ok.py" not in befunde
    for pfad in ("heim.py:1", "vpn.py:1", "mac.txt:1", "key.txt:1", "kipm.sqlite3", ".secret_key"):
        assert pfad in befunde


def test_echter_stand_ist_veroeffentlichbar():
    haupt = SKRIPT.parents[2]
    if not (haupt / "01-Planung").exists():
        pytest.skip("nur im Hauptrepository")
    dateien = spiegel.dateien_lesen(haupt, "HEAD")
    assert spiegel.pruefen(dateien) == []
    assert all(p.startswith(spiegel.BEREICHE) or p in ("README.md", ".gitignore") for p in dateien)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _schreiben(wurzel: Path, dateien: dict[str, str]):
    for pfad, text in dateien.items():
        (wurzel / pfad).parent.mkdir(parents=True, exist_ok=True)
        (wurzel / pfad).write_text(text, encoding="utf-8")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for name in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{name}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "test@example.invalid")
    fern = tmp_path / "fern.git"
    _git("init", "-q", "--bare", "-b", "main", str(fern), cwd=tmp_path)
    alt = tmp_path / "alt"
    _git("clone", "-q", str(fern), str(alt), cwd=tmp_path)
    _schreiben(alt, {"veraltet.txt": "weg"})
    _git("add", "-A", cwd=alt)
    _git("commit", "-q", "-m", "alt", cwd=alt)
    _git("push", "-q", "origin", "main", cwd=alt)

    haupt = tmp_path / "haupt"
    haupt.mkdir()
    _git("init", "-q", "-b", "main", cwd=haupt)
    _schreiben(
        haupt,
        {
            "01-Planung/GEHEIM.md": "privat",
            "02-Anwendung/app.py": "print('hallo')\n",
            "03-Server/INSTALLATION.md": "privat",
            "03-Server/skripte/install.sh": "echo ok\n",
            "03-Server/spiegel/README.md": "# Spiegel\n",
            "03-Server/spiegel/gitignore": "daten/\n",
        },
    )
    _git("add", "-A", cwd=haupt)
    _git("commit", "-q", "-m", "Erster Stand", cwd=haupt)
    return haupt, fern, tmp_path


def _inhalt(fern: Path, tmp_path: Path) -> set[str]:
    pruef = tmp_path / "pruef"
    if pruef.exists():
        _git("pull", "-q", cwd=pruef)
    else:
        _git("clone", "-q", str(fern), str(pruef), cwd=tmp_path)
    ausgabe = subprocess.run(["git", "ls-files"], cwd=pruef, capture_output=True, text=True)
    return set(ausgabe.stdout.split())


def test_spiegel_enthaelt_nur_freigegebene_bereiche(repos, capsys):
    haupt, fern, tmp_path = repos
    args = ["--haupt", str(haupt), "--url", str(fern), "--ziel", str(tmp_path / "kopie")]

    assert spiegel.main([*args, "--push"]) == 0
    assert _inhalt(fern, tmp_path) == {
        ".gitignore",
        "README.md",
        "02-Anwendung/app.py",
        "03-Server/skripte/install.sh",
    }
    assert "/03-Server/skripte/proxmox-installieren.sh" in capsys.readouterr().out

    assert spiegel.main([*args, "--push"]) == 0
    assert "bereits aktuell" in capsys.readouterr().out


def test_spiegel_bricht_bei_heimnetzadresse_ab(repos, capsys):
    haupt, fern, tmp_path = repos
    _schreiben(haupt, {"02-Anwendung/app.py": f"HOST = '{FREMDE_ADRESSE}'\n"})
    _git("commit", "-q", "-am", "Leck", cwd=haupt)

    args = ["--haupt", str(haupt), "--url", str(fern), "--ziel", str(tmp_path / "kopie")]
    assert spiegel.main([*args, "--push"]) == 1
    assert "Heimnetzadresse" in capsys.readouterr().out
    assert _inhalt(fern, tmp_path) == {"veraltet.txt"}


def test_push_verlangt_committeten_stand(repos, capsys):
    haupt, fern, tmp_path = repos
    _schreiben(haupt, {"02-Anwendung/app.py": "print('neu')\n"})

    args = ["--haupt", str(haupt), "--url", str(fern), "--ziel", str(tmp_path / "kopie")]
    assert spiegel.main([*args, "--push"]) == 1
    assert "erst im Hauptrepository committen" in capsys.readouterr().out

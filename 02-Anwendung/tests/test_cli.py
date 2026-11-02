"""Kommandozeilenwerkzeug gegen einen echten lokalen Server (LiveServer)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from steuerung import dienst
from steuerung.models import Auftrag

KIPM = Path(__file__).resolve().parents[1] / "client" / "kipm.py"


def kipm(live_server, schluessel, *argumente):
    umgebung = {**os.environ, "KIPM_URL": live_server.url, "KIPM_SCHLUESSEL": schluessel}
    return subprocess.run(
        [sys.executable, "-I", str(KIPM), *argumente],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=umgebung,
        timeout=60,
    )


@pytest.mark.django_db(transaction=True)
def test_cli_ablauf(live_server, owner):
    akteur = dienst.Akteur.aus_benutzer(owner)
    projekt = dienst.projekt_registrieren(akteur, kennung="cli-a", name="CLI A")
    dienst.arbeitsort_anlegen(akteur, projekt, kennung="repo", name="Repo")
    from tests.conftest import arbeitspaket

    auftrag = arbeitspaket(akteur, projekt, umfang="cli/**")
    dienst.auftrag_freigeben(akteur, auftrag)
    bindung, schluessel = dienst.bindung_anlegen(akteur, projekt, name="CLI-Chat")

    liste = kipm(live_server, schluessel, "meine-auftraege")
    assert (
        liste.returncode == 0 and auftrag.nummer in liste.stdout and "übernehmbar" in liste.stdout
    )

    lesen = kipm(live_server, schluessel, "lesen", auftrag.nummer)
    assert "Abnahmekriterien" in lesen.stdout and "cli/**" in lesen.stdout

    alt = kipm(live_server, schluessel, "uebernehmen", auftrag.nummer, "--revision", "1")
    assert alt.returncode == 1 and "veraltet" in alt.stdout

    ok = kipm(
        live_server, schluessel, "uebernehmen", auftrag.nummer, "--revision", "2", "--kennung", "u1"
    )
    assert ok.returncode == 0 and "IN_ARBEIT" in ok.stdout
    wieder = kipm(
        live_server, schluessel, "uebernehmen", auftrag.nummer, "--revision", "2", "--kennung", "u1"
    )
    assert wieder.returncode == 0 and "bereits ausgeführt" in wieder.stdout

    fortschritt = kipm(
        live_server, schluessel, "fortschritt", auftrag.nummer, "--revision", "2", "Hälfte fertig"
    )
    assert fortschritt.returncode == 0

    ergebnis = kipm(
        live_server,
        schluessel,
        "ergebnis",
        auftrag.nummer,
        "--revision",
        "2",
        "--zusammenfassung",
        "Umgesetzt",
        "--datei",
        "cli/x.py@abc123",
        "--pruefung",
        "pytest: ok",
    )
    assert ergebnis.returncode == 0 and "ZUR_PRUEFUNG" in ergebnis.stdout
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ZUR_PRUEFUNG

    falsch = kipm(live_server, "kipm_x_y", "meine-auftraege")
    assert falsch.returncode == 1 and "ungültig" in falsch.stdout


@pytest.mark.django_db(transaction=True)
def test_cli_liest_konfigurationsdatei(live_server, owner, tmp_path):
    akteur = dienst.Akteur.aus_benutzer(owner)
    projekt = dienst.projekt_registrieren(akteur, kennung="cli-k", name="CLI Konfig")
    _, schluessel = dienst.bindung_anlegen(akteur, projekt, name="Konfig-Chat")
    (tmp_path / ".kipm").write_text(
        "[kipm]\n" + f"url = {live_server.url}\n" + f"schluessel = {schluessel}\n",
        encoding="utf-8",
    )
    umgebung = {k: v for k, v in os.environ.items() if not k.startswith("KIPM_")}
    ergebnis = subprocess.run(
        [sys.executable, "-I", str(KIPM), "projekt"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=umgebung,
        cwd=tmp_path,
        timeout=60,
    )
    assert ergebnis.returncode == 0 and "CLI Konfig" in ergebnis.stdout

"""A-15: Kopie der Sicherung nach SharePoint (03-Server/skripte/kopieren.py) gegen einen nachgebildeten Microsoft-Dienst."""

import importlib.util
import io
import json
import os
import sys
import tarfile
import time
import urllib.parse
from pathlib import Path

import pytest

from tests.microsoft_nachbildung import Dienst

SKRIPTE = Path(__file__).resolve().parents[2] / "03-Server" / "skripte"
_spec = importlib.util.spec_from_file_location("kopieren", SKRIPTE / "kopieren.py")
assert _spec is not None and _spec.loader is not None
kopieren = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kopieren)

GEHEIMNIS = (
    "geheimnis-" + "nur-fuer-tests"
)  # zusammengesetzt, damit die Spiegelprüfung nichts findet
PRIVAT = "AGE-SECRET-KEY-" + "1RICHTIG"
FALSCHES_AGE = [sys.executable, str(Path(__file__).with_name("falsches_age.py"))]


def _archiv(pfad: Path, namen=("kipm.sqlite3", "MANIFEST.sha256")) -> Path:
    """Wie sichern.sh: tar -C <arbeit> -czf <datei> . (Namen mit ./ davor)."""
    with tarfile.open(pfad, "w:gz") as t:
        for name in namen:
            eintrag = tarfile.TarInfo(f"./{name}")
            inhalt = b"inhalt " + name.encode()
            eintrag.size = len(inhalt)
            t.addfile(eintrag, io.BytesIO(inhalt))
    return pfad


@pytest.fixture
def dienst():
    d = Dienst()
    d.start()
    yield d
    d.stopp()


@pytest.fixture
def ort(tmp_path, monkeypatch, dienst):
    ablage = tmp_path / "ablage"
    ablage.mkdir()
    _archiv(ablage / "kipm-20261104-0330.tar.gz")
    konfig = tmp_path / "sharepoint.env"
    konfig.write_text(
        f"KOPIE_MANDANT=mandant\nKOPIE_APP=app\nKOPIE_GEHEIMNIS={GEHEIMNIS}\nKOPIE_WEBSITE=firma.sharepoint.com:/sites/s\n",
        encoding="utf-8",
    )
    empfaenger = tmp_path / "empfaenger.txt"
    empfaenger.write_text("age1oeffentlich\n", encoding="utf-8")
    monkeypatch.setattr(kopieren, "ABLAGE", ablage)
    monkeypatch.setattr(kopieren, "KONFIG", konfig)
    monkeypatch.setattr(kopieren, "EMPFAENGER", empfaenger)
    monkeypatch.setattr(kopieren, "STAND", tmp_path / "kopie-stand.json")
    monkeypatch.setattr(kopieren, "LOGIN", dienst.basis)
    monkeypatch.setattr(kopieren, "GRAPH", dienst.basis)
    monkeypatch.setattr(kopieren, "AGE", FALSCHES_AGE)
    return tmp_path


def _stand(ort) -> dict:
    return json.loads((ort / "kopie-stand.json").read_text(encoding="utf-8"))


def test_nicht_eingerichtet_tut_nichts_und_meldet_keinen_fehler(ort, dienst, capsys):
    (ort / "sharepoint.env").unlink()
    assert kopieren.main([]) == 0
    assert dienst.anfragen == []
    assert not (ort / "kopie-stand.json").exists()
    assert "nicht eingerichtet" in capsys.readouterr().out


def test_erfolg_laedt_nur_verschluesselt_und_vermerkt_den_stand(ort, dienst, capsys):
    alt = ort / "ablage" / "kipm-20261103-0330.tar.gz"
    alt.write_bytes(b"alt")
    os.utime(alt, (1, 1))
    assert kopieren.main([]) == 0
    assert list(dienst.dateien) == ["kipm-20261104-0330.tar.gz.age"]
    inhalt = dienst.dateien["kipm-20261104-0330.tar.gz.age"]
    assert inhalt.startswith(b"AGE-NACHBILDUNG")
    hoch = next(a for a in dienst.anfragen if a["methode"] == "PUT")
    assert (
        hoch["pfad"]
        == "/sites/website-kennung/drive/root:/kipm-sicherung/kipm-20261104-0330.tar.gz.age:/content"
    )
    assert hoch["zugang"] == "Bearer token-der-nachbildung"
    anmeldung = urllib.parse.parse_qs(
        next(a for a in dienst.anfragen if a["methode"] == "POST")["koerper"].decode()
    )
    assert anmeldung["grant_type"] == ["client_credentials"]
    assert anmeldung["scope"] == ["https://graph.microsoft.com/.default"]
    stand = _stand(ort)
    assert stand["letzte_kopie"] and stand["fehler"] is None
    assert stand["datei"] == "kipm-20261104-0330.tar.gz.age"
    assert stand["groesse"] == len(inhalt)
    assert GEHEIMNIS not in capsys.readouterr().out + json.dumps(stand)


def test_erfolg_loescht_keine_datei_und_haengt_nichts_an_die_vorige(ort, dienst):
    assert kopieren.main([]) == 0
    assert {a["methode"] for a in dienst.anfragen} == {"POST", "GET", "PUT"}


def test_geheimnis_steht_nie_in_ausgabe_oder_stand_bei_fehler(ort, dienst, capsys):
    dienst.anmeldung_status = 401
    assert kopieren.main([]) == 1
    stand = _stand(ort)
    assert "abgelaufen" in stand["fehler"]
    assert GEHEIMNIS not in capsys.readouterr().out + json.dumps(stand)


def test_fehlschlag_behaelt_das_datum_der_letzten_kopie(ort, dienst):
    assert kopieren.main([]) == 0
    letzte = _stand(ort)["letzte_kopie"]
    dienst.hochladen_status = 403
    assert kopieren.main([]) == 1
    stand = _stand(ort)
    assert stand["letzte_kopie"] == letzte
    assert "Hochladen" in stand["fehler"]
    assert "accessDenied" in stand["fehler"]
    assert stand["fehler_zeit"]


def test_erfolg_loescht_den_alten_fehler(ort, dienst):
    dienst.hochladen_status = 403
    assert kopieren.main([]) == 1
    dienst.hochladen_status = 200
    assert kopieren.main([]) == 0
    assert _stand(ort)["fehler"] is None


def test_falsche_groesse_gilt_als_fehlschlag(ort, dienst):
    dienst.falsche_groesse = True
    assert kopieren.main([]) == 1
    assert "statt" in _stand(ort)["fehler"]
    assert "letzte_kopie" not in _stand(ort)


def test_alte_sicherung_wird_nicht_als_tageskopie_hochgeladen(ort, dienst):
    os.utime(ort / "ablage" / "kipm-20261104-0330.tar.gz", (1, 1))
    assert kopieren.main([]) == 1
    assert "Stunden alt" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def test_vortagssicherung_gilt_nicht_als_tageskopie(ort, dienst):
    # Zeitplan: Die Sicherung von 03:30 fiel aus, um 03:45 ist die vom Vortag gut 24 Stunden alt
    gestern = time.time() - (24 * 3600 + 15 * 60)
    os.utime(ort / "ablage" / "kipm-20261104-0330.tar.gz", (gestern, gestern))
    assert kopieren.main([]) == 1
    assert "Stunden alt" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


@pytest.mark.parametrize("kuerzen", [10, 40, -4])
def test_abgebrochene_sicherung_wird_nicht_hochgeladen(ort, dienst, kuerzen):
    datei = ort / "ablage" / "kipm-20261104-0330.tar.gz"
    datei.write_bytes(datei.read_bytes()[:kuerzen])
    assert kopieren.main([]) == 1
    assert "beschädigt oder unvollständig" in _stand(ort)["fehler"]
    assert "letzte_kopie" not in _stand(ort)
    assert dienst.anfragen == []


def test_datei_ohne_gzip_wird_nicht_hochgeladen(ort, dienst):
    (ort / "ablage" / "kipm-20261104-0330.tar.gz").write_bytes(b"kein archiv")
    assert kopieren.main([]) == 1
    assert "beschädigt" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def test_archiv_ohne_datenbank_wird_nicht_hochgeladen(ort, dienst):
    _archiv(ort / "ablage" / "kipm-20261104-0330.tar.gz", namen=("MANIFEST.sha256",))
    assert kopieren.main([]) == 1
    assert "keine vollständige" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def test_unerwarteter_fehler_steht_im_stand_ohne_seinen_text(ort, monkeypatch, capsys):
    def kaputt(*_):
        raise RuntimeError(GEHEIMNIS)

    monkeypatch.setattr(kopieren, "_hochladen", kaputt)
    assert kopieren.main([]) == 1
    assert _stand(ort)["fehler"] == "Unerwarteter Fehler (RuntimeError)"
    assert GEHEIMNIS not in capsys.readouterr().out


def test_stand_liegt_im_ordner_von_root():
    installieren = (SKRIPTE / "installieren.sh").read_text(encoding="utf-8")
    assert kopieren.STAND.parent.as_posix() == "/var/lib/kipm-kopie"
    assert "install -d -o root -g root -m 0755 /var/lib/kipm-kopie" in installieren


def test_keine_sicherung(ort, dienst):
    (ort / "ablage" / "kipm-20261104-0330.tar.gz").unlink()
    assert kopieren.main([]) == 1
    assert "Keine Sicherung" in _stand(ort)["fehler"]


def test_ohne_oeffentlichen_schluessel_geht_nichts_hinaus(ort, dienst):
    (ort / "empfaenger.txt").unlink()
    assert kopieren.main([]) == 1
    assert "einrichten" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def test_scheitert_die_verschluesselung_geht_nichts_hinaus(ort, dienst, monkeypatch):
    monkeypatch.setattr(kopieren, "AGE", [sys.executable, "-c", "raise SystemExit(3)"])
    assert kopieren.main([]) == 1
    assert "Verschlüsseln" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def test_fehlendes_programm_ist_ein_grund_kein_absturz(ort, monkeypatch):
    monkeypatch.setattr(kopieren, "AGE", [str(ort / "gibt-es-nicht")])
    assert kopieren.main([]) == 1
    assert "Programm fehlt" in _stand(ort)["fehler"]


def test_microsoft_nicht_erreichbar(ort, monkeypatch):
    monkeypatch.setattr(kopieren, "LOGIN", "http://127.0.0.1:9")
    assert kopieren.main([]) == 1
    assert "nicht erreichbar" in _stand(ort)["fehler"]


def test_weiterleitung_traegt_den_zugang_nicht_weiter(ort, dienst):
    dienst.umleiten = True
    assert kopieren.main([]) == 1
    assert "Status 302" in _stand(ort)["fehler"]
    assert not [a for a in dienst.anfragen if a["pfad"] == "/umgeleitet/"]


def test_unvollstaendige_angaben_werden_benannt(ort, dienst):
    (ort / "sharepoint.env").write_text("KOPIE_MANDANT=m\n", encoding="utf-8")
    assert kopieren.main([]) == 1
    assert "KOPIE_GEHEIMNIS" in _stand(ort)["fehler"]
    assert dienst.anfragen == []


def _antworten(monkeypatch, eingaben, verdeckt):
    eingaben, verdeckt = iter(eingaben), iter(verdeckt)
    monkeypatch.setattr("builtins.input", lambda _="": next(eingaben))
    monkeypatch.setattr(kopieren.getpass, "getpass", lambda _="": next(verdeckt))


def _einrichten(ort, monkeypatch):
    (ort / "sharepoint.env").unlink()
    (ort / "empfaenger.txt").unlink()
    keygen = ort / "keygen.py"
    keygen.write_text(
        f"import sys\nprint('# public key: age1neu')\nprint('{PRIVAT}')\nprint('Public key: age1neu', file=sys.stderr)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kopieren, "AGE_KEYGEN", [sys.executable, str(keygen)])
    _antworten(monkeypatch, ["mandant", "app", "firma.sharepoint.com:/sites/s", "ja"], [GEHEIMNIS])


def test_einrichten_speichert_nur_den_oeffentlichen_schluessel(ort, monkeypatch, capsys):
    _einrichten(ort, monkeypatch)
    assert kopieren.main(["--einrichten"]) == 0
    ausgabe = capsys.readouterr().out
    assert PRIVAT in ausgabe
    assert GEHEIMNIS not in ausgabe
    assert (ort / "empfaenger.txt").read_text(encoding="utf-8").strip() == "age1neu"
    assert GEHEIMNIS in (ort / "sharepoint.env").read_text(encoding="utf-8")
    keygen = ort / "keygen.py"
    for datei in ort.rglob("*"):
        if datei.is_file() and datei != keygen:
            assert PRIVAT not in datei.read_text(encoding="utf-8", errors="replace"), datei
    assert _stand(ort)["eingerichtet"]


def test_ohne_bestaetigung_gilt_der_schluessel_nicht(ort, monkeypatch, capsys):
    _einrichten(ort, monkeypatch)
    _antworten(
        monkeypatch, ["mandant", "app", "firma.sharepoint.com:/sites/s", "nein"], [GEHEIMNIS]
    )
    assert kopieren.main(["--einrichten"]) == 2
    assert not (ort / "empfaenger.txt").exists()
    assert "Nicht bestätigt" in capsys.readouterr().out
    assert kopieren.main([]) == 1  # keine Kopie, die niemand öffnen kann
    assert "einrichten" in _stand(ort)["fehler"]


@pytest.mark.skipif(os.name == "nt", reason="Dateirechte nur unter Linux")
def test_einrichten_zugangsdatei_nur_fuer_root(ort, monkeypatch):
    _einrichten(ort, monkeypatch)
    kopieren.main(["--einrichten"])
    assert (ort / "sharepoint.env").stat().st_mode & 0o777 == 0o600


def test_erneutes_einrichten_behaelt_schluessel_und_geheimnis(ort, monkeypatch, capsys):
    _antworten(monkeypatch, ["", "", ""], [""])
    assert kopieren.main(["--einrichten"]) == 0
    assert "age1oeffentlich" in (ort / "empfaenger.txt").read_text(encoding="utf-8")
    assert GEHEIMNIS in (ort / "sharepoint.env").read_text(encoding="utf-8")
    assert "AGE-SECRET-KEY-1" not in capsys.readouterr().out


def test_einrichten_meldet_abgelehnte_anmeldung(ort, dienst, monkeypatch, capsys):
    _einrichten(ort, monkeypatch)
    dienst.anmeldung_status = 401
    assert kopieren.main(["--einrichten"]) == 1
    assert "abgelaufen" in capsys.readouterr().out


def test_einrichten_ohne_angaben_speichert_nichts(ort, monkeypatch):
    (ort / "sharepoint.env").unlink()
    _antworten(monkeypatch, ["", "", ""], [""])
    assert kopieren.main(["--einrichten"]) == 2
    assert not (ort / "sharepoint.env").exists()


def _probe_schreiben(ort, monkeypatch):
    probe = ort / "probe.py"
    protokoll = ort / "probe.txt"
    probe.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(protokoll)!r}).write_bytes(sys.argv[1].encode() + b'|' + pathlib.Path(sys.argv[2]).read_bytes())\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kopieren, "WIEDERHERSTELLEN", [sys.executable, str(probe)])
    return protokoll


def test_pruefen_holt_die_juengste_kopie_und_prueft_sie_entschluesselt(ort, dienst, monkeypatch):
    assert kopieren.main([]) == 0
    dienst.dateien["kipm-20260101-0330.tar.gz.age"] = b"AGE-NACHBILDUNG\nalt"
    protokoll = _probe_schreiben(ort, monkeypatch)
    _antworten(monkeypatch, [], [PRIVAT])
    assert kopieren.main(["--pruefen"]) == 0
    art, inhalt = protokoll.read_bytes().split(b"|", 1)
    assert art == b"--pruefen"
    assert inhalt == (ort / "ablage" / "kipm-20261104-0330.tar.gz").read_bytes()
    abruf = next(a for a in dienst.anfragen if a["pfad"].startswith("/abruf/"))
    assert abruf["pfad"].endswith("kipm-20261104-0330.tar.gz.age")
    assert abruf["zugang"] is None


def test_pruefen_mit_falschem_schluessel_scheitert_ohne_probe(ort, dienst, monkeypatch, capsys):
    assert kopieren.main([]) == 0
    protokoll = _probe_schreiben(ort, monkeypatch)
    _antworten(monkeypatch, [], ["falsch"])
    assert kopieren.main(["--pruefen"]) == 1
    assert "falscher Schlüssel" in capsys.readouterr().out
    assert not protokoll.exists()


def test_pruefen_gibt_das_ergebnis_der_probe_weiter(ort, dienst, monkeypatch):
    assert kopieren.main([]) == 0
    monkeypatch.setattr(kopieren, "WIEDERHERSTELLEN", [sys.executable, "-c", "raise SystemExit(5)"])
    _antworten(monkeypatch, [], [PRIVAT])
    assert kopieren.main(["--pruefen"]) == 5


@pytest.mark.parametrize(
    "argumente", [["--pruefen"], ["--fremde-website", "x.sharepoint.com:/sites/y"]]
)
def test_vor_der_einrichtung_klare_meldung_statt_absturz(ort, capsys, argumente):
    (ort / "sharepoint.env").unlink()
    assert kopieren.main(argumente) == 1
    assert "--einrichten" in capsys.readouterr().out


def test_pruefen_ohne_kopie(ort, monkeypatch, capsys):
    assert kopieren.main(["--pruefen"]) == 1
    assert "noch keine Kopie" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "rueckgabe", "wort"),
    [(403, 0, "abgewiesen"), (404, 1, "nicht gefunden"), (500, 1, "UNKLAR"), (200, 1, "zu weit")],
)
def test_fremde_website_gilt_nur_abgewiesen_als_erfolg(
    ort, dienst, capsys, status, rueckgabe, wort
):
    dienst.website_status = status
    assert kopieren.main(["--fremde-website", "firma.sharepoint.com:/sites/andere"]) == rueckgabe
    assert wort in capsys.readouterr().out


def test_installation_und_sicherung():
    installieren = (SKRIPTE / "installieren.sh").read_text(encoding="utf-8")
    assert "age" in installieren.split("apt-get install")[1].splitlines()[0].split()
    assert '"$SKRIPTE/kopieren.py" /usr/local/sbin/kipm-kopieren' in installieren
    assert "45 3 * * * root /usr/local/sbin/kipm-kopieren" in installieren
    sichern = (SKRIPTE / "sichern.sh").read_text(encoding="utf-8")
    assert "sharepoint" not in sichern  # das Microsoft-Geheimnis kommt nicht in die Sicherung
    assert kopieren.KONFIG.parent.as_posix() == "/etc/kipm"
    assert "/etc/kipm/kipm.env" in sichern and kopieren.KONFIG.name != "kipm.env"

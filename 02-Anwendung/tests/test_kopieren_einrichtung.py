"""A-15: kipm-kopieren --einrichten legt nach einer Anmeldung alles selbst an und prüft es gegen die Nachbildung."""
# ruff: noqa: F811

import json
import os
import sys

import pytest

from tests.microsoft_nachbildung import ADMIN_TOKEN, GEHEIMNIS_ANFANG
from tests.test_kopieren import PRIVAT, _antworten, _stand, dienst, kopieren, ort  # noqa: F401


@pytest.fixture
def selbst(ort, monkeypatch):
    (ort / "sharepoint.env").unlink()
    (ort / "empfaenger.txt").unlink()
    keygen = ort / "keygen.py"
    keygen.write_text(
        f"import sys\nprint('# public key: age1neu')\nprint('{PRIVAT}')\nprint('Public key: age1neu', file=sys.stderr)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kopieren, "AGE_KEYGEN", [sys.executable, str(keygen)])
    monkeypatch.setattr(kopieren, "SICHERN", [sys.executable, "-c", "pass"])
    monkeypatch.setattr(kopieren, "WIEDERHERSTELLEN", [sys.executable, "-c", "pass"])
    monkeypatch.setattr(kopieren, "PAUSE", 0)
    _antworten(monkeypatch, [], [PRIVAT])
    return ort


def _konfig(ort) -> dict:
    zeilen = (ort / "sharepoint.env").read_text(encoding="utf-8").splitlines()
    return dict(z.split("=", 1) for z in zeilen)


def _anfragen(dienst, methode, pfad):
    return [a for a in dienst.anfragen if a["methode"] == methode and pfad in a["pfad"]]


def test_alles_in_einem_lauf(selbst, dienst, capsys):
    assert kopieren.main(["--einrichten"]) == 0
    ausgabe = capsys.readouterr().out
    assert "FERTIG" in ausgabe
    assert "ABCD1234" in ausgabe  # Fred sieht Adresse und Code
    assert _konfig(selbst) == {
        "KOPIE_MANDANT": "mandant-x",
        "KOPIE_APP": "app-id",
        "KOPIE_GEHEIMNIS": f"{GEHEIMNIS_ANFANG}1",
        "KOPIE_WEBSITE": "firma.sharepoint.com:/sites/kipm-sicherung",
    }
    assert (selbst / "empfaenger.txt").read_text(encoding="utf-8").strip() == "age1neu"
    assert list(dienst.dateien) == ["kipm-20261104-0330.tar.gz.age"]
    assert _stand(selbst)["letzte_kopie"]
    assert _stand(selbst)["eingerichtet"]


def test_app_bekommt_nur_die_enge_berechtigung_fuer_eine_website(selbst, dienst):
    assert kopieren.main(["--einrichten"]) == 0
    zugriff = dienst.apps[0]["requiredResourceAccess"][0]["resourceAccess"]
    assert zugriff == [{"id": "rolle-eng", "type": "Role"}]
    assert [z["appRoleId"] for z in dienst.zuweisungen] == ["rolle-eng"]
    assert dienst.zuweisungen[0]["principalId"] == "dienstkonto-1"
    assert dienst.zuweisungen[0]["resourceId"] == "graph-dienst"
    freigabe = dienst.freigaben[0]["wunsch"]
    assert freigabe["roles"] == ["write"]
    assert freigabe["grantedToIdentities"][0]["application"]["id"] == "app-id"
    assert _anfragen(dienst, "POST", "/sites/website-kennung/permissions")


def test_website_ist_privat_und_fred_gehoert_ihr(selbst, dienst):
    assert kopieren.main(["--einrichten"]) == 0
    gruppe = dienst.gruppen[0]
    assert gruppe["visibility"] == "Private"
    assert gruppe["groupTypes"] == ["Unified"]
    assert gruppe["owners@odata.bind"][0].endswith("/users/ich")
    assert gruppe["members@odata.bind"][0].endswith("/users/ich")


def test_geheimnisse_erscheinen_nirgends_ausser_in_der_zugangsdatei(selbst, dienst, capsys):
    assert kopieren.main(["--einrichten"]) == 0
    ausgabe = capsys.readouterr().out
    geheimnis = dienst.geheimnisse[0]
    assert geheimnis not in ausgabe
    assert ADMIN_TOKEN not in ausgabe
    for datei in selbst.rglob("*"):
        if datei.is_file() and datei.name not in {"keygen.py"}:
            inhalt = datei.read_text(encoding="utf-8", errors="replace")
            assert ADMIN_TOKEN not in inhalt, datei
            assert PRIVAT not in inhalt, datei
            if datei.name != "sharepoint.env":
                assert geheimnis not in inhalt, datei


def test_hochladen_nutzt_den_zugang_der_app_nicht_die_anmeldung(selbst, dienst):
    assert kopieren.main(["--einrichten"]) == 0
    assert {a["zugang"] for a in dienst.anfragen if a["methode"] == "PUT"} == {
        "Bearer token-der-nachbildung"
    }
    fremd = [a for a in dienst.anfragen if a["pfad"] == "/sites/firma.sharepoint.com"]
    assert fremd
    assert {a["zugang"] for a in fremd} == {"Bearer token-der-nachbildung"}


def test_erneuter_lauf_legt_nichts_doppelt_an_und_tauscht_das_geheimnis(
    selbst, dienst, monkeypatch
):
    assert kopieren.main(["--einrichten"]) == 0
    _antworten(monkeypatch, [], [PRIVAT])  # bestehender Schlüssel: nur für die Probe
    assert kopieren.main(["--einrichten"]) == 0
    assert len(dienst.gruppen) == len(dienst.apps) == len(dienst.dienstkonten) == 1
    assert len(dienst.zuweisungen) == len(dienst.freigaben) == 1
    assert dienst.entfernte_geheimnisse == ["schluessel-1"]
    assert _konfig(selbst)["KOPIE_GEHEIMNIS"] == f"{GEHEIMNIS_ANFANG}2"
    assert (selbst / "empfaenger.txt").read_text(encoding="utf-8").strip() == "age1neu"


def _fremde_app(**mehr) -> dict:
    return {
        "id": "fremd",
        "appId": "fremd-id",
        "displayName": kopieren.APP_NAME,
        "passwordCredentials": [{"keyId": "fremd-1"}],
        **mehr,
    }


def test_fremde_gleichnamige_app_bleibt_unberuehrt(selbst, dienst, capsys):
    dienst.apps.append(_fremde_app())
    assert kopieren.main(["--einrichten"]) == 1
    assert "nicht von diesem Skript" in capsys.readouterr().out
    assert dienst.entfernte_geheimnisse == []
    assert dienst.geheimnisse == []
    assert not _anfragen(dienst, "POST", "/servicePrincipals")
    assert not (selbst / "sharepoint.env").exists()


def test_zwei_gleichnamige_apps_werden_nicht_geraten(selbst, dienst):
    dienst.apps += [_fremde_app(notes=kopieren.MARKE), _fremde_app(id="f2", notes=kopieren.MARKE)]
    assert kopieren.main(["--einrichten"]) == 1
    assert dienst.geheimnisse == []


def test_eigene_app_mit_weiteren_rechten_wird_abgelehnt(selbst, dienst, capsys):
    dienst.apps.append(_fremde_app(notes=kopieren.MARKE))
    dienst.dienstkonten.append({"id": "dienstkonto-1", "appId": "fremd-id"})
    dienst.zuweisungen.append({"appRoleId": "rolle-breit"})
    assert kopieren.main(["--einrichten"]) == 1
    assert "weitere Rechte" in capsys.readouterr().out
    assert dienst.geheimnisse == []


def test_eigene_app_mit_mehr_rechten_auf_der_website_wird_abgelehnt(selbst, dienst, capsys):
    dienst.apps.append(_fremde_app(notes=kopieren.MARKE))
    dienst.freigaben.append(
        {"roles": ["owner"], "grantedToIdentitiesV2": [{"application": {"id": "fremd-id"}}]}
    )
    assert kopieren.main(["--einrichten"]) == 1
    assert "weitere Rechte" in capsys.readouterr().out
    assert dienst.geheimnisse == []


def test_fremde_website_ohne_gruppe_bleibt_unberuehrt(selbst, dienst, capsys):
    dienst.website_da = True
    assert kopieren.main(["--einrichten"]) == 1
    assert "nicht von diesem Skript" in capsys.readouterr().out
    assert not dienst.gruppen
    assert not dienst.apps


@pytest.mark.parametrize(
    "gruppe",
    [
        {"id": "g", "description": "Fremde Gruppe", "visibility": "Private"},
        {"id": "g", "description": kopieren.MARKE, "visibility": "Public"},
    ],
)
def test_fremde_oder_oeffentliche_gruppe_wird_abgelehnt(selbst, dienst, capsys, gruppe):
    dienst.gruppen.append(gruppe)
    assert kopieren.main(["--einrichten"]) == 1
    assert "nicht von diesem Skript" in capsys.readouterr().out
    assert not dienst.apps


def test_altes_geheimnis_erst_nach_dem_speichern_des_neuen(selbst, dienst, monkeypatch, capsys):
    assert kopieren.main(["--einrichten"]) == 0
    capsys.readouterr()

    def scheitert(_):
        raise OSError("Platte voll")

    _antworten(monkeypatch, [], [PRIVAT])
    monkeypatch.setattr(kopieren, "_konfig_schreiben", scheitert)
    assert kopieren.main(["--einrichten"]) == 1
    assert "OSError" in capsys.readouterr().out
    assert dienst.entfernte_geheimnisse == []  # das bisherige Geheimnis gilt weiter
    assert _konfig(selbst)["KOPIE_GEHEIMNIS"] == f"{GEHEIMNIS_ANFANG}1"


def test_scheitert_das_entfernen_bleibt_das_neue_gueltig(selbst, dienst, monkeypatch, capsys):
    assert kopieren.main(["--einrichten"]) == 0
    original = kopieren._graph

    def graph(was, *rest, **benannt):
        if was == "Altes Geheimnis entfernen":
            raise kopieren.KopieFehler("Altes Geheimnis entfernen: Status 503")
        return original(was, *rest, **benannt)

    monkeypatch.setattr(kopieren, "_graph", graph)
    _antworten(monkeypatch, [], [PRIVAT])
    assert kopieren.main(["--einrichten"]) == 0
    assert "Hinweis" in capsys.readouterr().out
    assert _konfig(selbst)["KOPIE_GEHEIMNIS"] == f"{GEHEIMNIS_ANFANG}2"


def test_konfig_wird_ganz_oder_gar_nicht_geschrieben(selbst, dienst):
    assert kopieren.main(["--einrichten"]) == 0
    assert not list(selbst.glob("sharepoint.env.*"))


def test_erneuter_lauf_verbraucht_keine_sicherung(selbst, dienst, monkeypatch):
    protokoll = selbst / "gesichert.txt"
    monkeypatch.setattr(
        kopieren,
        "SICHERN",
        [sys.executable, "-c", f"open({str(protokoll)!r}, 'w').close()"],
    )
    assert kopieren.main(["--einrichten"]) == 0
    assert not protokoll.exists()  # die Sicherung ist noch keine Stunde alt


def test_alte_sicherung_wird_vor_der_probe_erneuert(selbst, dienst, monkeypatch):
    datei = selbst / "ablage" / "kipm-20261104-0330.tar.gz"
    os.utime(datei, (1, 1))
    monkeypatch.setattr(
        kopieren,
        "SICHERN",
        [sys.executable, "-c", f"import os; os.utime({str(datei)!r})"],
    )
    assert kopieren.main(["--einrichten"]) == 0
    assert list(dienst.dateien) == ["kipm-20261104-0330.tar.gz.age"]


def test_unerwartete_antwort_endet_mit_grund_ohne_absturz(selbst, dienst, monkeypatch, capsys):
    original = kopieren._graph

    def graph(was, *rest, **benannt):
        if was == "Stammwebsite lesen":
            return {"id": "stamm"}
        return original(was, *rest, **benannt)

    monkeypatch.setattr(kopieren, "_graph", graph)
    assert kopieren.main(["--einrichten"]) == 1
    assert "Unerwartete Antwort" in capsys.readouterr().out


def test_wartet_auf_die_anmeldung(selbst, dienst):
    dienst.anmeldung_wartet = 3
    assert kopieren.main(["--einrichten"]) == 0
    assert len(_anfragen(dienst, "POST", "/organizations/oauth2/v2.0/token")) == 4


def test_wartet_bis_microsoft_die_neue_app_kennt(selbst, dienst):
    dienst.client_fehlschlaege = 3
    dienst.hochladen_fehlschlaege = 2
    dienst.website_verzoegerung = 3
    assert kopieren.main(["--einrichten"]) == 0
    assert _stand(selbst)["fehler"] is None


def test_konto_ohne_rechte_schreibt_nichts_und_nennt_den_ausweg(selbst, dienst, capsys):
    dienst.admin_status = 403
    assert kopieren.main(["--einrichten"]) == 1
    ausgabe = capsys.readouterr().out
    assert "Authorization_RequestDenied" in ausgabe
    assert "Administratorrechte" in ausgabe
    assert "--einrichten-von-hand" in ausgabe
    assert not (selbst / "sharepoint.env").exists()


def test_abgelehnte_anmeldung_bricht_sauber_ab(selbst, dienst, capsys, monkeypatch):
    def abgelehnt(*_):
        raise kopieren.KopieFehler("Anmeldung: Status 400 authorization_declined")

    monkeypatch.setattr(kopieren, "anmelden", abgelehnt)
    assert kopieren.main(["--einrichten"]) == 1
    assert "authorization_declined" in capsys.readouterr().out
    assert not dienst.anfragen


def test_ohne_gesicherten_schluessel_keine_kopie(selbst, dienst, monkeypatch, capsys):
    _antworten(monkeypatch, [], ["falsch", "falsch", "falsch"])
    assert kopieren.main(["--einrichten"]) == 2
    assert "Nicht bestätigt" in capsys.readouterr().out
    assert not (selbst / "empfaenger.txt").exists()
    assert not dienst.dateien
    assert _stand(selbst)["eingerichtet"]  # die Warnung zählt ab jetzt


def test_tippfehler_beim_schluessel_darf_korrigiert_werden(selbst, dienst, monkeypatch):
    _antworten(monkeypatch, [], ["falsch", PRIVAT])
    assert kopieren.main(["--einrichten"]) == 0
    assert (selbst / "empfaenger.txt").exists()


def test_zu_weiter_zugang_gilt_nicht_als_fertig(selbst, dienst, capsys):
    dienst.fremde_erreichbar = True
    assert kopieren.main(["--einrichten"]) == 1
    ausgabe = capsys.readouterr().out
    assert "NICHT BESTANDEN" in ausgabe
    assert "FERTIG" not in ausgabe


def test_gescheiterte_probe_gilt_nicht_als_fertig(selbst, dienst, monkeypatch, capsys):
    monkeypatch.setattr(kopieren, "WIEDERHERSTELLEN", [sys.executable, "-c", "raise SystemExit(4)"])
    assert kopieren.main(["--einrichten"]) == 1
    assert "NICHT BESTANDEN" in capsys.readouterr().out


def test_upload_scheitert_dauerhaft(selbst, dienst, monkeypatch, capsys):
    monkeypatch.setattr(kopieren, "VERSUCHE", 2)
    dienst.hochladen_status = 403
    assert kopieren.main(["--einrichten"]) == 1
    assert "ließ sich nicht hochladen" in capsys.readouterr().out
    assert json.loads((selbst / "kopie-stand.json").read_text(encoding="utf-8"))["fehler"]

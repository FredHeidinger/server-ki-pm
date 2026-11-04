"""A-15: Die Startseite warnt, wenn die Kopie der Sicherung ausbleibt."""

import datetime
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import sicherung

JETZT = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=datetime.UTC)


@pytest.fixture
def stand(settings, tmp_path):
    settings.KIPM_KOPIE_STAND = tmp_path / "kopie-stand.json"

    def schreiben(**werte):
        settings.KIPM_KOPIE_STAND.write_text(json.dumps(werte), encoding="utf-8")

    return schreiben


def _vor(**abstand) -> str:
    return (JETZT - datetime.timedelta(**abstand)).isoformat()


def test_nicht_eingerichtet_keine_warnung(stand):
    assert sicherung.kopie_warnung(JETZT) is None


def test_frische_kopie_keine_warnung(stand):
    stand(letzte_kopie=_vor(hours=9), eingerichtet=_vor(days=30))
    assert sicherung.kopie_warnung(JETZT) is None


def test_eine_gescheiterte_nacht_warnt_noch_nicht(stand):
    stand(letzte_kopie=_vor(days=1, hours=9), fehler="Hochladen: Status 503")
    assert sicherung.kopie_warnung(JETZT) is None


def test_genau_an_der_grenze_noch_keine_warnung(stand):
    stand(letzte_kopie=_vor(days=2))
    assert sicherung.kopie_warnung(JETZT) is None


def test_alte_kopie_warnt_mit_datum_und_grund(stand, settings):
    settings.TIME_ZONE = "Europe/Vienna"
    stand(letzte_kopie=_vor(days=3), fehler="Anmeldung abgelehnt")
    with timezone.override("Europe/Vienna"):
        text = sicherung.kopie_warnung(JETZT) or ""
    assert "07.11.2026 13:00" in text
    assert "Anmeldung abgelehnt" in text
    assert "/var/log/kipm-kopie.log" in text


def test_grund_mit_punkt_ergibt_keinen_doppelten_punkt(stand):
    stand(letzte_kopie=_vor(days=3), fehler="Neu eintragen.")
    assert "Neu eintragen. Protokoll" in (sicherung.kopie_warnung(JETZT) or "")


def test_zeitplan_ausgefallen_warnt_auch_ohne_fehler(stand):
    stand(letzte_kopie=_vor(days=5), fehler=None)
    text = sicherung.kopie_warnung(JETZT) or ""
    assert "Die letzte Kopie" in text
    assert "Letzter Fehler" not in text


def test_nach_einrichtung_nie_gelaufen_warnt_nach_der_grenze(stand):
    stand(eingerichtet=_vor(hours=30))
    assert sicherung.kopie_warnung(JETZT) is None
    stand(eingerichtet=_vor(days=3))
    assert "noch keine Kopie" in (sicherung.kopie_warnung(JETZT) or "")


@pytest.mark.parametrize(
    "roh",
    [
        "{kaputt",
        "[]",
        '{"letzte_kopie": "gestern"}',
        "{}",
        '{"letzte_kopie": "2026-11-01T10:00:00"}',
    ],
)
def test_kaputter_stand_warnt_statt_zu_schweigen(stand, settings, roh):
    settings.KIPM_KOPIE_STAND.write_text(roh, encoding="utf-8")
    assert "nicht lesbar" in (sicherung.kopie_warnung(JETZT) or "")


def test_ungueltiges_utf8_warnt_statt_die_startseite_zu_kippen(stand, settings):
    settings.KIPM_KOPIE_STAND.write_bytes(b"\xff\xfe")
    assert "nicht lesbar" in (sicherung.kopie_warnung(JETZT) or "")


def test_stand_liegt_nicht_im_ordner_des_dienstkontos():
    from django.conf import settings as echte

    assert echte.KIPM_KOPIE_STAND.as_posix() == "/var/lib/kipm-kopie/kopie-stand.json"


def test_nicht_lesbare_datei_warnt(settings, tmp_path):
    settings.KIPM_KOPIE_STAND = tmp_path  # ein Ordner lässt sich nicht als Datei lesen
    assert "nicht lesbar" in (sicherung.kopie_warnung(JETZT) or "")


@pytest.mark.django_db
def test_startseite_zeigt_die_warnung_als_text(stand, owner_client):
    stand(letzte_kopie="2020-01-01T00:00:00+00:00", fehler="<b>kaputt</b>")
    html = owner_client.get(reverse("start")).content.decode()
    assert "Sicherungskopie: Die letzte Kopie" in html
    assert "&lt;b&gt;kaputt&lt;/b&gt;" in html
    assert "<b>kaputt</b>" not in html


@pytest.mark.django_db
def test_startseite_ohne_warnung_bleibt_wie_sie_ist(stand, owner_client):
    assert "Sicherungskopie" not in owner_client.get(reverse("start")).content.decode()
    stand(letzte_kopie=timezone.now().isoformat())
    assert "Sicherungskopie" not in owner_client.get(reverse("start")).content.decode()

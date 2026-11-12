"""Testlauf KTL-1, Punkt 3: Jede 400- und 409-Antwort an die Werkbank nennt ihre Mängel in
``details.maengel`` (nicht leere Liste). Der Vertrag lässt ``details`` offen; die Liste ist eine
Ergänzung, keine Vertragsänderung."""

import json
import uuid

import pytest

from steuerung import werkbank_vertrag
from tests.werkbank_hilfen import (
    BASIS,
    KOPF,
    ereignis_nachricht,
    kopf,
    senden,
)

pytestmark = pytest.mark.django_db


def _maengel(antwort, status):
    assert antwort.status_code == status, antwort.content
    body = antwort.json()
    assert werkbank_vertrag.schema_fehler("fehler", body) == []
    maengel = body["fehler"].get("details", {}).get("maengel")
    assert isinstance(maengel, list) and maengel and all(isinstance(m, str) and m for m in maengel)
    return maengel


def test_400_ungueltige_operation_id(client, w_instanz):
    antwort = client.get(BASIS + "operationen/keine-uuid/", **kopf(w_instanz))
    assert _maengel(antwort, 400)


def test_400_kaputter_koerper(client, w_instanz):
    antwort = client.post(
        BASIS + "ereignisse/", data="{kein json", content_type="application/json",
        **kopf(w_instanz),
    )  # fmt: skip
    assert _maengel(antwort, 400)


def test_400_schemafehler_nennt_jede_abweichung(client, w_ausfuehrung, w_instanz):
    n = ereignis_nachricht(w_ausfuehrung, w_instanz, "lebenszeichen", {}, sequenz=1)
    del n["nutzlast"]["ereignis_id"]
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    assert _maengel(senden(client, w_instanz, "ereignisse/", n), 400)


def test_409_vergebene_sequenz(client, w_ausfuehrung, w_instanz):
    n = ereignis_nachricht(w_ausfuehrung, w_instanz, "lebenszeichen", {}, sequenz=1)
    assert senden(client, w_instanz, "ereignisse/", n).status_code == 200
    zweite = ereignis_nachricht(w_ausfuehrung, w_instanz, "lebenszeichen", {}, sequenz=1)
    assert _maengel(senden(client, w_instanz, "ereignisse/", zweite), 409)


def test_409_widerspruechliche_wiederholung(client, w_ausfuehrung, w_instanz):
    op = uuid.uuid4()
    n = ereignis_nachricht(
        w_ausfuehrung, w_instanz, "lebenszeichen", {}, sequenz=1, operation_id=op
    )
    assert senden(client, w_instanz, "ereignisse/", n).status_code == 200
    anders = ereignis_nachricht(
        w_ausfuehrung, w_instanz, "lebenszeichen", {}, sequenz=2, operation_id=op
    )
    assert _maengel(senden(client, w_instanz, "ereignisse/", anders), 409)


def test_409_abschluss_behaelt_seine_eigenen_maengel(client, w_ausfuehrung, w_instanz):
    from tests.test_werkbank_abschluss import bericht

    n = ereignis_nachricht(
        w_ausfuehrung, w_instanz, "abschluss_bericht", bericht(w_ausfuehrung), sequenz=1
    )  # Planfassung nie bestätigt, Paket p1 unbekannt
    maengel = _maengel(senden(client, w_instanz, "ereignisse/", n), 409)
    assert any("Planfassung" in m for m in maengel)


def test_auch_andere_status_bleiben_schemagerecht(client):
    antwort = client.get(BASIS + "abgleich/", **KOPF)  # ohne Zugang: 401
    assert antwort.status_code == 401
    assert werkbank_vertrag.schema_fehler("fehler", json.loads(antwort.content)) == []

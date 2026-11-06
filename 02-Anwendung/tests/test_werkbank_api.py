"""Werkbank-Anbindung WP3: HTTP-Grundgerüst unter /api/werkbank/v1/ (Vertrag kipm-werkbank/1).

Anmeldung, Fehlerformat, Quellbindung, eigener Wiederholungsschutz, Abgleich, Operationsabruf,
Lebenszeichen und Bereitschaft. Die fachliche Wirkung von uebernehmen, schritt und den übrigen
Ereignisarten kommt in WP4/WP5; bis dahin antwortet die API klar „noch nicht verfügbar“.
"""

import json
import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import dienst, werkbank, werkbank_vertrag
from steuerung.models import (
    Auftrag,
    ChatBindung,
    WerkbankAusfuehrung,
    WerkbankEreignis,
    WerkbankInstanz,
    WerkbankOperation,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)

pytestmark = pytest.mark.django_db

KOPF = {"HTTP_X_KIPM_VERTRAG": "kipm-werkbank/1"}
BASIS = "/api/werkbank/v1/"
HASH = "a" * 64


@pytest.fixture
def instanz(akteur, projekt):
    inst, schluessel = werkbank.instanz_anlegen(akteur, name="Werkbank PC", projekte=[projekt])
    inst.klartext = schluessel
    return inst


@pytest.fixture
def pbindung(instanz, projekt):
    return WerkbankProjektbindung.objects.create(
        instanz=instanz, projekt=projekt, arbeitsort=projekt.arbeitsorte.first()
    )


def _auftrag(projekt, titel="Synthetischer Auftrag", **felder):
    daten = {
        "projekt": projekt,
        "titel": titel,
        "revision_nr": 1,
        "zustand": Auftrag.IN_ARBEIT,
        "ausfuehrungsweg": Auftrag.WEG_WERKBANK,
    }
    return Auftrag.objects.create(**(daten | felder))


@pytest.fixture
def auftrag(projekt):
    return _auftrag(projekt)


@pytest.fixture
def zuweisung(auftrag, instanz, pbindung):
    return WerkbankZuweisung.objects.create(
        auftrag=auftrag, instanz=instanz, projektbindung=pbindung
    )


@pytest.fixture
def ausfuehrung(auftrag, zuweisung, pbindung):
    return WerkbankAusfuehrung.objects.create(
        auftrag=auftrag,
        revision=auftrag.revision_nr,
        zuweisung=zuweisung,
        zuweisung_version=zuweisung.version,
        projektbindung=pbindung,
        projektbindung_version=pbindung.version,
        snapshot={},
        snapshot_sha256=HASH,
        zustand="laeuft",
        statusversion=3,
    )


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


def kopf(instanz, **mehr):
    return {"HTTP_AUTHORIZATION": f"Bearer {instanz.klartext}"} | KOPF | mehr


def quelle(instanz, pb, a, ausf=None):
    q = {
        "server_id": str(werkbank.server_id()),
        "werkbank_id": str(instanz.werkbank_id),
        "projektbindung_id": pb.pk,
        "projektbindung_version": pb.version,
        "auftrag_id": a.pk,
        "revision": a.revision_nr,
        "zuweisung_version": 1,
    }
    if ausf is not None:
        q |= {"ausfuehrung_id": str(ausf.ausfuehrung_id), "snapshot_sha256": ausf.snapshot_sha256}
    return q


def nachricht(art, q, nutzlast, operation_id=None):
    n = {
        "vertrag": "kipm-werkbank/1",
        "operation_id": str(operation_id or uuid.uuid4()),
        "art": art,
        "quelle": q,
        "nutzlast": nutzlast,
    }
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    return n


def lebenszeichen(q, sequenz=1, operation_id=None, ereignis_id=None):
    return nachricht(
        "ereignisse",
        q,
        {
            "ereignis_id": str(ereignis_id or uuid.uuid4()),
            "sequenz": sequenz,
            "statusversion": None,
            "ereignis": {"art": "lebenszeichen", "daten": {}},
        },
        operation_id,
    )


def schritt(q, ausf, operation_id=None, versuch=1):
    return nachricht(
        "schritt",
        q,
        {
            "schritt": {
                "ausfuehrung_id": str(ausf.ausfuehrung_id),
                "planfassung": "vorplanung",
                "paket": "root",
                "phase": "planung",
                "position": 0,
            },
            "versuch_nr": versuch,
            "eingabehash": HASH,
            "wirkung": "lesen",
            "owner_aktion_id": None,
        },
        operation_id,
    )


def uebernehmen(q, operation_id=None):
    return nachricht(
        "uebernehmen",
        q,
        {
            "repo_fingerabdruck": "repo-1",
            "konfig_fingerabdruecke": {"pruefprofil": HASH},
            "faehigkeiten": ["git", "pytest"],
            "werkbank_version": "1.0.0",
        },
        operation_id,
    )


def post(client, instanz, pfad, n, **mehr):
    inhalt = n if isinstance(n, (bytes, str)) else json.dumps(n)
    return client.post(
        BASIS + pfad, data=inhalt, content_type="application/json", **kopf(instanz, **mehr)
    )


def fehler_pruefen(antwort, status, code):
    assert antwort.status_code == status, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("fehler", daten) == []
    assert daten["fehler"]["code"] == code
    return daten["fehler"]


# --- Anmeldung ------------------------------------------------------------------------------


def test_ohne_schluessel_401(client):
    fehler = fehler_pruefen(client.get(BASIS + "abgleich/", **KOPF), 401, "NICHT_ANGEMELDET")
    assert fehler["naechste_handlung"]


@pytest.mark.parametrize("kopfzeile", ["Bearer kipm_abc_falsch", "Basic abc", "Bearer ", "kipm_x"])
def test_falsche_schluessel_401(client, instanz, kopfzeile):
    antwort = client.get(BASIS + "abgleich/", HTTP_AUTHORIZATION=kopfzeile, **KOPF)
    fehler_pruefen(antwort, 401, "NICHT_ANGEMELDET")


def test_widerrufener_zugang_401(client, akteur, instanz):
    werkbank.instanz_widerrufen(akteur, instanz, "Test")
    fehler_pruefen(client.get(BASIS + "abgleich/", **kopf(instanz)), 401, "NICHT_ANGEMELDET")


def test_abgelaufener_zugang_401(client, instanz):
    ChatBindung.objects.filter(pk=instanz.bindung_id).update(
        gueltig_bis=timezone.now() - timedelta(seconds=1)
    )
    fehler_pruefen(client.get(BASIS + "abgleich/", **kopf(instanz)), 401, "NICHT_ANGEMELDET")


def test_deaktivierte_instanz_401(client, akteur, instanz):
    werkbank.instanz_aktiv_setzen(akteur, instanz, False)
    fehler_pruefen(client.get(BASIS + "abgleich/", **kopf(instanz)), 401, "NICHT_ANGEMELDET")


def test_chat_und_pc_schluessel_werden_abgewiesen(client, bindung, pc_zugang):
    for zugang in (bindung, pc_zugang):
        antwort = client.get(
            BASIS + "abgleich/", HTTP_AUTHORIZATION=f"Bearer {zugang.klartext}", **KOPF
        )
        fehler = fehler_pruefen(antwort, 403, "FALSCHE_ZUGANGSART")
        assert zugang.klartext not in json.dumps(fehler)


def test_chat_weg_bleibt_unveraendert(client, bindung):
    antwort = client.get("/api/projekt/", HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}")
    assert antwort.status_code == 200 and antwort.json()["ok"] is True


def test_werkbank_schluessel_hinterlaesst_keinen_chat_besuchsvermerk(client, instanz):
    vorher = ChatBindung.objects.values().get(pk=instanz.bindung_id)
    assert (
        client.get(BASIS + "abgleich/", **kopf(instanz, HTTP_X_KIPM_ORDNER="C:/x")).status_code
        == 200
    )
    assert ChatBindung.objects.values().get(pk=instanz.bindung_id) == vorher


# --- Vertragskopf, Methode ------------------------------------------------------------------


@pytest.mark.parametrize("wert", [None, "kipm-werkbank/2", "", "x" * 500])
def test_falscher_vertragskopf_426(client, instanz, wert):
    k = {"HTTP_AUTHORIZATION": f"Bearer {instanz.klartext}"}
    if wert is not None:
        k["HTTP_X_KIPM_VERTRAG"] = wert
    fehler = fehler_pruefen(client.get(BASIS + "abgleich/", **k), 426, "VERTRAG_INKOMPATIBEL")
    assert "kipm-werkbank/1" in fehler["naechste_handlung"] + fehler["text"]
    assert len(json.dumps(fehler)) < 2000
    instanz.refresh_from_db()
    assert instanz.letzter_kontakt is None


def test_methode_nicht_erlaubt_405(client, instanz):
    fehler_pruefen(client.post(BASIS + "abgleich/", **kopf(instanz)), 405, "METHODE_NICHT_ERLAUBT")
    fehler_pruefen(client.get(BASIS + "ereignisse/", **kopf(instanz)), 405, "METHODE_NICHT_ERLAUBT")


# --- Abgleich -------------------------------------------------------------------------------


def test_abgleich_leer_ist_schemagueltig_und_setzt_kontakt(client, instanz):
    vorher = timezone.now()
    antwort = client.get(BASIS + "abgleich/", **kopf(instanz))
    assert antwort.status_code == 200
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("abgleich_antwort", daten) == []
    assert daten["server_id"] == str(werkbank.server_id())
    assert daten["zuweisungen"] == daten["aktionen"] == daten["halte"] == daten["konflikte"] == []
    assert daten["seite"] == {"groesse": 0, "naechste": None}
    instanz.refresh_from_db()
    assert vorher <= instanz.letzter_kontakt <= timezone.now()


def test_abgleich_zeigt_zuweisung_und_halt(
    client, instanz, pbindung, auftrag, zuweisung, ausfuehrung
):
    WerkbankAusfuehrung.objects.filter(pk=ausfuehrung.pk).update(
        zustand="halt_angefordert", halt_zweck="abbrechen"
    )
    daten = client.get(BASIS + "abgleich/", **kopf(instanz)).json()
    assert werkbank_vertrag.schema_fehler("abgleich_antwort", daten) == []
    (eintrag,) = daten["zuweisungen"]
    assert eintrag["auftrag_id"] == auftrag.pk and eintrag["nummer"] == auftrag.nummer
    assert eintrag["freigegeben"] is True
    assert eintrag["ausfuehrung_id"] == str(ausfuehrung.ausfuehrung_id)
    (halt,) = daten["halte"]
    assert halt["zweck"] == "abbrechen"
    assert halt["quelle"]["ausfuehrung_id"] == str(ausfuehrung.ausfuehrung_id)


def test_abgleich_zeigt_nur_eigene_erlaubte_und_offene(
    client, akteur, instanz, pbindung, projekt, projekt_b
):
    fremde = WerkbankInstanz.objects.create(name="Andere Werkbank")
    fremd_pb = WerkbankProjektbindung.objects.create(instanz=fremde, projekt=projekt)
    WerkbankZuweisung.objects.create(
        auftrag=_auftrag(projekt, "fremd"), instanz=fremde, projektbindung=fremd_pb
    )
    erledigt = _auftrag(projekt, "fertig", zustand=Auftrag.ERLEDIGT)
    WerkbankZuweisung.objects.create(auftrag=erledigt, instanz=instanz, projektbindung=pbindung)
    eigener = _auftrag(projekt, "eigen")
    WerkbankZuweisung.objects.create(auftrag=eigener, instanz=instanz, projektbindung=pbindung)
    daten = client.get(BASIS + "abgleich/", **kopf(instanz)).json()
    assert [z["auftrag_id"] for z in daten["zuweisungen"]] == [eigener.pk]
    werkbank.instanz_projekte_setzen(akteur, instanz, [projekt_b])
    assert client.get(BASIS + "abgleich/", **kopf(instanz)).json()["zuweisungen"] == []


def test_abgleich_paginiert_mit_cursor_bis_100(client, instanz, pbindung, projekt):
    ids = set()
    for i in range(205):
        a = _auftrag(projekt, f"A{i}")
        WerkbankZuweisung.objects.create(auftrag=a, instanz=instanz, projektbindung=pbindung)
        ids.add(a.pk)
    gesehen, seite, cursor = [], 0, None
    while True:
        url = BASIS + "abgleich/" + (f"?seite={cursor}" if cursor else "")
        daten = client.get(url, **kopf(instanz)).json()
        assert werkbank_vertrag.schema_fehler("abgleich_antwort", daten) == []
        assert len(daten["zuweisungen"]) <= 100
        gesehen += [z["auftrag_id"] for z in daten["zuweisungen"]]
        seite += 1
        cursor = daten["seite"]["naechste"]
        if cursor is None:
            break
        assert seite < 10
    assert seite == 3 and len(gesehen) == 205 and set(gesehen) == ids


def test_abgleich_ungueltiger_cursor_400(client, instanz):
    fehler_pruefen(
        client.get(BASIS + "abgleich/?seite=kaputt!", **kopf(instanz)), 400, "UNGUELTIGE_NACHRICHT"
    )


# --- Operationen ----------------------------------------------------------------------------


def test_unbekannte_operation(client, instanz):
    op = uuid.uuid4()
    antwort = client.get(f"{BASIS}operationen/{op}/", **kopf(instanz))
    daten = antwort.json()
    assert antwort.status_code == 200
    assert werkbank_vertrag.schema_fehler("operation_antwort", daten) == []
    assert daten["status"] == "unbekannt" and daten["ausgang"] is None


def test_operation_mit_ungueltiger_kennung_400(client, instanz):
    fehler_pruefen(
        client.get(BASIS + "operationen/keine-uuid/", **kopf(instanz)), 400, "UNGUELTIGE_NACHRICHT"
    )


def test_verlorene_antwort_operationsabruf(client, instanz, pbindung, auftrag, ausfuehrung):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung))
    erste = post(client, instanz, "ereignisse/", n)
    assert erste.status_code == 200
    abruf = client.get(f"{BASIS}operationen/{n['operation_id']}/", **kopf(instanz)).json()
    assert werkbank_vertrag.schema_fehler("operation_antwort", abruf) == []
    assert abruf["status"] == "gespeichert" and abruf["inhaltshash"] == n["inhaltshash"]
    assert abruf["ausgang"] == {"http_status": 200, "antwort": erste.json()}
    # Lesen ist kein neuer Startschein: nichts hat sich verändert.
    assert WerkbankOperation.objects.count() == 1 and WerkbankEreignis.objects.count() == 1


def test_operation_einer_anderen_werkbank_ist_unbekannt(
    client, akteur, instanz, pbindung, auftrag, ausfuehrung, projekt
):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung))
    assert post(client, instanz, "ereignisse/", n).status_code == 200
    zweite, schluessel = werkbank.instanz_anlegen(akteur, name="Zweite", projekte=[projekt])
    zweite.klartext = schluessel
    abruf = client.get(f"{BASIS}operationen/{n['operation_id']}/", **kopf(zweite)).json()
    assert abruf["status"] == "unbekannt" and abruf["ausgang"] is None


# --- Nachrichten: Form, Größe, Quellbindung -------------------------------------------------


def test_ungueltiges_json_400(client, instanz):
    fehler_pruefen(
        post(client, instanz, "ereignisse/", b"{nicht json"), 400, "UNGUELTIGE_NACHRICHT"
    )
    fehler_pruefen(post(client, instanz, "ereignisse/", b"[1]"), 400, "UNGUELTIGE_NACHRICHT")


def test_schemafehler_400_ohne_aenderung(client, instanz, pbindung, auftrag, ausfuehrung):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    ok = lebenszeichen(q)
    ohne_quelle = {k: v for k, v in ok.items() if k != "quelle"}
    unbekannt = ok | {"zusatz": 1}
    falscher_vertrag = nachricht("ereignisse", q, ok["nutzlast"]) | {"vertrag": "kipm-werkbank/2"}
    for schlecht in (ohne_quelle, unbekannt, falscher_vertrag):
        fehler = fehler_pruefen(
            post(client, instanz, "ereignisse/", schlecht), 400, "UNGUELTIGE_NACHRICHT"
        )
        assert fehler["details"]["verstoesse"]
    assert WerkbankOperation.objects.count() == 0


def test_falscher_inhaltshash_400(client, instanz, pbindung, auftrag, ausfuehrung):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung)) | {"inhaltshash": "b" * 64}
    fehler_pruefen(post(client, instanz, "ereignisse/", n), 400, "UNGUELTIGE_NACHRICHT")
    assert WerkbankOperation.objects.count() == 0


def test_zu_grosse_nachricht_413(client, instanz):
    gross = b'{"x":"' + b"a" * (werkbank.MAX_NACHRICHT_BYTES + 1) + b'"}'
    fehler_pruefen(post(client, instanz, "ereignisse/", gross), 413, "ZU_GROSS")


def test_fremdes_projekt_ist_404(
    client, akteur, instanz, pbindung, auftrag, ausfuehrung, projekt_b
):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung))
    werkbank.instanz_projekte_setzen(akteur, instanz, [projekt_b])
    fehler_pruefen(post(client, instanz, "ereignisse/", n), 404, "NICHT_GEFUNDEN")
    assert WerkbankOperation.objects.count() == 0


def test_fremde_projektbindung_auftrag_ausfuehrung_werkbank_sind_404(
    client, akteur, instanz, pbindung, auftrag, ausfuehrung, projekt
):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    fremde = WerkbankInstanz.objects.create(name="Fremd")
    fremd_pb = WerkbankProjektbindung.objects.create(instanz=fremde, projekt=projekt)
    for aenderung in (
        {"projektbindung_id": fremd_pb.pk},
        {"projektbindung_id": 999999},
        {"auftrag_id": 999999},
        {"ausfuehrung_id": str(uuid.uuid4())},
        {"werkbank_id": str(uuid.uuid4())},
    ):
        n = lebenszeichen(q | aenderung)
        fehler_pruefen(post(client, instanz, "ereignisse/", n), 404, "NICHT_GEFUNDEN")
    assert WerkbankOperation.objects.count() == 0


def test_auftrag_einer_anderen_werkbank_ist_404(
    client, instanz, pbindung, projekt, auftrag, ausfuehrung
):
    anderer = _auftrag(projekt, "gehoert anderer")
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung) | {"auftrag_id": anderer.pk})
    fehler_pruefen(post(client, instanz, "ereignisse/", n), 404, "NICHT_GEFUNDEN")


def test_falsche_server_id_409(client, instanz, pbindung, auftrag, ausfuehrung):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung) | {"server_id": str(uuid.uuid4())}
    fehler_pruefen(post(client, instanz, "ereignisse/", lebenszeichen(q)), 409, "ZUSTANDSKONFLIKT")


# --- Lebenszeichen und Wiederholungsschutz --------------------------------------------------


def test_lebenszeichen_setzt_kontakt_und_speichert_nachweis_und_ereignis(
    client, instanz, pbindung, auftrag, ausfuehrung
):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung), sequenz=7)
    vorher = timezone.now()
    antwort = post(client, instanz, "ereignisse/", n)
    assert antwort.status_code == 200
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("ereignisse_antwort", daten) == []
    assert daten["ergebnis"] == "verarbeitet" and daten["technischer_zustand"] == "laeuft"
    assert daten["statusversion"] == 3 and daten["haltanforderung"] is None
    instanz.refresh_from_db()
    assert vorher <= instanz.letzter_kontakt <= timezone.now()
    op = WerkbankOperation.objects.get()
    assert op.art == "ereignis" and op.nutzlast_sha256 == n["inhaltshash"]
    assert op.ausgang_status == 200 and op.ausgang == daten
    e = WerkbankEreignis.objects.get()
    assert e.art == "lebenszeichen" and e.sequenz == 7 and e.operation == op
    assert e.ausfuehrung == ausfuehrung and e.verarbeitung == "angewendet"


def test_identische_wiederholung_liefert_gespeicherten_ausgang_ohne_zweite_wirkung(
    client, instanz, pbindung, auftrag, ausfuehrung
):
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung))
    erste = post(client, instanz, "ereignisse/", n)
    zweite = post(client, instanz, "ereignisse/", n)
    assert zweite.status_code == 200
    gleich = {k: v for k, v in erste.json().items() if k != "ergebnis"}
    assert {k: v for k, v in zweite.json().items() if k != "ergebnis"} == gleich
    assert zweite.json()["ergebnis"] == "wiederholt"
    assert werkbank_vertrag.schema_fehler("ereignisse_antwort", zweite.json()) == []
    assert WerkbankOperation.objects.count() == 1 and WerkbankEreignis.objects.count() == 1


def test_widerspruechliche_wiederholung_409(client, instanz, pbindung, auftrag, ausfuehrung):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    op = uuid.uuid4()
    erste = lebenszeichen(q, sequenz=1, operation_id=op)
    anders = lebenszeichen(q, sequenz=2, operation_id=op)
    assert post(client, instanz, "ereignisse/", erste).status_code == 200
    fehler_pruefen(post(client, instanz, "ereignisse/", anders), 409, "WIDERSPRUCH_WIEDERHOLUNG")
    assert WerkbankOperation.objects.count() == 1 and WerkbankEreignis.objects.count() == 1
    assert WerkbankOperation.objects.get().nutzlast_sha256 == erste["inhaltshash"]


def test_gleiche_operation_id_je_instanz_getrennt(
    client, akteur, instanz, pbindung, auftrag, ausfuehrung, projekt
):
    """Die operation_id gilt je Werkbank: eine andere Instanz mit derselben Kennung kollidiert nicht."""
    op = uuid.uuid4()
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung), operation_id=op)
    assert post(client, instanz, "ereignisse/", n).status_code == 200
    zweite, schluessel = werkbank.instanz_anlegen(akteur, name="Zweite", projekte=[projekt])
    zweite.klartext = schluessel
    zweite_pb = WerkbankProjektbindung.objects.create(instanz=zweite, projekt=projekt)
    a2 = _auftrag(projekt, "zweiter")
    z2 = WerkbankZuweisung.objects.create(auftrag=a2, instanz=zweite, projektbindung=zweite_pb)
    ausf2 = WerkbankAusfuehrung.objects.create(
        auftrag=a2, revision=1, zuweisung=z2, zuweisung_version=1, projektbindung=zweite_pb,
        projektbindung_version=1, snapshot={}, snapshot_sha256=HASH, zustand="laeuft",
    )  # fmt: skip
    n2 = lebenszeichen(quelle(zweite, zweite_pb, a2, ausf2), operation_id=op)
    assert post(client, zweite, "ereignisse/", n2).status_code == 200
    assert WerkbankOperation.objects.count() == 2


def test_gleiche_ereignis_id_mit_neuer_operation_id_erzeugt_keine_zweite_wirkung(
    client, instanz, pbindung, auftrag, ausfuehrung
):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    ereignis = uuid.uuid4()
    assert (
        post(client, instanz, "ereignisse/", lebenszeichen(q, 1, ereignis_id=ereignis)).status_code
        == 200
    )
    zweite = post(client, instanz, "ereignisse/", lebenszeichen(q, 1, ereignis_id=ereignis))
    assert zweite.status_code == 200 and zweite.json()["ergebnis"] == "wiederholt"
    assert WerkbankEreignis.objects.count() == 1


def test_gleiche_sequenz_mit_anderer_ereignis_id_409(
    client, instanz, pbindung, auftrag, ausfuehrung
):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    assert post(client, instanz, "ereignisse/", lebenszeichen(q, 4)).status_code == 200
    fehler_pruefen(
        post(client, instanz, "ereignisse/", lebenszeichen(q, 4)), 409, "ZUSTANDSKONFLIKT"
    )
    assert WerkbankEreignis.objects.count() == 1


def test_veraltetes_lebenszeichen_wird_historisch_quittiert_und_aendert_nichts(
    client, instanz, pbindung, auftrag, zuweisung, ausfuehrung
):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    WerkbankZuweisung.objects.filter(pk=zuweisung.pk).update(version=2)
    antwort = post(client, instanz, "ereignisse/", lebenszeichen(q))
    assert antwort.status_code == 200 and antwort.json()["ergebnis"] == "historisch_quittiert"
    assert WerkbankEreignis.objects.get().verarbeitung == "historisch"
    ausfuehrung.refresh_from_db()
    assert ausfuehrung.zustand == "laeuft" and ausfuehrung.statusversion == 3


# --- Noch nicht verfügbare Wirkungen --------------------------------------------------------


def test_uebernehmen_eines_nicht_freigegebenen_auftrags_speichert_nichts(
    client, instanz, pbindung, auftrag, zuweisung
):
    # (WP4 setzt ``uebernehmen`` um; der Auftrag steht hier in Arbeit, nicht „freigegeben“.)
    n = uebernehmen(quelle(instanz, pbindung, auftrag))
    fehler = fehler_pruefen(post(client, instanz, "uebernehmen/", n), 409, "ZUSTANDSKONFLIKT")
    assert fehler["naechste_handlung"]
    assert WerkbankOperation.objects.count() == 0
    auftrag.refresh_from_db()
    assert not WerkbankAusfuehrung.objects.filter(auftrag=auftrag).exists()


def test_uebernehmen_mit_veralteter_bindung_409(client, instanz, pbindung, auftrag, zuweisung):
    q = quelle(instanz, pbindung, auftrag) | {"projektbindung_version": 5}
    fehler = fehler_pruefen(post(client, instanz, "uebernehmen/", uebernehmen(q)), 409, "VERALTET")
    assert fehler["details"]["aktuelle_projektbindung_version"] == pbindung.version


def test_uebernehmen_ohne_zuweisung_an_diese_werkbank_404(client, instanz, pbindung, auftrag):
    n = uebernehmen(quelle(instanz, pbindung, auftrag))
    fehler_pruefen(post(client, instanz, "uebernehmen/", n), 404, "NICHT_GEFUNDEN")


def test_schritt_wird_seit_wp5_autorisiert(client, instanz, pbindung, auftrag, ausfuehrung):
    n = schritt(quelle(instanz, pbindung, auftrag, ausfuehrung), ausfuehrung)
    antwort = post(client, instanz, "schritt/", n)
    assert antwort.status_code == 200 and antwort.json()["erlaubt"] is True
    assert WerkbankOperation.objects.count() == 1


def test_schritt_mit_veralteter_zuweisung_409(
    client, instanz, pbindung, auftrag, zuweisung, ausfuehrung
):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    WerkbankZuweisung.objects.filter(pk=zuweisung.pk).update(version=2)
    fehler = fehler_pruefen(
        post(client, instanz, "schritt/", schritt(q, ausfuehrung)), 409, "VERALTET"
    )
    assert fehler["details"]["aktuelle_zuweisung_version"] == 2


def test_paket_status_wird_seit_wp5_verarbeitet(client, instanz, pbindung, auftrag, ausfuehrung):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    n = nachricht(
        "ereignisse",
        q,
        {
            "ereignis_id": str(uuid.uuid4()),
            "sequenz": 1,
            "statusversion": 4,
            "ereignis": {
                "art": "paket_status",
                "daten": {"paket": "root", "zustand": "laeuft", "versuche": 1},
            },
        },
    )
    antwort = post(client, instanz, "ereignisse/", n)
    assert antwort.status_code == 200 and antwort.json()["ergebnis"] == "verarbeitet"
    assert WerkbankEreignis.objects.count() == 1


# --- Wiederholung eines Schritts prüft den Berechtigungsstand erneut ------------------------


def _gespeicherter_schritt(instanz, pbindung, auftrag, ausfuehrung):
    q = quelle(instanz, pbindung, auftrag, ausfuehrung)
    n = schritt(q, ausfuehrung)
    WerkbankOperation.objects.create(
        instanz=instanz,
        operation_id=n["operation_id"],
        art="schritt",
        nutzlast_sha256=n["inhaltshash"],
        quelle=q,
        ausfuehrung=ausfuehrung,
        planfassung="vorplanung",
        paket="root",
        phase="planung",
        position=0,
        versuch_nr=1,
        eingabe_sha256=HASH,
        ausgang_status=200,
        ausgang={"erlaubt": True},
    )
    return n


def test_wiederholter_schritt_liefert_gespeicherten_erfolg_nur_bei_gleichem_stand(
    client, instanz, pbindung, auftrag, zuweisung, ausfuehrung
):
    n = _gespeicherter_schritt(instanz, pbindung, auftrag, ausfuehrung)
    ok = post(client, instanz, "schritt/", n)
    assert ok.status_code == 200 and ok.json() == {"erlaubt": True}
    WerkbankZuweisung.objects.filter(pk=zuweisung.pk).update(version=2)
    fehler_pruefen(post(client, instanz, "schritt/", n), 409, "VERALTET")


def test_wiederholter_schritt_nach_ende_der_ausfuehrung_409(
    client, instanz, pbindung, auftrag, ausfuehrung
):
    n = _gespeicherter_schritt(instanz, pbindung, auftrag, ausfuehrung)
    WerkbankAusfuehrung.objects.filter(pk=ausfuehrung.pk).update(zustand="abgebrochen")
    fehler_pruefen(post(client, instanz, "schritt/", n), 409, "ZUSTANDSKONFLIKT")


def test_wiederholter_schritt_nach_entzug_des_projekts_404(
    client, akteur, instanz, pbindung, auftrag, ausfuehrung, projekt_b
):
    n = _gespeicherter_schritt(instanz, pbindung, auftrag, ausfuehrung)
    werkbank.instanz_projekte_setzen(akteur, instanz, [projekt_b])
    fehler_pruefen(post(client, instanz, "schritt/", n), 404, "NICHT_GEFUNDEN")


# --- Interne Fehler, Bereitschaft, Erreichbarkeit -------------------------------------------


def test_interner_fehler_verraet_nichts_und_hinterlaesst_nichts(
    client, instanz, pbindung, auftrag, ausfuehrung, monkeypatch
):
    def kaputt(*_a, **_k):
        raise RuntimeError("geheim-xyz Traceback")

    monkeypatch.setitem(werkbank.HANDLER, "lebenszeichen", kaputt)
    n = lebenszeichen(quelle(instanz, pbindung, auftrag, ausfuehrung))
    antwort = post(client, instanz, "ereignisse/", n)
    fehler_pruefen(antwort, 500, "INTERNER_FEHLER")
    text = antwort.content.decode()
    assert "geheim-xyz" not in text and "Traceback" not in text and instanz.klartext not in text
    assert WerkbankOperation.objects.count() == 0 and WerkbankEreignis.objects.count() == 0


def test_erreichbarkeitswarnung_nach_90_sekunden(instanz):
    jetzt = timezone.now()
    assert werkbank.WARNUNG_S == 90 and werkbank.LEBENSZEICHEN_S == 30
    assert werkbank.nicht_erreichbar(instanz, jetzt) is False  # nie Kontakt: keine Warnung
    for alter, erwartet in ((89, False), (90, False), (91, True), (600, True)):
        instanz.letzter_kontakt = jetzt - timedelta(seconds=alter)
        assert werkbank.nicht_erreichbar(instanz, jetzt) is erwartet, alter


def test_bereitschaft_speichert_gemeldete_fingerabdruecke_ohne_regelpruefung(instanz, pbindung):
    nutzlast = {
        "repo_fingerabdruck": "repo-1",
        "konfig_fingerabdruecke": {"pruefprofil": HASH, "modelle": "b" * 64},
        "faehigkeiten": ["git", "pytest"],
        "werkbank_version": "1.2.3",
    }
    geaendert = werkbank.bereitschaft_melden(pbindung, nutzlast)
    pbindung.refresh_from_db()
    instanz.refresh_from_db()
    assert geaendert == []
    assert pbindung.repo_fingerabdruck == "repo-1" and pbindung.bereit is True
    assert pbindung.bereit_gruende == [] and pbindung.bereit_gemeldet_am is not None
    assert pbindung.konfig_fingerabdruecke == nutzlast["konfig_fingerabdruecke"]
    assert instanz.faehigkeiten == {"liste": ["git", "pytest"]}
    assert instanz.versionen == {"werkbank": "1.2.3"}
    # Normale Git-Fortschreibung (neuer Repo-Fingerabdruck) ändert die Konfiguration nicht.
    geaendert = werkbank.bereitschaft_melden(pbindung, nutzlast | {"repo_fingerabdruck": "repo-2"})
    assert geaendert == []
    neu = nutzlast["konfig_fingerabdruecke"] | {"pruefprofil": "c" * 64}
    assert werkbank.bereitschaft_melden(pbindung, nutzlast | {"konfig_fingerabdruecke": neu}) == [
        "pruefprofil"
    ]


def test_zugang_fuer_dienstmodule_unveraendert(bindung, projekt):
    assert dienst.bindung_finden(bindung.klartext) is not None

"""Werkbank-Anbindung WP2: Datenmodell A–G, Standardwerte und Eindeutigkeiten."""

import uuid

import pytest
from django.db import IntegrityError, transaction

from steuerung.models import (
    Auftrag,
    ServerKennung,
    WerkbankAktion,
    WerkbankAusfuehrung,
    WerkbankEreignis,
    WerkbankInstanz,
    WerkbankOperation,
    WerkbankProjektbindung,
    WerkbankZuweisung,
)
from tests.conftest import arbeitspaket
from tests.werkbank_hilfen import bindung_sichern

pytestmark = pytest.mark.django_db

HASH = "a" * 64


@pytest.fixture
def instanz(akteur, projekt):
    return WerkbankInstanz.objects.create(name="PC Fred")


@pytest.fixture
def pbindung(instanz, projekt):
    ort = projekt.arbeitsorte.first()
    return bindung_sichern(instanz=instanz, projekt=projekt, arbeitsort=ort)


@pytest.fixture
def auftrag(akteur, projekt):
    return arbeitspaket(akteur, projekt)


def _ausfuehrung(auftrag, pbindung, **felder):
    zuw = WerkbankZuweisung.objects.get_or_create(
        auftrag=auftrag, defaults={"instanz": pbindung.instanz, "projektbindung": pbindung}
    )[0]
    daten = {
        "auftrag": auftrag,
        "revision": auftrag.revision_nr,
        "zuweisung": zuw,
        "zuweisung_version": zuw.version,
        "projektbindung": pbindung,
        "projektbindung_version": pbindung.version,
        "snapshot": {},
        "snapshot_sha256": HASH,
    }
    return WerkbankAusfuehrung.objects.create(**(daten | felder))


def test_auftrag_ausfuehrungsweg_standard_chat(auftrag):
    assert auftrag.ausfuehrungsweg == Auftrag.WEG_CHAT == "chat"
    assert dict(Auftrag.WEGE) == {"chat": "Chat", "werkbank": "Werkbank"}
    assert Auftrag.objects.exclude(ausfuehrungsweg="chat").count() == 0


def test_instanz_standardwerte(instanz):
    assert isinstance(instanz.werkbank_id, uuid.UUID)
    assert instanz.aktiv is True
    assert instanz.letzter_kontakt is None
    assert instanz.faehigkeiten == {} and instanz.versionen == {}
    assert WerkbankInstanz.objects.create(name="Zweite").werkbank_id != instanz.werkbank_id


def test_projektbindung_standard_und_eindeutig(pbindung, instanz, projekt):
    assert pbindung.version == 1
    assert pbindung.bereit is False
    assert pbindung.bereit_gruende == [] and pbindung.konfig_fingerabdruecke == {}
    with pytest.raises(IntegrityError), transaction.atomic():
        WerkbankProjektbindung.objects.create(instanz=instanz, projekt=projekt)


def test_server_kennung_dauerhaft_und_einmalig():
    erste = ServerKennung.holen()
    assert isinstance(erste.server_id, uuid.UUID)
    assert ServerKennung.holen().server_id == erste.server_id
    assert ServerKennung.objects.count() == 1


def test_zuweisung_genau_ein_ziel_je_auftrag(auftrag, pbindung, instanz):
    WerkbankZuweisung.objects.create(auftrag=auftrag, instanz=instanz, projektbindung=pbindung)
    z = WerkbankZuweisung.objects.get(auftrag=auftrag)
    assert z.version == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        WerkbankZuweisung.objects.create(auftrag=auftrag, instanz=instanz, projektbindung=pbindung)


def test_ausfuehrung_standard_und_eine_aktive_je_auftrag(auftrag, pbindung):
    a = _ausfuehrung(auftrag, pbindung)
    assert isinstance(a.ausfuehrung_id, uuid.UUID)
    assert a.zustand == "plant" and a.statusversion == 0
    assert a.pakete == [] and a.halt_zweck == ""
    with pytest.raises(IntegrityError), transaction.atomic():
        _ausfuehrung(auftrag, pbindung)
    a.zustand = "abgebrochen"
    a.save()
    assert _ausfuehrung(auftrag, pbindung).pk != a.pk


def _aktion(a, owner, art, zustand_erwartet):
    return WerkbankAktion.objects.create(
        ausfuehrung=a,
        art=art,
        revision=1,
        zuweisung_version=1,
        projektbindung_version=1,
        erwarteter_zustand=zustand_erwartet,
        owner=owner,
    )


def test_aktion_standard_und_einmaliger_operationsbezug(auftrag, pbindung, instanz, owner):
    a = _ausfuehrung(auftrag, pbindung)
    aktion = _aktion(a, owner, "antworten", "wartet_antwort")
    assert isinstance(aktion.aktion_id, uuid.UUID)
    assert aktion.zustand == "ausstehend"
    op = WerkbankOperation.objects.create(
        instanz=instanz, operation_id=uuid.uuid4(), art="schritt", nutzlast_sha256=HASH
    )
    aktion.operation = op
    aktion.save()
    zweite = _aktion(a, owner, "abbrechen", "laeuft")
    zweite.operation = op
    with pytest.raises(IntegrityError), transaction.atomic():
        zweite.save()


def test_operation_instanz_und_operation_id_eindeutig(instanz):
    oid = uuid.uuid4()
    WerkbankOperation.objects.create(
        instanz=instanz, operation_id=oid, art="ereignis", nutzlast_sha256=HASH
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        WerkbankOperation.objects.create(
            instanz=instanz, operation_id=oid, art="ereignis", nutzlast_sha256=HASH
        )
    andere = WerkbankInstanz.objects.create(name="Andere")
    WerkbankOperation.objects.create(
        instanz=andere, operation_id=oid, art="ereignis", nutzlast_sha256=HASH
    )


def _schritt(instanz, a, versuch, **felder):
    daten = {
        "instanz": instanz,
        "operation_id": uuid.uuid4(),
        "art": "schritt",
        "nutzlast_sha256": HASH,
        "ausfuehrung": a,
        "planfassung": "vorplanung",
        "paket": "root",
        "phase": "plan",
        "position": 0,
        "versuch_nr": versuch,
        "eingabe_sha256": HASH,
    }
    return WerkbankOperation.objects.create(**(daten | felder))


def test_schrittschluessel_und_versuch_eindeutig(auftrag, pbindung, instanz):
    a = _ausfuehrung(auftrag, pbindung)
    erster = _schritt(instanz, a, 1, klaerung="geklaert")
    assert erster.klaerung == "geklaert"
    # Zwei UUIDs für denselben logischen Versuch erzeugen keine zweite Autorisierung.
    with pytest.raises(IntegrityError), transaction.atomic():
        _schritt(instanz, a, 1, klaerung="geklaert")
    # Anderer Versuch oder anderer Schritt (Position) sind zulässig.
    _schritt(instanz, a, 2, klaerung="geklaert")
    _schritt(instanz, a, 1, position=1, klaerung="geklaert")


def test_hoechstens_ein_ungeklaerter_versuch_je_schritt(auftrag, pbindung, instanz):
    a = _ausfuehrung(auftrag, pbindung)
    assert _schritt(instanz, a, 1).klaerung == "ungeklaert"
    with pytest.raises(IntegrityError), transaction.atomic():
        _schritt(instanz, a, 2)
    # Anderer Schritt hat eigenen Spielraum.
    _schritt(instanz, a, 1, position=1)
    # Nach Klärung des ersten Versuchs ist ein neuer erlaubt.
    WerkbankOperation.objects.filter(ausfuehrung=a, position=0, versuch_nr=1).update(
        klaerung="geklaert"
    )
    _schritt(instanz, a, 2)


def _ereignis(instanz, **felder):
    daten = {
        "instanz": instanz,
        "ereignis_id": uuid.uuid4(),
        "art": "lebenszeichen",
        "sequenz": 1,
        "inhalt_sha256": HASH,
    }
    return WerkbankEreignis.objects.create(**(daten | felder))


def test_ereignis_eindeutig_je_instanz_und_sequenz_je_ausfuehrung(auftrag, pbindung, instanz):
    a = _ausfuehrung(auftrag, pbindung)
    eid = uuid.uuid4()
    e = _ereignis(instanz, ereignis_id=eid, ausfuehrung=a, rueckmeldung={"x": 1})
    assert e.verarbeitung == "offen" and e.rueckmeldung == {"x": 1}
    with pytest.raises(IntegrityError), transaction.atomic():
        _ereignis(instanz, ereignis_id=eid, ausfuehrung=a, sequenz=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        _ereignis(instanz, ausfuehrung=a, sequenz=1)
    # Ohne Ausführung (z. B. Lebenszeichen der Instanz) gilt die Sequenzregel nicht.
    _ereignis(instanz)
    _ereignis(instanz)


def test_bestand_bleibt_chat_und_alte_wege_unveraendert(akteur, projekt, auftrag):
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ENTWURF and auftrag.ausfuehrungsweg == "chat"


def test_migration_0017_laesst_bestand_chat(transactional_db):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("steuerung", "0016_fruehere_nummern")])
    alt = executor.loader.project_state([("steuerung", "0016_fruehere_nummern")]).apps
    p = alt.get_model("steuerung", "Projekt").objects.create(
        kennung="alt", name="Alt", kuerzel="ALT"
    )
    a = alt.get_model("steuerung", "Auftrag").objects.create(projekt=p, titel="alt", revision_nr=1)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert Auftrag.objects.get(pk=a.pk).ausfuehrungsweg == "chat"


def test_neue_tabellen_zaehlen_in_bestand_mit():
    import importlib.util
    from pathlib import Path

    skript = Path(__file__).resolve().parents[2] / "03-Server" / "skripte" / "bestand.py"
    spec = importlib.util.spec_from_file_location("bestand", skript)
    bestand = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bestand)
    neu = {
        "serverkennung",
        "werkbankinstanz",
        "werkbankprojektbindung",
        "werkbankzuweisung",
        "werkbankausfuehrung",
        "werkbankaktion",
        "werkbankoperation",
        "werkbankereignis",
    }
    assert neu <= set(bestand.TABELLEN)

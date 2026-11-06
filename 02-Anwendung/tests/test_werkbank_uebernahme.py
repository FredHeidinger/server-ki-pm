"""Werkbank-Anbindung WP4: Zuweisung, Snapshot samt Kontexthash, Freigabe bindet die Kontextbasis,
atomares ``uebernehmen`` und Ausschluss aus der freien Chat-Übernahme."""

import json
import uuid

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst, werkbank, werkbank_vertrag
from steuerung.models import (
    Abhaengigkeit,
    Auftrag,
    Ereignis,
    WerkbankAusfuehrung,
    WerkbankOperation,
    WerkbankZuweisung,
)
from tests.conftest import arbeitspaket
from tests.werkbank_hilfen import bindung_sichern

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
    return bindung_sichern(instanz=instanz, projekt=projekt, arbeitsort=projekt.arbeitsorte.first())


@pytest.fixture
def entwurf(akteur, projekt):
    return arbeitspaket(akteur, projekt)


@pytest.fixture
def bereit(akteur, entwurf, pbindung):
    """Zugewiesen und freigegeben (atomar)."""
    werkbank.zuweisen(akteur, entwurf, pbindung, freigeben=True)
    entwurf.refresh_from_db()
    return entwurf


def kopf(instanz):
    return {"HTTP_AUTHORIZATION": f"Bearer {instanz.klartext}"} | KOPF


def quelle(instanz, pb, a, version=None):
    z = WerkbankZuweisung.objects.get(auftrag=a)
    return {
        "server_id": str(werkbank.server_id()),
        "werkbank_id": str(instanz.werkbank_id),
        "projektbindung_id": pb.pk,
        "projektbindung_version": pb.version,
        "auftrag_id": a.pk,
        "revision": a.revision_nr,
        "zuweisung_version": version or z.version,
    }


def nachricht(q, operation_id=None, repo="repo-1"):
    n = {
        "vertrag": "kipm-werkbank/1",
        "operation_id": str(operation_id or uuid.uuid4()),
        "art": "uebernehmen",
        "quelle": q,
        "nutzlast": {
            "repo_fingerabdruck": repo,
            "konfig_fingerabdruecke": {"pruefprofil": HASH},
            "faehigkeiten": ["git", "pytest"],
            "werkbank_version": "1.0.0",
        },
    }
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    return n


def senden(client, instanz, n):
    return client.post(
        BASIS + "uebernehmen/",
        data=json.dumps(n),
        content_type="application/json",
        **kopf(instanz),
    )


def uebernehmen(client, instanz, pb, a, **mehr):
    return senden(client, instanz, nachricht(quelle(instanz, pb, a), **mehr))


def code(antwort, status):
    assert antwort.status_code == status, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("fehler", daten) == []
    return daten["fehler"]["code"]


# --- Zuweisung -------------------------------------------------------------------------------


def test_zuweisen_reserviert_nur_das_ziel(akteur, entwurf, pbindung):
    z = werkbank.zuweisen(akteur, entwurf, pbindung)
    entwurf.refresh_from_db()
    assert z.version == 1 and z.projektbindung == pbindung and z.kontext_sha256 == ""
    assert entwurf.ausfuehrungsweg == Auftrag.WEG_WERKBANK
    assert entwurf.zustand == Auftrag.ENTWURF and not entwurf.hat_bearbeiter
    assert Ereignis.objects.filter(ereignis="Werkbank zugewiesen").exists()
    assert z.ereignis is not None and z.geaendert_von == akteur.name


def test_nur_der_owner_weist_zu(akteur, entwurf, pbindung, bindung):
    pc, _ = dienst.bindung_anlegen(akteur, None, name="PC")
    for fremd in (dienst.Akteur.aus_bindung(bindung), dienst.Akteur.aus_bindung(pc)):
        with pytest.raises(dienst.KeinZugriff):
            werkbank.zuweisen(fremd, entwurf, pbindung)
    assert not WerkbankZuweisung.objects.exists()


def test_ziel_muss_zum_projekt_gehoeren_und_aktiv_sein(
    akteur, entwurf, pbindung, instanz, projekt_b
):
    fremd = bindung_sichern(
        instanz=instanz, projekt=projekt_b, arbeitsort=projekt_b.arbeitsorte.first()
    )
    with pytest.raises(dienst.DienstFehler):
        werkbank.zuweisen(akteur, entwurf, fremd)  # anderes Projekt
    werkbank.instanz_aktiv_setzen(akteur, instanz, False)
    with pytest.raises(dienst.DienstFehler):
        werkbank.zuweisen(akteur, entwurf, pbindung)  # Instanz abgeschaltet
    assert not WerkbankZuweisung.objects.exists()
    entwurf.refresh_from_db()
    assert entwurf.ausfuehrungsweg == Auftrag.WEG_CHAT


def test_zuweisen_hebt_vorhandene_freigabe_auf(akteur, projekt, pbindung):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    werkbank.zuweisen(akteur, a, pbindung)
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and a.freigegeben_am is None


def test_freigeben_und_zuweisen_ist_atomar(akteur, entwurf, pbindung):
    z = werkbank.zuweisen(akteur, entwurf, pbindung, freigeben=True)
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.FREIGEGEBEN and entwurf.freigegeben_am is not None
    assert len(z.kontext_sha256) == 64 and not entwurf.hat_bearbeiter
    assert z.kontext_sha256 == werkbank.kontext_hash(werkbank.snapshot_bauen(entwurf))


def test_freigeben_und_zuweisen_scheitert_ganz_oder_gar_nicht(akteur, projekt, pbindung):
    wunsch = dienst.wunsch_erfassen(akteur, projekt, titel="Nur ein Wunsch", beschreibung="x")
    with pytest.raises(dienst.DienstFehler):
        werkbank.zuweisen(akteur, wunsch, pbindung, freigeben=True)
    wunsch.refresh_from_db()
    assert not WerkbankZuweisung.objects.exists()
    assert wunsch.ausfuehrungsweg == Auftrag.WEG_CHAT and wunsch.zustand == Auftrag.ENTWURF


def test_normale_freigabe_nach_zuweisung_bindet_die_kontextbasis(akteur, entwurf, pbindung):
    werkbank.zuweisen(akteur, entwurf, pbindung)
    dienst.auftrag_freigeben(akteur, entwurf)
    z = WerkbankZuweisung.objects.get(auftrag=entwurf)
    assert z.kontext_sha256 == werkbank.kontext_hash(werkbank.snapshot_bauen(entwurf))


def test_freigabe_zuruecknehmen_loest_bindung_und_erhoeht_fassung(akteur, bereit):
    dienst.freigabe_zuruecknehmen(akteur, bereit, "Neu denken")
    z = WerkbankZuweisung.objects.get(auftrag=bereit)
    assert z.version == 2 and z.kontext_sha256 == ""


def test_neue_revision_beendet_die_freigabe_und_erhoeht_die_fassung(akteur, bereit):
    dienst.auftrag_bearbeiten(
        akteur,
        bereit,
        titel=bereit.titel,
        beschreibung="Anders.",
        ziel="Anderes Ziel.",
        nicht_ziele="keine",
        arbeitsort=bereit.aktuelle_revision.arbeitsort,
        schreibumfang="src/auth/**",
        quellen="",
        abnahmekriterien="Eins",
        prioritaet="hoch",
        modul=bereit.modul,
    )
    bereit.refresh_from_db()
    z = WerkbankZuweisung.objects.get(auftrag=bereit)
    assert bereit.zustand == Auftrag.ENTWURF and z.version == 2 and z.kontext_sha256 == ""


def test_zielwechsel_erhoeht_fassung_und_nimmt_freigabe_zurueck(akteur, bereit, pbindung, projekt):
    zweite, _ = werkbank.instanz_anlegen(akteur, name="Werkbank 2", projekte=[projekt])
    ziel2 = bindung_sichern(instanz=zweite, projekt=projekt, arbeitsort=projekt.arbeitsorte.first())
    z = werkbank.zuweisen(akteur, bereit, ziel2)
    bereit.refresh_from_db()
    assert z.version == 2 and z.projektbindung == ziel2 and z.instanz == zweite
    assert z.kontext_sha256 == "" and bereit.zustand == Auftrag.ENTWURF
    # dasselbe Ziel noch einmal: keine neue Fassung
    assert werkbank.zuweisen(akteur, bereit, ziel2).version == 2


def test_zuweisung_aufheben_nur_ohne_ausfuehrung(akteur, bereit, pbindung):
    werkbank.zuweisung_aufheben(akteur, bereit)
    bereit.refresh_from_db()
    assert bereit.ausfuehrungsweg == Auftrag.WEG_CHAT and bereit.zustand == Auftrag.ENTWURF
    assert not WerkbankZuweisung.objects.exists()


def test_nach_beginn_der_ausfuehrung_kein_zielwechsel(
    client, akteur, instanz, pbindung, bereit, projekt
):
    assert uebernehmen(client, instanz, pbindung, bereit).status_code == 200
    with pytest.raises(dienst.Konflikt):
        werkbank.zuweisen(akteur, bereit, pbindung)
    with pytest.raises(dienst.Konflikt):
        werkbank.zuweisung_aufheben(akteur, bereit)


# --- Kontextbasis ----------------------------------------------------------------------------


def test_kontexthash_folgt_nur_fachlichem_kontext(akteur, projekt, bereit):
    vorher = werkbank.kontext_hash(werkbank.snapshot_bauen(bereit))
    # Fortschritt anderer Aufträge, Termine, Meilensteine, Projekt.inhalt_revision: kein Einfluss
    anderer = arbeitspaket(akteur, projekt, titel="Anderer", umfang="docs/**")
    dienst.auftrag_freigeben(akteur, anderer)
    akte.meilenstein_anlegen(akteur, projekt, name="M-A")
    Auftrag.objects.filter(pk=bereit.pk).update(faellig=timezone.localdate())
    assert werkbank.kontext_hash(werkbank.snapshot_bauen(bereit)) == vorher
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Neu entschieden", warum="Darum")
    nach_entscheidung = werkbank.kontext_hash(werkbank.snapshot_bauen(bereit))
    assert nach_entscheidung != vorher
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=None, zweck="Neuer Zweck")
    assert werkbank.kontext_hash(werkbank.snapshot_bauen(bereit)) != nach_entscheidung


def test_snapshot_ist_vollstaendig_und_vertragsgemaess(akteur, projekt, entwurf):
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=None, zweck="Zweck des Projekts")
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Ohne Abnahme", warum="Schlank")
    akte.text_anlegen(
        akteur, projekt, titel="Notiz", text="Hinweis kipm_0123456789ab_ABCDEFGHIJKLMNOP bleibt"
    )
    akte.link_anlegen(akteur, projekt, titel="Doku", uri="https://example.invalid/doku")
    s = werkbank.snapshot_bauen(entwurf)
    assert werkbank_vertrag.schema_fehler("auftragssnapshot", s) == []
    assert s["auftrag_id"] == entwurf.pk and s["revision"] == entwurf.revision_nr
    assert [k["id"] for k in s["abnahmekriterien"]] == ["AK-1", "AK-2"]
    assert s["schreibumfang"] == ["src/auth/**"] and s["nummer"] == entwurf.nummer
    assert s["arbeitsort"]["id"] == projekt.arbeitsorte.first().pk
    assert s["profil"]["fassung"] == Auftrag.objects.get(pk=entwurf.pk).projekt.profil_revision
    assert [e["titel"] for e in s["entscheidungen"]] == ["Ohne Abnahme"]
    assert s["entscheidungen"][0]["version"] >= 1 and s["entscheidungen"][0]["warum"] == "Schlank"
    assert [t["titel"] for t in s["texte"]] == ["Notiz"]
    assert "kipm_0123456789ab" not in json.dumps(s) and s["texte"][0]["version"] == 1
    assert [x["url"] for x in s["links"]] == ["https://example.invalid/doku"]
    assert werkbank_vertrag.sha256_hex(s) == werkbank_vertrag.sha256_hex(json.loads(json.dumps(s)))


# --- Ausschluss aus der freien Chat-Übernahme ------------------------------------------------


def test_werkbank_auftrag_wird_chats_nicht_angeboten(akteur, projekt, bereit, bindung):
    assert bereit not in dienst.weiter_reihenfolge(projekt)
    frei = arbeitspaket(akteur, projekt, titel="Frei", umfang="docs/**")
    dienst.auftrag_freigeben(akteur, frei)
    assert dienst.weiter_reihenfolge(projekt) == [frei]
    chat = dienst.Akteur.aus_bindung(bindung)
    with pytest.raises(dienst.Konflikt) as fehler:
        dienst.auftrag_uebernehmen(chat, bereit, bereit.revision_nr)
    assert "Werkbank" in str(fehler.value)
    with pytest.raises(dienst.Konflikt):
        dienst.auftrag_uebernehmen(akteur, bereit, bereit.revision_nr)
    bereit.refresh_from_db()
    assert not bereit.hat_bearbeiter and bereit.zustand == Auftrag.FREIGEGEBEN


def test_sitzungsstart_bietet_werkbank_auftrag_nicht_als_naechsten_an(client, bereit, bindung):
    antwort = client.get(
        reverse("api_sitzungsstart"), HTTP_AUTHORIZATION=f"Bearer {bindung.klartext}"
    )
    daten = antwort.json()
    assert daten["naechster"] is None
    assert all(not e.get("uebernehmbar") for e in daten["offen"])


# --- POST uebernehmen ------------------------------------------------------------------------


def test_uebernehmen_erzeugt_genau_eine_ausfuehrung(client, instanz, pbindung, bereit):
    antwort = uebernehmen(client, instanz, pbindung, bereit)
    assert antwort.status_code == 200, antwort.content
    daten = antwort.json()
    assert werkbank_vertrag.schema_fehler("uebernehmen_antwort", daten) == []
    assert daten["ergebnis"] == "uebernommen" and daten["statusversion"] == 1
    ausf = WerkbankAusfuehrung.objects.get()
    assert str(ausf.ausfuehrung_id) == daten["quelle"]["ausfuehrung_id"]
    assert ausf.snapshot == daten["snapshot"] and ausf.zustand == "plant"
    assert ausf.snapshot_sha256 == werkbank_vertrag.sha256_hex(daten["snapshot"])
    assert ausf.snapshot_sha256 == daten["quelle"]["snapshot_sha256"]
    assert ausf.revision == bereit.revision_nr and ausf.zuweisung_version == 1
    assert ausf.statusversion == 1
    bereit.refresh_from_db()
    assert bereit.zustand == Auftrag.IN_ARBEIT and bereit.hat_bearbeiter
    assert "Werkbank PC" in bereit.bearbeiter_name
    assert bereit.uebernommene_revision == bereit.revision_nr and bereit.uebernommen_am
    assert Ereignis.objects.filter(objekt_kennung=bereit.nummer, ereignis="Übernommen").exists()
    op = WerkbankOperation.objects.get()
    assert op.ausgang_status == 200 and op.ausfuehrung == ausf
    pbindung.refresh_from_db()
    assert pbindung.repo_fingerabdruck == "repo-1" and pbindung.bereit


def test_wiederholung_liefert_dieselbe_ausfuehrung(client, instanz, pbindung, bereit):
    op = uuid.uuid4()
    erste = uebernehmen(client, instanz, pbindung, bereit, operation_id=op)
    zweite = uebernehmen(client, instanz, pbindung, bereit, operation_id=op)
    assert zweite.status_code == 200 and zweite.json()["ergebnis"] == "wiederholt"
    assert zweite.json()["quelle"] == erste.json()["quelle"]
    assert WerkbankAusfuehrung.objects.count() == 1
    assert Ereignis.objects.filter(ereignis="Übernommen").count() == 1


def test_neue_operation_nach_erfolg_ist_zustandskonflikt(client, instanz, pbindung, bereit):
    q = quelle(instanz, pbindung, bereit)
    assert senden(client, instanz, nachricht(q)).status_code == 200
    assert code(senden(client, instanz, nachricht(q)), 409) == "ZUSTANDSKONFLIKT"
    assert WerkbankAusfuehrung.objects.count() == 1


def test_nicht_freigegeben_wird_abgelehnt(client, akteur, instanz, pbindung, bereit):
    dienst.freigabe_zuruecknehmen(akteur, bereit)
    q = quelle(instanz, pbindung, bereit)
    assert code(senden(client, instanz, nachricht(q)), 409) in {"VERALTET", "ZUSTANDSKONFLIKT"}
    assert not WerkbankAusfuehrung.objects.exists()


def test_geaenderter_kontext_nach_freigabe_sperrt_die_uebernahme(
    client, akteur, projekt, instanz, pbindung, bereit
):
    akte.entscheidung_eintragen(akteur, projekt, entscheidung="Spät geändert", warum="x")
    assert code(uebernehmen(client, instanz, pbindung, bereit), 409) == "KONTEXT_VERALTET"
    bereit.refresh_from_db()
    assert bereit.zustand == Auftrag.FREIGEGEBEN and not WerkbankAusfuehrung.objects.exists()


def test_freigabe_ohne_kontextbindung_reicht_nicht(client, instanz, pbindung, bereit):
    WerkbankZuweisung.objects.filter(auftrag=bereit).update(kontext_sha256="")
    assert code(uebernehmen(client, instanz, pbindung, bereit), 409) == "KONTEXT_VERALTET"


def test_offener_vorgaenger_blockiert(client, akteur, projekt, instanz, pbindung, bereit):
    vorgaenger = arbeitspaket(akteur, projekt, titel="Zuerst", umfang="docs/**")
    Abhaengigkeit.objects.create(auftrag=bereit, vorgaenger=vorgaenger)
    assert code(uebernehmen(client, instanz, pbindung, bereit), 409) == "ZUSTANDSKONFLIKT"
    assert not WerkbankAusfuehrung.objects.exists()


def test_umfangsueberschneidung_blockiert(client, akteur, projekt, instanz, pbindung, bereit):
    anderer = arbeitspaket(akteur, projekt, titel="Parallel", umfang="src/auth/**")
    Auftrag.objects.filter(pk=anderer.pk).update(zustand=Auftrag.IN_ARBEIT)
    assert code(uebernehmen(client, instanz, pbindung, bereit), 409) == "ZUSTANDSKONFLIKT"


def test_archiviertes_projekt_blockiert(client, instanz, pbindung, bereit, projekt):
    type(projekt).objects.filter(pk=projekt.pk).update(aktiv=False)
    assert uebernehmen(client, instanz, pbindung, bereit).status_code in {404, 409}
    assert not WerkbankAusfuehrung.objects.exists()


def test_fremder_bearbeiter_blockiert(client, akteur, bindung, instanz, pbindung, bereit):
    Auftrag.objects.filter(pk=bereit.pk).update(
        bearbeiter_bindung=bindung, bearbeiter_name="Chat A"
    )
    assert code(uebernehmen(client, instanz, pbindung, bereit), 409) == "ZUSTANDSKONFLIKT"


def test_veraltete_zuweisungsfassung_409(client, akteur, instanz, pbindung, bereit):
    q = quelle(instanz, pbindung, bereit)
    q["zuweisung_version"] = 7
    assert code(senden(client, instanz, nachricht(q)), 409) == "VERALTET"


def test_fehler_mitten_im_vorgang_nimmt_alles_zurueck(
    client, instanz, pbindung, bereit, monkeypatch
):
    echt = dienst._ereignis

    def kaputt(*args, **kwargs):
        if kwargs.get("ereignis") == "Übernommen":
            raise RuntimeError("Absturz")
        return echt(*args, **kwargs)

    monkeypatch.setattr(dienst, "_ereignis", kaputt)
    client.raise_request_exception = False
    antwort = uebernehmen(client, instanz, pbindung, bereit)
    assert antwort.status_code >= 500
    bereit.refresh_from_db()
    assert bereit.zustand == Auftrag.FREIGEGEBEN and not bereit.hat_bearbeiter
    assert not WerkbankAusfuehrung.objects.exists() and not WerkbankOperation.objects.exists()


def test_abgleich_nennt_die_ausfuehrung_nach_der_uebernahme(client, instanz, pbindung, bereit):
    uebernehmen(client, instanz, pbindung, bereit)
    abgleich = client.get(BASIS + "abgleich/", **kopf(instanz)).json()
    ausf = WerkbankAusfuehrung.objects.get()
    eintrag = next(z for z in abgleich["zuweisungen"] if z["auftrag_id"] == bereit.pk)
    assert eintrag["ausfuehrung_id"] == str(ausf.ausfuehrung_id)


def test_werkbank_auftrag_gilt_nicht_als_verstummt(client, instanz, pbindung, bereit):
    uebernehmen(client, instanz, pbindung, bereit)
    Auftrag.objects.filter(pk=bereit.pk).update(
        uebernommen_am=timezone.now() - timezone.timedelta(days=3)
    )
    bereit.refresh_from_db()
    assert dienst.verstummt(bereit) is None


def test_gewoehnliche_chat_uebernahme_bleibt_unveraendert(akteur, projekt, bindung):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), a, a.revision_nr)
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT and a.ausfuehrungsweg == Auftrag.WEG_CHAT

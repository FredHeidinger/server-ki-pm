"""KIPM-36 (KP02-Rest): Vertrag kipm-werkbank/2 = Fassung 1 plus W1-Delegation und Zielverbund.

Erwartungen aus dem Auftrag und vertrag/kipm-werkbank-2/VERTRAG.md, vor dem Code geschrieben:
Aushandlung nennt Fassung 2; eine Ausführung bleibt bei ihrer Fassung (B16); delegierte Aufträge
laufen nur über Fassung 2, Start- und Integrationsfreigabe kommen aus der Erlaubnis (A01, A05);
der Zielverbund nimmt nur saubere Mitglieder auf (X07, X08), entwertet die Freigabe bei
Änderung (X12) und verlangt Integration je Repository (X10)."""

import json
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import dienst, werkbank, werkbank_vertrag
from steuerung.models import Auftrag, WerkbankAusfuehrung, WerkbankProjektbindung
from tests import werkbank_hilfen as h
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db
V1, V2 = "kipm-werkbank/1", "kipm-werkbank/2"
COMMIT_A, COMMIT_B = "a" * 40, "b" * 40
PLAN = "d" * 64
CAP = {"waehrung": "USD", "geld_mikro": 5_000_000, "zeit_ms": 600_000, "aufrufe": 10}


# --- Aufbau ----------------------------------------------------------------------------------


@pytest.fixture
def inst(akteur, projekt, projekt_b):
    i, schluessel = werkbank.instanz_anlegen(
        akteur, name="Werkbank PC", projekte=[projekt, projekt_b]
    )
    i.klartext = schluessel
    return i


@pytest.fixture
def pb_a(inst, projekt):
    ort = projekt.arbeitsorte.first()
    ort.pfad = "C:/repos/alpha"
    ort.save()
    return h.bindung_sichern(instanz=inst, projekt=projekt, arbeitsort=ort)


@pytest.fixture
def pb_b(inst, projekt_b):
    ort = projekt_b.arbeitsorte.first()
    ort.pfad = "C:/repos/beta"
    ort.save()
    return h.bindung_sichern(instanz=inst, projekt=projekt_b, arbeitsort=ort)


def nachricht(art, q, nutzlast, vertrag=V2, operation_id=None):
    n = {
        "vertrag": vertrag,
        "operation_id": str(operation_id or uuid.uuid4()),
        "art": art,
        "quelle": q,
        "nutzlast": nutzlast,
    }
    n["inhaltshash"] = werkbank_vertrag.inhaltshash(n)
    return n


def repos(*eintraege):
    return [
        {"repo_id": f"repo-{pb.pk}", "basiscommit": c, "repo_fingerabdruck": f"fp-{pb.pk}"}
        for pb, c in eintraege
    ]


def uebernahme(inst, pb, a, *, vertrag=V2, repo_liste=None):
    nutzlast = {
        "repo_fingerabdruck": "repo-1",
        "konfig_fingerabdruecke": {"pruefprofil": "a" * 64},
        "faehigkeiten": ["git", "pytest"],
        "werkbank_version": "2.0.0",
    }
    if vertrag == V2:
        nutzlast["repos"] = repo_liste if repo_liste is not None else repos((pb, COMMIT_A))
    return nachricht("uebernehmen", h.vor_quelle(inst, pb, a), nutzlast, vertrag)


def ereignis(ausf, inst, art, daten, *, vertrag=V2):
    ausf.refresh_from_db()
    return nachricht(
        "ereignisse",
        h.quelle_von(ausf, inst),
        {
            "ereignis_id": str(uuid.uuid4()),
            "sequenz": ausf.ereignisse.count() + 1,
            "statusversion": None,
            "ereignis": {"art": art, "daten": daten},
        },
        vertrag,
    )


def schritt(ausf, inst, *, paket="p1", versuch=1, planfassung=PLAN, vertrag=V2, aktion=None):
    ausf.refresh_from_db()
    return nachricht(
        "schritt",
        h.quelle_von(ausf, inst),
        {
            "schritt": {
                "ausfuehrung_id": str(ausf.ausfuehrung_id),
                "planfassung": planfassung,
                "paket": paket,
                "phase": "bauen",
                "position": 0,
            },
            "versuch_nr": versuch,
            "eingabehash": "a" * 64,
            "wirkung": "schreiben",
            "owner_aktion_id": str(aktion) if aktion else None,
        },
        vertrag,
    )


def post(client, inst, pfad, n, vertrag=V2):
    return client.post(
        h.BASIS + pfad,
        data=json.dumps(n),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {inst.klartext}",
        HTTP_X_KIPM_VERTRAG=vertrag,
    )


def abgleich(client, inst, vertrag=V2):
    return client.get(
        h.BASIS + "abgleich/",
        HTTP_AUTHORIZATION=f"Bearer {inst.klartext}",
        HTTP_X_KIPM_VERTRAG=vertrag,
    )


def freigegeben(akteur, projekt, pb, *, verbund=(), titel="Verbundauftrag"):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel.lower()}/**")
    werkbank.zuweisen(akteur, a, pb)
    if verbund:
        werkbank.verbund_setzen(akteur, a, list(verbund))
    a.refresh_from_db()
    dienst.auftrag_freigeben(akteur, a)
    a.refresh_from_db()
    return a


def uebernommen(client, akteur, inst, projekt, pb, *, verbund=(), repo_liste=None):
    a = freigegeben(akteur, projekt, pb, verbund=verbund)
    liste = repo_liste or repos((pb, COMMIT_A), *[(m, COMMIT_B) for m in verbund])
    antwort = post(client, inst, "uebernehmen/", uebernahme(inst, pb, a, repo_liste=liste))
    assert antwort.status_code == 200, antwort.content
    return a, WerkbankAusfuehrung.objects.get(auftrag=a), antwort.json()


def plan(client, inst, ausf, pakete):
    n = ereignis(ausf, inst, "plan_bestaetigt", {"planfassung": PLAN, "pakete": pakete})
    return post(client, inst, "ereignisse/", n)


# --- Kriterium 1: Fassung, Aushandlung, B16 ----------------------------------------------------


def test_aushandlung_nennt_beide_fassungen(client, inst):
    url = "/api/werkbank/faehigkeiten/"
    auth = {"HTTP_AUTHORIZATION": f"Bearer {inst.klartext}"}
    beide = client.get(url, {"vertrag": [V1, V2]}, **auth)
    assert beide.status_code == 200 and beide.json()["gemeinsam"] == [V1, V2]
    nur2 = client.get(url, {"vertrag": [V2]}, **auth)
    assert nur2.status_code == 200 and nur2.json()["gemeinsam"] == [V2]
    assert client.get(url, {"vertrag": ["kipm-werkbank/3"]}, **auth).status_code == 426


def test_fassung_1_bleibt_bytegleich_und_fassung_2_ist_erzeugt():
    import subprocess
    import sys
    from pathlib import Path

    stamm = Path(werkbank_vertrag.__file__).resolve().parents[1]
    geaendert = subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "vertrag/kipm-werkbank-1"], cwd=stamm, check=False
    )
    assert geaendert.returncode == 0
    sys.path.insert(0, str(stamm / "vertrag" / "kipm-werkbank-2"))
    try:
        import erzeugen
    finally:
        sys.path.pop(0)
    for name, schema in erzeugen.erzeugen().items():
        assert (erzeugen.NEU / name).read_text("utf-8") == erzeugen.text(schema), name


def test_beispiele_der_fassung_2_passen_zu_den_schemas():
    from pathlib import Path

    ordner = Path(werkbank_vertrag.__file__).resolve().parents[1] / "vertrag/kipm-werkbank-2"
    beispiele = sorted((ordner / "beispiele").glob("*.json"))
    assert len(beispiele) >= 6
    for pfad in beispiele:
        if pfad.name == "hash-vektoren.json":
            continue
        name = pfad.name.split(".")[0]
        fehler = werkbank_vertrag.schema_fehler(
            name, json.loads(pfad.read_text("utf-8")), vertrag=V2
        )
        assert (not fehler) == pfad.name.endswith(".gut.json"), (pfad.name, fehler)


def test_einfacher_auftrag_laeuft_ueber_fassung_2_und_bleibt_dabei(
    client, akteur, inst, projekt, pb_a
):
    a, ausf, antwort = uebernommen(client, akteur, inst, projekt, pb_a)
    assert antwort["vertrag"] == V2 and ausf.vertrag == V2
    snap = antwort["snapshot"]
    assert not werkbank_vertrag.schema_fehler("auftragssnapshot", snap, vertrag=V2)
    assert snap["snapshot_version"] == 2
    assert snap["zustaendigkeit"] == {
        "weg": "W1",
        "fuehrendes_projekt": projekt.kennung,
        "erlaubnis": None,
    }
    assert [m["repo_id"] for m in snap["zielverbund"]] == [f"repo-{pb_a.pk}"]
    assert snap["zielverbund"][0]["fuehrend"] is True
    # Eine Nachricht in Fassung 1 zu dieser Ausführung wird abgewiesen, nichts gespeichert.
    alt = ereignis(ausf, inst, "lebenszeichen", {}, vertrag=V1)
    fehler = post(client, inst, "ereignisse/", alt, vertrag=V1)
    assert fehler.status_code == 409 and fehler.json()["fehler"]["code"] == "VERTRAG_INKOMPATIBEL"
    assert not ausf.ereignisse.exists()


def test_abgleich_in_fassung_2_ist_schemagueltig(client, akteur, inst, projekt, pb_a):
    freigegeben(akteur, projekt, pb_a)
    antwort = abgleich(client, inst)
    assert antwort.status_code == 200
    daten = antwort.json()
    assert daten["vertrag"] == V2 and len(daten["zuweisungen"]) == 1
    assert not werkbank_vertrag.schema_fehler("abgleich_antwort", daten, vertrag=V2)


def test_uebernahme_verlangt_genau_die_repos_des_verbunds(
    client, akteur, inst, projekt, pb_a, pb_b
):
    a = freigegeben(akteur, projekt, pb_a, verbund=[pb_b])
    for liste in (
        repos((pb_a, COMMIT_A)),
        repos((pb_a, COMMIT_A), (pb_b, COMMIT_B), (pb_b, COMMIT_B)),
        [*repos((pb_a, COMMIT_A)), {**repos((pb_b, COMMIT_B))[0], "repo_id": "repo-99999"}],
    ):
        antwort = post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a, repo_liste=liste))
        assert antwort.status_code == 409, liste
    assert not WerkbankAusfuehrung.objects.filter(auftrag=a).exists()


# --- Kriterium 2: W1-Delegation ----------------------------------------------------------------


@pytest.fixture
def delegiert(akteur, projekt, bindung, inst, pb_a):
    pb_a.repo_fingerabdruck = "repo-delegiert"
    pb_a.bereit = True
    pb_a.save()

    def anlegen(aktionen=("auftrag_anlegen", "freigeben", "zuweisen", "starten", "integrieren")):
        d = werkbank.delegation_erteilen(
            akteur,
            projekt,
            bindung,
            aktionen=list(aktionen),
            ziele=[pb_a],
            budget=CAP,
            gueltig_bis=timezone.now() + timedelta(days=1),
            standard_ziel=pb_a,
        )
        a = werkbank.delegation_auftrag_anlegen(
            dienst.Akteur.aus_bindung(bindung),
            projekt,
            delegation_id=d.pk,
            revision=d.revision,
            budget={"waehrung": "USD", "geld_mikro": 1_000_000, "zeit_ms": 1000, "aufrufe": 1},
            titel="Delegiert erstellen",
            beschreibung="Anmeldehinweis verbessern",
            planung={
                "ziel": "Klarer Hinweis",
                "nicht_ziele": "keine",
                "schreibumfang": "src/auth/**",
                "abnahmekriterien": "Hinweis sichtbar",
            },
            freigeben=True,
        )
        assert a.zustand == Auftrag.FREIGEGEBEN
        return d, a

    return anlegen


def test_delegierter_auftrag_nur_ueber_fassung_2(client, inst, pb_a, delegiert):
    d, a = delegiert()
    assert not abgleich(client, inst, V1).json()["zuweisungen"]
    assert [z["auftrag_id"] for z in abgleich(client, inst).json()["zuweisungen"]] == [a.pk]
    alt = post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a, vertrag=V1), vertrag=V1)
    assert alt.status_code == 409
    antwort = post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a))
    assert antwort.status_code == 200, antwort.content
    erlaubnis = antwort.json()["snapshot"]["zustaendigkeit"]["erlaubnis"]
    assert erlaubnis["id"] == d.pk and erlaubnis["revision"] == d.revision
    assert "starten" in erlaubnis["aktionen"]


def _startfreigabe_verlangt(client, inst, ausf):
    assert plan(client, inst, ausf, [{"paket": "p1", "titel": "Bau", "zustand": "offen",
                                      "repo_id": f"repo-{ausf.projektbindung_id}"}]
                ).status_code == 200  # fmt: skip
    n = ereignis(
        ausf,
        inst,
        "freigabe_noetig",
        {"freigabe_id": "f1", "art": "start", "pakete": ["p1"], "grund": "Bau beginnt"},
    )
    assert post(client, inst, "ereignisse/", n).status_code == 200


def test_erlaubnis_mit_starten_gibt_den_start_frei(client, akteur, inst, pb_a, delegiert):
    d, a = delegiert()
    post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a))
    ausf = WerkbankAusfuehrung.objects.get(auftrag=a)
    _startfreigabe_verlangt(client, inst, ausf)
    antwort = post(client, inst, "schritt/", schritt(ausf, inst))
    assert antwort.status_code == 200 and antwort.json()["erlaubt"] is True, antwort.content
    assert "W1-Erlaubnis" in antwort.json()["text"]


def test_erlaubnis_ohne_starten_wartet_auf_fred(client, inst, pb_a, delegiert):
    _, a = delegiert(aktionen=("auftrag_anlegen", "freigeben", "zuweisen"))
    post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a))
    ausf = WerkbankAusfuehrung.objects.get(auftrag=a)
    _startfreigabe_verlangt(client, inst, ausf)
    antwort = post(client, inst, "schritt/", schritt(ausf, inst)).json()
    assert antwort["erlaubt"] is False and antwort["grund_code"] == "FREIGABE_AUSSTEHEND"


def test_abgelaufene_erlaubnis_lehnt_neue_schritte_ab(client, inst, pb_a, delegiert):
    from steuerung.models import WerkbankChatDelegationsfassung

    d, a = delegiert()
    post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a))
    ausf = WerkbankAusfuehrung.objects.get(auftrag=a)
    WerkbankChatDelegationsfassung.objects.filter(delegation=d).update(
        gueltig_bis=timezone.now() - timedelta(minutes=1)
    )
    antwort = post(client, inst, "schritt/", schritt(ausf, inst, paket="root",
                                                     planfassung="vorplanung")).json()  # fmt: skip
    assert antwort["erlaubt"] is False and antwort["grund_code"] == "ERLAUBNIS_UNGUELTIG"


def test_widerruf_haelt_die_laufende_ausfuehrung_an(client, akteur, inst, pb_a, delegiert):
    d, a = delegiert()
    post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a))
    ausf = WerkbankAusfuehrung.objects.get(auftrag=a)
    werkbank.delegation_widerrufen(akteur, d, erwartet_revision=d.revision)
    antwort = post(client, inst, "schritt/", schritt(ausf, inst, paket="root",
                                                     planfassung="vorplanung"))  # fmt: skip
    assert antwort.status_code == 409 and antwort.json()["fehler"]["code"] == "HALT_ANGEFORDERT"


# --- Kriterium 3: Zielverbund ------------------------------------------------------------------


def test_verbund_setzen_und_snapshot_nennt_jedes_mitglied(
    client, akteur, inst, projekt, projekt_b, pb_a, pb_b
):
    a, ausf, antwort = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    verbund = antwort["snapshot"]["zielverbund"]
    assert [(m["repo_id"], m["fuehrend"], m["projekt"]) for m in verbund] == [
        (f"repo-{pb_a.pk}", True, projekt.kennung),
        (f"repo-{pb_b.pk}", False, projekt_b.kennung),
    ]
    assert verbund[1]["projektbindung_version"] == pb_b.version
    assert [e["repos"] for e in antwort["snapshot"]["integrationseinheiten"]] == [
        [f"repo-{pb_a.pk}"],
        [f"repo-{pb_b.pk}"],
    ]


def test_verbund_aendern_hebt_die_freigabe_auf(akteur, projekt, pb_a, pb_b):
    a = freigegeben(akteur, projekt, pb_a)
    version = a.zuweisung.version
    werkbank.verbund_setzen(akteur, a, [pb_b])
    a.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF and a.zuweisung.version == version + 1


@pytest.mark.parametrize("fall", ["haupt", "doppelt", "unbestaetigt", "alias", "verschachtelt"])
def test_unsaubere_mitglieder_werden_abgewiesen(akteur, projekt, projekt_b, pb_a, pb_b, fall):
    a = arbeitspaket(akteur, projekt, titel="Mitglieder", umfang="m/**")
    werkbank.zuweisen(akteur, a, pb_a)
    mitglieder = [pb_b]
    ort = pb_b.arbeitsort
    if fall == "haupt":
        mitglieder = [pb_a]
    elif fall == "doppelt":
        mitglieder = [pb_b, pb_b]
    elif fall == "unbestaetigt":
        ort.bestaetigt = False
        ort.save()
    elif fall == "alias":
        ort.pfad = "c:\\repos\\ALPHA\\"
        ort.save()
    else:
        ort.pfad = "C:/repos/alpha/unter"
        ort.save()
    with pytest.raises(dienst.DienstFehler):
        werkbank.verbund_setzen(akteur, a, mitglieder)
    assert not a.zuweisung.verbund.exists()


def test_fremde_werkbank_ist_kein_mitglied(akteur, projekt, projekt_b, pb_a):
    andere, _ = werkbank.instanz_anlegen(akteur, name="Andere", projekte=[projekt_b])
    fremd = WerkbankProjektbindung.objects.get(instanz=andere, projekt=projekt_b)
    fremd.arbeitsort = projekt_b.arbeitsorte.first()
    fremd.save()
    a = arbeitspaket(akteur, projekt, titel="Fremd", umfang="f/**")
    werkbank.zuweisen(akteur, a, pb_a)
    with pytest.raises(dienst.DienstFehler):
        werkbank.verbund_setzen(akteur, a, [fremd])


def test_nur_fred_setzt_den_verbund(akteur, projekt, bindung, pb_a, pb_b):
    a = arbeitspaket(akteur, projekt, titel="Rechte", umfang="r/**")
    werkbank.zuweisen(akteur, a, pb_a)
    with pytest.raises(dienst.KeinZugriff):
        werkbank.verbund_setzen(dienst.Akteur.aus_bindung(bindung), a, [pb_b])


def test_verbundauftrag_nicht_ueber_fassung_1(client, akteur, inst, projekt, pb_a, pb_b):
    a = freigegeben(akteur, projekt, pb_a, verbund=[pb_b])
    assert not abgleich(client, inst, V1).json()["zuweisungen"]
    alt = post(client, inst, "uebernehmen/", uebernahme(inst, pb_a, a, vertrag=V1), vertrag=V1)
    assert alt.status_code == 409 and alt.json()["fehler"]["code"] == "VERTRAG_INKOMPATIBEL"


def test_mitglied_geaendert_nach_freigabe(client, akteur, inst, projekt, pb_a, pb_b):
    a = freigegeben(akteur, projekt, pb_a, verbund=[pb_b])
    WerkbankProjektbindung.objects.filter(pk=pb_b.pk).update(version=pb_b.version + 1)
    n = uebernahme(inst, pb_a, a, repo_liste=repos((pb_a, COMMIT_A), (pb_b, COMMIT_B)))
    antwort = post(client, inst, "uebernehmen/", n)
    assert antwort.status_code == 409 and antwort.json()["fehler"]["code"] == "KONTEXT_VERALTET"


def test_mitglied_geaendert_waehrend_der_arbeit(client, akteur, inst, projekt, pb_a, pb_b):
    a, ausf, _ = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    WerkbankProjektbindung.objects.filter(pk=pb_b.pk).update(version=pb_b.version + 1)
    antwort = post(client, inst, "schritt/", schritt(ausf, inst, paket="root",
                                                     planfassung="vorplanung")).json()  # fmt: skip
    assert antwort["erlaubt"] is False and antwort["grund_code"] == "MITGLIED_GEAENDERT"


def test_paket_braucht_ein_repo_des_verbunds(client, akteur, inst, projekt, pb_a, pb_b):
    a, ausf, _ = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    falsch = plan(client, inst, ausf, [{"paket": "p1", "titel": "x", "zustand": "offen",
                                        "repo_id": "repo-99999"}])  # fmt: skip
    assert falsch.status_code == 400
    ausf.refresh_from_db()
    assert ausf.planfassung != PLAN


def _bericht(kriterien_ids, integrationen):
    return {
        "planfassung": PLAN,
        "kriterien": [
            {"kriterium_id": k, "behandlung": "erfuellt", "nachweis": "test", "commit": COMMIT_A}
            for k in kriterien_ids
        ],
        "paketplan": [
            {"paket": "pa", "titel": "A", "zustand": "erledigt"},
            {"paket": "pb", "titel": "B", "zustand": "erledigt"},
        ],
        "integrationen": integrationen,
        "pruefungen": [
            {"paket": p, "art": "test", "ergebnis": "bestanden", "beschreibung": "grün"}
            for p in ("pa", "pb")
        ],
        "gesamtpruefung": {
            "paket": "root",
            "art": "gesamt",
            "ergebnis": "bestanden",
            "beschreibung": "Kombination geprüft",
        },
        "offene_arbeiten": [],
        "ungeklaerte_wirkungen": [],
    }


def test_abschluss_verlangt_integration_je_repo(client, akteur, inst, projekt, pb_a, pb_b):
    a, ausf, antwort = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    ra, rb = f"repo-{pb_a.pk}", f"repo-{pb_b.pk}"
    pakete = [
        {"paket": "pa", "titel": "A", "zustand": "offen", "repo_id": ra},
        {"paket": "pb", "titel": "B", "zustand": "offen", "repo_id": rb},
    ]
    assert plan(client, inst, ausf, pakete).status_code == 200
    for paket, repo, commit in (("pa", ra, COMMIT_A), ("pb", rb, COMMIT_B)):
        n = ereignis(ausf, inst, "paket_status", {"paket": paket, "zustand": "erledigt",
                                                  "integration": {"repo_id": repo,
                                                                  "commit": commit}})  # fmt: skip
        assert post(client, inst, "ereignisse/", n).status_code == 200
    ids = [k["id"] for k in antwort["snapshot"]["abnahmekriterien"]]
    # Integration für B nennt das falsche Repository: unvollständig, nichts erledigt.
    falsch = _bericht(ids, [{"paket": "pa", "commit": COMMIT_A, "repo_id": ra},
                            {"paket": "pb", "commit": COMMIT_B, "repo_id": ra}])  # fmt: skip
    fehler = post(client, inst, "ereignisse/", ereignis(ausf, inst, "abschluss_bericht", falsch))
    assert fehler.status_code == 409
    assert any("repo" in m.lower() for m in fehler.json()["fehler"]["details"]["maengel"])
    a.refresh_from_db()
    assert a.zustand != Auftrag.ERLEDIGT
    gut = _bericht(ids, [{"paket": "pa", "commit": COMMIT_A, "repo_id": ra},
                         {"paket": "pb", "commit": COMMIT_B, "repo_id": rb}])  # fmt: skip
    ok = post(client, inst, "ereignisse/", ereignis(ausf, inst, "abschluss_bericht", gut))
    assert ok.status_code == 200, ok.content
    a.refresh_from_db()
    assert a.zustand == Auftrag.ERLEDIGT


def test_teilintegration_bleibt_sichtbar_und_blockiert(
    client, owner_client, akteur, inst, projekt, pb_a, pb_b
):
    a, ausf, _ = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    ra, rb = f"repo-{pb_a.pk}", f"repo-{pb_b.pk}"
    plan(client, inst, ausf, [
        {"paket": "pa", "titel": "A", "zustand": "offen", "repo_id": ra},
        {"paket": "pb", "titel": "B", "zustand": "offen", "repo_id": rb},
    ])  # fmt: skip
    n = ereignis(ausf, inst, "paket_status", {"paket": "pa", "zustand": "erledigt",
                                              "integration": {"repo_id": ra,
                                                              "commit": COMMIT_A}})  # fmt: skip
    assert post(client, inst, "ereignisse/", n).status_code == 200
    n = ereignis(ausf, inst, "fehler", {"paket": "pb", "code": "INTEGRATION", "text": "Konflikt"})
    assert post(client, inst, "ereignisse/", n).status_code == 200
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    seite = owner_client.get(reverse("auftrag_detail", args=[a.pk])).content.decode()
    assert COMMIT_A[:12] in seite and pb_a.arbeitsort.name in seite


def test_fred_legt_den_verbund_in_der_oberflaeche_fest(owner_client, akteur, projekt, pb_a, pb_b):
    a = arbeitspaket(akteur, projekt, titel="Oberflaeche", umfang="o/**")
    werkbank.zuweisen(akteur, a, pb_a)
    url = reverse("auftrag_zuweisen", kwargs={"auftrag_id": a.pk})
    seite = owner_client.get(reverse("auftrag_detail", args=[a.pk])).content.decode()
    assert "Zielverbund" in seite and pb_b.arbeitsort.name in seite
    owner_client.post(url, {"wahl": "verbund", "mitglied": [pb_b.pk]})
    a.refresh_from_db()
    assert [m.projektbindung_id for m in a.zuweisung.verbund.all()] == [pb_b.pk]
    seite = owner_client.get(reverse("auftrag_detail", args=[a.pk])).content.decode()
    assert "Werkbank mit Vertrag 2" in seite
    owner_client.post(url, {"wahl": "verbund"})
    assert not a.zuweisung.verbund.exists()


def test_paket_ohne_bestaetigtes_repo_macht_den_abschluss_unvollstaendig(
    client, akteur, inst, projekt, pb_a, pb_b
):
    """Gegenprüfung KIPM-36: Ein Paket, das erst im Abschlussbericht auftaucht (hier ein Teilpaket),
    hat kein Repository aus dem bestätigten Plan; seine Integration darf nicht ungeprüft gelten."""
    a, ausf, antwort = uebernommen(client, akteur, inst, projekt, pb_a, verbund=[pb_b])
    ra, rb = f"repo-{pb_a.pk}", f"repo-{pb_b.pk}"
    plan(client, inst, ausf, [{"paket": "pa", "titel": "A", "zustand": "offen", "repo_id": ra}])
    ids = [k["id"] for k in antwort["snapshot"]["abnahmekriterien"]]
    bericht = _bericht(ids, [{"paket": "pa", "commit": COMMIT_A, "repo_id": ra},
                             {"paket": "pb", "commit": COMMIT_B, "repo_id": rb}])  # fmt: skip
    fehler = post(client, inst, "ereignisse/", ereignis(ausf, inst, "abschluss_bericht", bericht))
    assert fehler.status_code == 409, fehler.content
    assert any("pb" in m for m in fehler.json()["fehler"]["details"]["maengel"])
    a.refresh_from_db()
    assert a.zustand != Auftrag.ERLEDIGT


# --- Antworten an die Werkbank (Rückfragen zu Fassung 2, 26.11.2026) ----------------------------
# Diese Fälle halten vorhandenes Serververhalten fest, auf das sich die Werkbank-Gegenseite stützt;
# sie sind darum von Anfang an grün.


def operation(client, inst, operation_id, vertrag=V2):
    return client.get(
        h.BASIS + f"operationen/{operation_id}/",
        HTTP_AUTHORIZATION=f"Bearer {inst.klartext}",
        HTTP_X_KIPM_VERTRAG=vertrag,
    )


def test_verbundmitglieder_stehen_in_der_ablehnung_und_nichts_wird_gespeichert(
    client, akteur, inst, projekt, pb_a, pb_b
):
    """Frage 1: Die Werkbank erfährt die Mitglieder aus details.erwartet (führendes zuerst); die
    abgelehnte Übernahme ist nicht gespeichert, ein neuer Versuch mit allen Repositories gelingt."""
    a = freigegeben(akteur, projekt, pb_a, verbund=[pb_b])
    erster = uebernahme(inst, pb_a, a, repo_liste=repos((pb_a, COMMIT_A)))
    fehler = post(client, inst, "uebernehmen/", erster)
    assert fehler.status_code == 409
    koerper = fehler.json()
    assert not werkbank_vertrag.schema_fehler("fehler", koerper, vertrag=V2)
    assert koerper["fehler"]["code"] == "ZUSTANDSKONFLIKT"
    assert koerper["fehler"]["details"]["erwartet"] == [f"repo-{pb_a.pk}", f"repo-{pb_b.pk}"]
    assert operation(client, inst, erster["operation_id"]).json()["status"] == "unbekannt"
    zweiter = uebernahme(inst, pb_a, a, repo_liste=repos((pb_a, COMMIT_A), (pb_b, COMMIT_B)))
    assert post(client, inst, "uebernehmen/", zweiter).status_code == 200


def test_nach_freds_klaerung_gelingt_dieselbe_uebernahme_ohne_neue_zuweisungsfassung(
    client, akteur, inst, projekt, pb_a
):
    """Frage 4: WIEDERHERSTELLUNG_UNGEKLAERT erhöht keine Fassung und wird nicht gespeichert;
    nach der Klärung wirkt dieselbe Nachricht (gleiche operation_id)."""
    a = freigegeben(akteur, projekt, pb_a)
    fremd = str(uuid.uuid4())
    client.get(
        h.BASIS + "abgleich/",
        {"ausfuehrungen": fremd},
        HTTP_AUTHORIZATION=f"Bearer {inst.klartext}",
        HTTP_X_KIPM_VERTRAG=V2,
    )
    version = a.zuweisung.version
    n = uebernahme(inst, pb_a, a)
    fehler = post(client, inst, "uebernehmen/", n)
    assert fehler.json()["fehler"]["code"] == "WIEDERHERSTELLUNG_UNGEKLAERT"
    assert operation(client, inst, n["operation_id"]).json()["status"] == "unbekannt"
    werkbank.konflikte_klaeren(akteur, inst)
    a.zuweisung.refresh_from_db()
    assert a.zuweisung.version == version
    assert post(client, inst, "uebernehmen/", n).status_code == 200


def test_widerruf_der_erlaubnis_erhoeht_die_zuweisungsfassung(akteur, delegiert):
    """Frage 4: Nach ERLAUBNIS_UNGUELTIG kommt ein neues Angebot nur mit neuer Zuweisungsfassung
    (Widerruf setzt den Auftrag in den Entwurf zurück)."""
    d, a = delegiert()
    version = a.zuweisung.version
    werkbank.delegation_widerrufen(akteur, d, erwartet_revision=d.revision)
    a.refresh_from_db()
    a.zuweisung.refresh_from_db()
    assert a.zustand == Auftrag.ENTWURF
    assert a.zuweisung.version > version


def test_operationsabfrage_antwortet_in_beiden_fassungen(client, akteur, inst, projekt, pb_a):
    """Frage 5: GET operationen/<id>/ liest in jeder gesprochenen Fassung; die gespeicherte
    Antwort behält ihre eigene Fassung."""
    a = freigegeben(akteur, projekt, pb_a)
    n = uebernahme(inst, pb_a, a)
    assert post(client, inst, "uebernehmen/", n).status_code == 200
    for kopf in (V1, V2):
        antwort = operation(client, inst, n["operation_id"], vertrag=kopf)
        assert antwort.status_code == 200
        daten = antwort.json()
        assert daten["vertrag"] == kopf and daten["status"] == "gespeichert"
        assert daten["ausgang"]["antwort"]["vertrag"] == V2

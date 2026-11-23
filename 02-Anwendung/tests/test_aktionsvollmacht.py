"""Aktionsvollmacht: Fred erteilt in der Oberfläche eine gespeicherte, serverseitig geprüfte
Vollmacht für einen klar definierten technischen Standardfall (zuerst: Wiederaufnahme beim bisherigen
Bearbeiter nach belegter Fehlerkorrektur). Ein Zugang mit Vollmacht führt den Fall danach ohne
erneute Rückfrage aus; jede Ausführung ist an Revision, maßgebliche Blockademeldung, Bearbeiter und
einen konkreten Liefernachweis gebunden. Referenzfall: E-213 / NEX-63 r3 mit NEX-69 r5 L-1,
synthetisch nachgebildet."""

import json
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, auftragsaktionen, dienst, lieferungen
from steuerung.models import Aktionsvollmacht, Aktionsvorschlag, Auftrag, Ereignis, Meldung
from tests.conftest import arbeitspaket
from tests.test_api import api
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db
COMMIT = "c" * 40


@pytest.fixture
def pc(akteur):
    """Freds PC-Zugang „Claude Desktop“: Bearbeiter der Nexus-Aufträge und ausführender Zugang."""
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    b.klartext = schluessel
    return b


@pytest.fixture
def claude(pc, projekt):
    return dienst.Akteur.aus_bindung(pc, projekt=projekt)


@pytest.fixture
def nex69(akteur, projekt, claude):
    """Lieferauftrag mit belegter Korrektur (wie NEX-69 r5 L-1)."""
    a = arbeitspaket(akteur, projekt, titel="Core-Korrektur", umfang="core/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(claude, a, a.revision_nr)
    a.refresh_from_db()
    a.lieferung = lieferungen.melden(
        claude,
        a,
        a.revision_nr,
        lieferkennung="c02b-aufloeser",
        zusammenfassung="C13/C14/C17 korrigiert",
        dateien=[f"src/nexus_core/registry.py@{COMMIT}"],
        pruefungen=["pytest tests/core: grün"],
    )
    return a


@pytest.fixture
def nex63(akteur, projekt, claude):
    """Wie NEX-63 r3: Ergebnis mit Befund → Blockiert mit FEHLER beim bisherigen Bearbeiter."""
    a = arbeitspaket(akteur, projekt, titel="Finance-Anschlussprüfung", umfang="docs/bericht.md")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(claude, a, a.revision_nr)
    dienst.ergebnis_einreichen(
        claude,
        a,
        a.revision_nr,
        zusammenfassung="Nachtrag geschrieben, drei Core-Abweichungen.",
        dateien=f"docs/bericht.md@{COMMIT}",
        pruefungen="verify.py: PASS",
        fehlgeschlagen="finance_faelle_u1.py: C13, C14, C17 abweichend",
    )
    dienst.sitzungsende_melden(claude, a)
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    return a


@pytest.fixture
def e213(akteur, projekt):
    return akte.entscheidung_eintragen(
        akteur,
        projekt,
        entscheidung="NEX-63 r3 darf beim bisherigen Bearbeiter wieder aufgenommen werden.",
    )


@pytest.fixture
def vollmacht(akteur, projekt, pc, e213):
    return auftragsaktionen.vollmacht_erteilen(
        akteur,
        projekt,
        aktion=Aktionsvorschlag.WIEDERAUFNAHME,
        ausfuehrende_bindung=pc,
        bearbeiter_bindung=pc,
        meldungsarten=[Meldung.FEHLER],
        gueltig_bis=timezone.now() + timedelta(days=7),
        entscheidung=e213,
    )


def _nachweis(nex69, **extra):
    return {
        "auftrag": nex69.nummer,
        "revision": nex69.revision_nr,
        "lieferung": f"L-{nex69.lieferung.pk}",
    } | extra


def _ausfuehren(claude, nex63, vm, nex69, **extra):
    daten = dict(
        vollmacht=vm.nummer,
        aktion=Aktionsvorschlag.WIEDERAUFNAHME,
        revision=nex63.revision_nr,
        meldung_id=lieferungen._blocker(nex63).pk,
        nachweis=_nachweis(nex69),
        text="C13, C14, C17 gegen den Korrekturstand wiederholen.",
    )
    return auftragsaktionen.mit_vollmacht_ausfuehren(claude, nex63, **(daten | extra))


def _antworten(a):
    return a.meldungen.filter(art=Meldung.ANTWORT).count()


# --- Referenzfall ---------------------------------------------------------------------------------


def test_referenzfall_e213_nex63_wiederaufnahme(nex63, nex69, vollmacht, claude, pc):
    fehler = lieferungen._blocker(nex63)
    v, wiederholt = _ausfuehren(claude, nex63, vollmacht, nex69)
    nex63.refresh_from_db()
    assert not wiederholt
    assert nex63.zustand == Auftrag.IN_ARBEIT
    assert nex63.bearbeiter_bindung_id == pc.pk and nex63.revision_nr == v.erwartete_revision
    assert v.zustand == Aktionsvorschlag.AUSGEFUEHRT and v.vollmacht_id == vollmacht.pk
    assert v.meldung_id == fehler.pk and v.ausgefuehrt_am is not None
    assert v.nachweis == {
        "auftrag": nex69.nummer,
        "revision": nex69.revision_nr,
        "lieferung": nex69.lieferung.pk,
    }
    antwort = nex63.meldungen.filter(art=Meldung.ANTWORT).get()
    assert v.nummer in antwort.text and vollmacht.nummer in antwort.text
    assert f"L-{nex69.lieferung.pk}" in antwort.text
    folge = list(
        Ereignis.objects.filter(objekt_kennung=nex63.nummer)
        .order_by("id")
        .values_list("ereignis", flat=True)
    )
    i = folge.index("Antwort gegeben, Blockade aufgehoben")
    assert folge[i + 1] == f"Aktion ausgeführt ({v.nummer}) mit Vollmacht {vollmacht.nummer}"


def test_zweiter_identischer_aufruf_wirkt_nicht_noch_einmal(nex63, nex69, vollmacht, claude):
    fehler = lieferungen._blocker(nex63).pk
    v1, _ = _ausfuehren(claude, nex63, vollmacht, nex69, meldung_id=fehler)
    v2, wiederholt = _ausfuehren(claude, nex63, vollmacht, nex69, meldung_id=fehler)
    assert wiederholt and v2.pk == v1.pk
    assert _antworten(nex63) == 1
    assert Aktionsvorschlag.objects.filter(vollmacht=vollmacht).count() == 1


def test_parallele_aufrufe_wirken_hoechstens_einmal(transactional_db, akteur, projekt):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    pc, _ = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    claude = dienst.Akteur.aus_bindung(pc, projekt=projekt)
    lief = arbeitspaket(akteur, projekt, titel="Core-Korrektur", umfang="core/**")
    dienst.auftrag_freigeben(akteur, lief)
    dienst.auftrag_uebernehmen(claude, lief, lief.revision_nr)
    lief.refresh_from_db()
    lief.lieferung = lieferungen.melden(
        claude,
        lief,
        lief.revision_nr,
        lieferkennung="fix",
        zusammenfassung="Korrektur",
        dateien=[f"a.py@{COMMIT}"],
        pruefungen=["grün"],
    )
    a = arbeitspaket(akteur, projekt, titel="Prüfung", umfang="docs/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(claude, a, a.revision_nr)
    dienst.fehler_melden(claude, a, a.revision_nr, "Befund C13")
    a.refresh_from_db()
    vm = auftragsaktionen.vollmacht_erteilen(
        akteur,
        projekt,
        aktion=Aktionsvorschlag.WIEDERAUFNAHME,
        ausfuehrende_bindung=pc,
        bearbeiter_bindung=pc,
        meldungsarten=[Meldung.FEHLER],
        gueltig_bis=timezone.now() + timedelta(days=1),
    )
    fehler = lieferungen._blocker(a).pk  # beide Aufrufe sind identisch
    start = Barrier(2)

    def lauf(_):
        close_old_connections()
        try:
            start.wait(timeout=5)
            v, wiederholt = _ausfuehren(claude, a, vm, lief, meldung_id=fehler)
            return (v.pk, wiederholt)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ergebnisse = sorted(pool.map(lauf, range(2)), key=lambda e: e[1])
    assert ergebnisse[0][0] == ergebnisse[1][0]
    assert [e[1] for e in ergebnisse] == [False, True]
    assert _antworten(a) == 1


# --- Abweisungen mit sichtbarem Wartegrund -----------------------------------------------------------


def _abgewiesen(claude, nex63, vollmacht, nex69, muster, fehler=dienst.Konflikt, **extra):
    with pytest.raises(fehler, match=muster):
        _ausfuehren(claude, nex63, vollmacht, nex69, **extra)
    nex63.refresh_from_db()
    assert nex63.zustand == Auftrag.BLOCKIERT and _antworten(nex63) == 0
    assert not Aktionsvorschlag.objects.filter(auftrag=nex63).exists()


def test_ohne_vollmacht_keine_ausfuehrung(nex63, nex69, claude, vollmacht, akteur):
    with pytest.raises(dienst.KeinZugriff):
        _ausfuehren(claude, nex63, vollmacht, nex69, vollmacht="VM-999")
    auftragsaktionen.vollmacht_widerrufen(akteur, vollmacht)
    _abgewiesen(claude, nex63, vollmacht, nex69, "widerrufen")


def test_abgelaufene_vollmacht(nex63, nex69, claude, vollmacht):
    Aktionsvollmacht.objects.filter(pk=vollmacht.pk).update(
        gueltig_bis=timezone.now() - timedelta(minutes=1)
    )
    _abgewiesen(claude, nex63, vollmacht, nex69, "abgelaufen")


def test_nur_der_benannte_zugang_fuehrt_aus(nex63, nex69, vollmacht, bindung, akteur, projekt):
    anderer, _ = dienst.bindung_anlegen(akteur, None, name="Anderer PC-Chat")
    for wer in (
        dienst.Akteur.aus_bindung(bindung),
        dienst.Akteur.aus_bindung(anderer, projekt=projekt),
    ):
        with pytest.raises(dienst.KeinZugriff):
            _ausfuehren(wer, nex63, vollmacht, nex69)
    nex63.refresh_from_db()
    assert nex63.zustand == Auftrag.BLOCKIERT


def test_vollmacht_gilt_nur_fuer_ihre_aktion(nex63, nex69, claude, vollmacht):
    _abgewiesen(claude, nex63, vollmacht, nex69, "Aktion", aktion=Aktionsvorschlag.NEUSCHNITT)


def test_veraltete_revision_hinterlaesst_wartegrund(nex63, nex69, vollmacht, claude):
    _abgewiesen(claude, nex63, vollmacht, nex69, "Revision", revision=nex63.revision_nr - 1)
    grund = auftragsaktionen.wartegrund(nex63)
    assert grund and "Revision" in grund
    assert (
        Ereignis.objects.filter(
            objekt_kennung=nex63.nummer,
            ereignis=f"Aktion mit Vollmacht nicht ausgeführt ({vollmacht.nummer})",
        ).count()
        == 1
    )
    # Dieselbe Abweisung noch einmal: kein zweites, gleiches Ereignis.
    _abgewiesen(claude, nex63, vollmacht, nex69, "Revision", revision=nex63.revision_nr - 1)
    assert (
        Ereignis.objects.filter(
            objekt_kennung=nex63.nummer,
            ereignis=f"Aktion mit Vollmacht nicht ausgeführt ({vollmacht.nummer})",
        ).count()
        == 1
    )


def test_neue_blockademeldung_bindet_neu(nex63, nex69, vollmacht, claude):
    alt = lieferungen._blocker(nex63).pk
    dienst.rueckfrage_melden(claude, nex63, nex63.revision_nr, "Darf ich tools/ ändern?")
    _abgewiesen(claude, nex63, vollmacht, nex69, "maßgebliche", meldung_id=alt)
    # Auch mit der neuen Meldung nicht: Die Vollmacht deckt nur FEHLER, keine Rückfrage.
    neu = lieferungen._blocker(nex63).pk
    _abgewiesen(claude, nex63, vollmacht, nex69, "Rückfrage", meldung_id=neu)


@pytest.mark.parametrize(
    ("nachweis", "muster", "fehler"),
    [
        (None, "Nachweis", dienst.DienstFehler),
        ({"auftrag": "SA-1", "revision": 1}, "Nachweis", dienst.DienstFehler),
        ("falsche_lieferung", "Lieferung", dienst.Konflikt),
        ("falsche_revision", "Revision", dienst.Konflikt),
    ],
)
def test_fehlender_oder_unpassender_nachweis(
    nex63, nex69, vollmacht, claude, nachweis, muster, fehler
):
    if nachweis == "falsche_lieferung":
        nachweis = _nachweis(nex69, lieferung="L-999")
    elif nachweis == "falsche_revision":
        nachweis = _nachweis(nex69, revision=nex69.revision_nr + 1)
    _abgewiesen(claude, nex63, vollmacht, nex69, muster, fehler=fehler, nachweis=nachweis)


def test_falscher_bearbeiter(nex69, vollmacht, claude, akteur, projekt, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    a = arbeitspaket(akteur, projekt, titel="Fremd bearbeitet", umfang="fremd/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.fehler_melden(chat, a, a.revision_nr, "Befund")
    a.refresh_from_db()
    with pytest.raises(dienst.Konflikt, match="Bearbeiter"):
        _ausfuehren(claude, a, vollmacht, nex69)


def test_ordnerkonflikt(akteur, projekt, claude, nex69, vollmacht):
    ort = dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="finance", name="Finance", pfad=r"C:\synth\Nexus-Finanzcockpit"
    )
    auftraege = []
    for titel, umfang in (("Prüfung", "docs/bericht.md"), ("Andere Arbeit", "src/**")):
        a = arbeitspaket(akteur, projekt, titel=titel, umfang=umfang)
        dienst.arbeitsort_setzen(akteur, a, ort)
        a.refresh_from_db()
        auftraege.append(a)
    pruefung, andere = auftraege
    dienst.auftrag_freigeben(akteur, pruefung)
    dienst.auftrag_uebernehmen(claude, pruefung, pruefung.revision_nr)
    dienst.fehler_melden(
        claude, pruefung, pruefung.revision_nr, "Befund C13"
    )  # gibt den Ordner frei
    dienst.auftrag_freigeben(akteur, andere)
    dienst.auftrag_uebernehmen(claude, andere, andere.revision_nr)  # belegt denselben Ordner
    pruefung.refresh_from_db()
    with pytest.raises(dienst.Konflikt, match="gesperrt"):
        _ausfuehren(claude, pruefung, vollmacht, nex69)
    pruefung.refresh_from_db()
    assert pruefung.zustand == Auftrag.BLOCKIERT
    assert "gesperrt" in auftragsaktionen.wartegrund(pruefung)


def test_vollmacht_hebt_keinen_lieferblocker_auf(akteur, projekt, claude, nex69, vollmacht):
    """Reine Lieferblockaden laufen weiter über die Lieferautomatik mit ihren exakten Zuordnungen."""
    a = arbeitspaket(akteur, projekt, titel="Verbraucher", umfang="verbraucher/**")
    dienst.auftrag_freigeben(akteur, a)
    dienst.auftrag_uebernehmen(claude, a, a.revision_nr)
    lieferungen.warten(
        claude,
        a,
        a.revision_nr,
        text="Anschluss fehlt.",
        bedarf=[{"auftrag": nex69.nummer, "revision": nex69.revision_nr, "lieferkennung": "neu"}],
        nur_lieferungen=True,
    )
    a.refresh_from_db()
    with pytest.raises(dienst.Konflikt, match="Lieferautomatik"):
        _ausfuehren(claude, a, vollmacht, nex69)


# --- Vollmacht erteilen: nur Fred, gespeichert, nicht aus Text ---------------------------------------


def test_nur_fred_erteilt_vollmachten(projekt, pc, claude, zweiter_mensch):
    for wer in (claude, dienst.Akteur.aus_benutzer(zweiter_mensch)):
        with pytest.raises(dienst.KeinZugriff):
            auftragsaktionen.vollmacht_erteilen(
                wer,
                projekt,
                aktion=Aktionsvorschlag.WIEDERAUFNAHME,
                ausfuehrende_bindung=pc,
                bearbeiter_bindung=pc,
                meldungsarten=[Meldung.FEHLER],
                gueltig_bis=timezone.now() + timedelta(days=1),
            )


def test_ausfuehrender_zugang_muss_planen_duerfen(akteur, projekt, pc, bindung):
    with pytest.raises(dienst.DienstFehler, match="PC-Zugang"):
        auftragsaktionen.vollmacht_erteilen(
            akteur,
            projekt,
            aktion=Aktionsvorschlag.WIEDERAUFNAHME,
            ausfuehrende_bindung=bindung,
            bearbeiter_bindung=pc,
            meldungsarten=[Meldung.FEHLER],
            gueltig_bis=timezone.now() + timedelta(days=1),
        )


def test_oberflaeche_erteilen_und_widerrufen(owner_client, projekt, pc):
    url = reverse("vollmacht_erteilen", kwargs={"kennung": projekt.kennung})
    r = owner_client.post(
        url,
        {
            "aktion": "wiederaufnahme",
            "ausfuehrende_bindung": pc.pk,
            "bearbeiter_bindung": pc.pk,
            "meldungsarten": ["FEHLER"],
            "gueltig_bis": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "entscheidung": "",
            "auftrag": "",
        },
        follow=True,
    )
    vm = Aktionsvollmacht.objects.get()
    html = r.content.decode()
    assert vm.nummer in html and "Wiederaufnahme" in html and "Claude Desktop" in html
    assert vm.erteilt_von.is_superuser
    owner_client.post(
        reverse("vollmacht_widerrufen", kwargs={"kennung": projekt.kennung, "nummer": vm.pk})
    )
    vm.refresh_from_db()
    assert vm.widerrufen_am is not None


# --- Lese- und Aktionsweg für die Vorratsautomation ----------------------------------------------------


def test_http_und_mcp_aktionsweg(client, nex63, nex69, vollmacht, pc, projekt):
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_aktion_ausfuehren",
        projekt=projekt.kennung,
        auftrag=nex63.nummer,
        vollmacht=vollmacht.nummer,
        aktion="wiederaufnahme",
        revision=nex63.revision_nr,
        meldung_id=lieferungen._blocker(nex63).pk,
        nachweis=_nachweis(nex69),
        text="C13, C14, C17 nachlaufen lassen.",
        anfrage_kennung=uuid.uuid4().hex,
        fred_sagte=None,
    )
    assert not fehler, daten
    assert daten["aktion"]["zustand"] == "AUSGEFUEHRT" and daten["wiederholt"] is False
    nex63.refresh_from_db()
    assert nex63.zustand == Auftrag.IN_ARBEIT


def test_http_ohne_vollmacht_409_mit_grund(nex63, nex69, vollmacht, pc, projekt):
    r = api(pc.klartext).post(
        reverse("api_aktion_vollmacht", kwargs={"auftrag_id": nex63.pk}),
        json.dumps(
            {
                "vollmacht": vollmacht.nummer,
                "aktion": "wiederaufnahme",
                "revision": nex63.revision_nr - 1,
                "meldung_id": lieferungen._blocker(nex63).pk,
                "nachweis": _nachweis(nex69),
                "text": "x",
                "anfrage_kennung": uuid.uuid4().hex,
            }
        ),
        content_type="application/json",
        HTTP_X_KIPM_PROJEKT=projekt.kennung,
    )
    assert r.status_code == 409 and "Revision" in r.json()["fehler"]


def test_lesen_zeigt_vorbereitet_ausgefuehrt_und_fortschritt_danach(
    client, nex63, nex69, vollmacht, pc, claude, projekt, akteur
):
    fehler, daten = _werkzeug(client, pc.klartext, "kipm_aktionen", projekt=projekt.kennung)
    assert not fehler, daten
    assert [v["nummer"] for v in daten["vollmachten"]] == [vollmacht.nummer]
    assert daten["vollmachten"][0]["aktion"] == "wiederaufnahme"
    v, _ = _ausfuehren(claude, nex63, vollmacht, nex69)
    _, daten = _werkzeug(client, pc.klartext, "kipm_aktionen", projekt=projekt.kennung)
    eintrag = next(x for x in daten["aktionen"] if x["nummer"] == v.nummer)
    assert eintrag["zustand"] == "AUSGEFUEHRT" and eintrag["vollmacht"] == vollmacht.nummer
    assert eintrag["auftrag"] == nex63.nummer and eintrag["bearbeiter_danach"] is None
    dienst.fortschritt_melden(claude, nex63, nex63.revision_nr, "C13 behoben, C14 offen.")
    _, daten = _werkzeug(client, pc.klartext, "kipm_aktionen", projekt=projekt.kennung)
    eintrag = next(x for x in daten["aktionen"] if x["nummer"] == v.nummer)
    assert eintrag["bearbeiter_danach"]["art"] == "FORTSCHRITT"
    assert "C13 behoben" in eintrag["bearbeiter_danach"]["text"]
    # Ein vorbereiteter, noch nicht bestätigter Vorschlag ist davon unterscheidbar.
    planung, _ = dienst.bindung_anlegen(akteur, None, name="Planung")
    dienst.fehler_melden(claude, nex63, nex63.revision_nr, "Neuer Befund")
    nex63.refresh_from_db()
    offen = auftragsaktionen.vorschlagen(
        dienst.Akteur.aus_bindung(planung, projekt=projekt),
        nex63,
        art=Aktionsvorschlag.WIEDERAUFNAHME,
        revision=nex63.revision_nr,
        meldung_id=lieferungen._blocker(nex63).pk,
        text="weiter",
    )
    _, daten = _werkzeug(client, pc.klartext, "kipm_aktionen", projekt=projekt.kennung)
    eintrag = next(x for x in daten["aktionen"] if x["nummer"] == offen.nummer)
    assert eintrag["zustand"] == "OFFEN" and eintrag["vollmacht"] is None


# --- Bestehender Freigabepfad für ein vorbereitetes Issue (wie NEX-199 nach E-212) ------------------------


def test_freigabepfad_vorbereitetes_issue(owner_client, akteur, projekt, claude):
    ort = dienst.arbeitsort_anlegen(
        akteur, projekt, kennung="core", name="Core", pfad=r"C:\synth\Nexus-Core"
    )
    issue = arbeitspaket(akteur, projekt, titel="IPC-Zeilenlimit", umfang="src/nexus_core/ipc.py")
    dienst.arbeitsort_setzen(akteur, issue, ort)
    issue.refresh_from_db()
    assert dienst.freigabefaehig(issue)
    # Bedienbarkeit: Freds Freigabe ist ein Knopf in „Wartet auf dich“.
    html = owner_client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung})).content
    assert reverse("auftrag_aktion", kwargs={"auftrag_id": issue.pk, "aktion": "freigeben"}) in (
        html.decode()
    )
    # Schreibumfang: ein laufender Auftrag mit überschneidendem Umfang verhindert die Freigabe.
    anderer = arbeitspaket(akteur, projekt, titel="Core-Umbau", umfang="src/nexus_core/**")
    dienst.auftrag_freigeben(akteur, anderer)
    with pytest.raises(dienst.Konflikt, match="Überschneidung"):
        dienst.auftrag_freigeben(akteur, issue)
    dienst.auftrag_abbrechen(akteur, anderer, "Testfall")
    owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": issue.pk, "aktion": "freigeben"})
    )
    issue.refresh_from_db()
    assert issue.zustand == Auftrag.FREIGEGEBEN
    # Rechte: ein Projektschlüssel gibt nie frei; Ordnersperre: belegter Ordner verhindert die Übernahme.
    belegt = arbeitspaket(akteur, projekt, titel="Belegt Core", umfang="tests/**")
    dienst.arbeitsort_setzen(akteur, belegt, ort)
    belegt.refresh_from_db()
    dienst.auftrag_freigeben(akteur, belegt)
    dienst.auftrag_uebernehmen(claude, belegt, belegt.revision_nr)
    with pytest.raises(dienst.Konflikt, match="gesperrt"):
        dienst.auftrag_uebernehmen(claude, issue, issue.revision_nr)


# --- Befunde der Gegenprüfung -------------------------------------------------------------------------


def test_nachweis_begruendet_je_auftrag_nur_eine_wiederaufnahme(nex63, nex69, vollmacht, claude):
    _ausfuehren(claude, nex63, vollmacht, nex69)
    nex63.refresh_from_db()
    dienst.fehler_melden(claude, nex63, nex63.revision_nr, "C14 weiter abweichend")
    nex63.refresh_from_db()
    with pytest.raises(dienst.Konflikt, match="schon"):
        _ausfuehren(claude, nex63, vollmacht, nex69)
    nex63.refresh_from_db()
    assert nex63.zustand == Auftrag.BLOCKIERT and _antworten(nex63) == 1
    neue = lieferungen.melden(
        claude,
        nex69,
        nex69.revision_nr,
        lieferkennung="c02b-aufloeser-2",
        zusammenfassung="C14 korrigiert",
        dateien=[f"src/nexus_core/registry.py@{'e' * 40}"],
        pruefungen=["pytest tests/core: grün"],
    )
    _ausfuehren(
        claude, nex63, vollmacht, nex69, nachweis=_nachweis(nex69, lieferung=f"L-{neue.pk}")
    )
    nex63.refresh_from_db()
    assert nex63.zustand == Auftrag.IN_ARBEIT


def test_vollmacht_auf_genau_eine_lieferung(akteur, projekt, pc, claude, nex63, nex69, e213):
    andere = lieferungen.melden(
        claude,
        nex69,
        nex69.revision_nr,
        lieferkennung="anderes",
        zusammenfassung="Anderer Stand",
        dateien=[f"src/x.py@{'f' * 40}"],
        pruefungen=["grün"],
    )
    vm = auftragsaktionen.vollmacht_erteilen(
        akteur,
        projekt,
        aktion=Aktionsvorschlag.WIEDERAUFNAHME,
        ausfuehrende_bindung=pc,
        bearbeiter_bindung=pc,
        meldungsarten=[Meldung.FEHLER],
        gueltig_bis=timezone.now() + timedelta(days=7),
        auftrag=nex63,
        entscheidung=e213,
        lieferung=nex69.lieferung,
    )
    _abgewiesen(
        claude, nex63, vm, nex69, "nur", nachweis=_nachweis(nex69, lieferung=f"L-{andere.pk}")
    )
    v, _ = _ausfuehren(claude, nex63, vm, nex69)
    assert v.nachweis["lieferung"] == nex69.lieferung.pk


def test_widerruf_kurz_vor_der_ausfuehrung_wirkt(nex63, nex69, vollmacht, claude, monkeypatch):
    """Widerruft Fred, während ein Aufruf die Vollmacht schon gelesen hat und auf die Sperre wartet,
    gilt der Widerruf: Die Vollmacht wird in der Transaktion neu gelesen."""
    original = auftragsaktionen._schon_ausgefuehrt

    def dazwischen(*args, **kwargs):
        Aktionsvollmacht.objects.filter(pk=vollmacht.pk).update(widerrufen_am=timezone.now())
        return original(*args, **kwargs)

    monkeypatch.setattr(auftragsaktionen, "_schon_ausgefuehrt", dazwischen)
    _abgewiesen(claude, nex63, vollmacht, nex69, "widerrufen")


def test_wartegrund_gilt_nur_fuer_die_aktuelle_blockade(nex63, nex69, vollmacht, claude, akteur):
    _abgewiesen(claude, nex63, vollmacht, nex69, "Revision", revision=nex63.revision_nr - 1)
    assert auftragsaktionen.wartegrund(nex63)
    dienst.rueckfrage_beantworten(akteur, nex63, "Fred setzt selbst fort.")
    nex63.refresh_from_db()
    assert auftragsaktionen.wartegrund(nex63) is None
    dienst.fehler_melden(claude, nex63, nex63.revision_nr, "Neuer Befund")
    nex63.refresh_from_db()
    assert auftragsaktionen.wartegrund(nex63) is None  # gehört zur früheren Blockade

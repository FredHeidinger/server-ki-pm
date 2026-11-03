"""Regressionstests zu den Befunden der unabhängigen Gegenprüfung (Verbesserungsliste und A-2)."""

import json
import os
import subprocess
import sys

import pytest
from django.urls import reverse

from steuerung import akte, dienst
from steuerung.models import Arbeitsort, Installation, Modul, Nachweis
from tests.test_mcp import _werkzeug
from tests.test_meldevertrag import MELDER, _bericht


@pytest.fixture
def pc(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Assistent")
    b.klartext = schluessel
    return b


@pytest.fixture
def delta(akteur):
    p = dienst.projekt_registrieren(
        akteur, kennung="delta", name="Delta", module=[("oberflaeche", "Oberfläche")]
    )
    akte.umgebung_anlegen(akteur, p, name="Lokal", art="lokal")
    akte.umgebung_anlegen(akteur, p, name="Produktion", art="produktion")
    return p


def _ki(pc, projekt):
    return dienst.Akteur.aus_bindung(pc, projekt=projekt)


# Befund 1: Verwerfen bestätigt nicht; Reaktivierung durch die KI macht wieder einen Vorschlag.
def test_verworfener_vorschlag_wird_durch_ki_nicht_wirksam(client, owner_client, projekt, pc):
    ort = dienst.arbeitsort_anlegen(
        _ki(pc, projekt), projekt, kennung="x", name="Fremd", pfad=r"W:\Fremd"
    )
    owner_client.post(
        reverse(
            "arbeitsort_pflegen", kwargs={"kennung": "synth-a", "pk": ort.pk, "wahl": "archivieren"}
        )
    )
    ort.refresh_from_db()
    assert not ort.aktiv and not ort.bestaetigt
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_arbeitsort_aendern",
        projekt="synth-a",
        arbeitsort="x",
        revision=ort.revision,
        aktiv=True,
    )
    assert not fehler, daten
    ort.refresh_from_db()
    assert ort.aktiv and not ort.bestaetigt
    assert dienst.projekt_fuer_ordner(r"W:\Fremd") == (None, None)


def test_bestaetigter_ort_nach_reaktivierung_durch_ki_wieder_vorschlag(akteur, projekt, pc):
    ort = projekt.arbeitsorte.get()
    dienst.arbeitsort_aendern(akteur, ort, erwartete_revision=None, aktiv=False)
    ort.refresh_from_db()
    dienst.arbeitsort_aendern(_ki(pc, projekt), ort, erwartete_revision=ort.revision, aktiv=True)
    ort.refresh_from_db()
    assert not ort.bestaetigt


def test_verworfene_umgebung_wird_durch_ki_nicht_nutzbar(akteur, delta, pc):
    ki = _ki(pc, delta)
    u = akte.umgebung_anlegen(ki, delta, name="Schatten", art="test")
    akte.umgebung_aendern(akteur, u, erwartete_revision=None, aktiv=False)
    u.refresh_from_db()
    assert not u.bestaetigt
    akte.umgebung_aendern(ki, u, erwartete_revision=u.revision, aktiv=True)
    u.refresh_from_db()
    with pytest.raises(dienst.DienstFehler, match="Entwurf"):
        akte.installation_melden(ki, delta, umgebung=u, komponente="a", version="1", herkunft="x")


# Befund 2: Übernehmen bindet die angezeigte Revision bzw. den angezeigten Entwurf.
def test_uebernehmen_bindet_angezeigte_revision(client, owner_client, projekt, pc):
    ki = _ki(pc, projekt)
    a = dienst.wunsch_erfassen(ki, projekt, titel="T", beschreibung="b")
    ort = dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="V", pfad=r"W:\A")
    dienst.arbeitsort_setzen(ki, a, ort)
    gesehen = ort.revision
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})).content
    assert f'name="revision" value="{gesehen}"' in seite.decode()
    dienst.arbeitsort_aendern(ki, ort, erwartete_revision=gesehen, pfad=r"W:\B")
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": a.pk, "wahl": "bestaetigen"}),
        {"arbeitsort": ort.pk, "revision": gesehen},
    )
    ort.refresh_from_db()
    assert not ort.bestaetigt and ort.pfad == r"W:\B"


def test_setzen_eines_vorschlags_bestaetigt_nicht_still(owner_client, projekt, pc):
    ki = _ki(pc, projekt)
    a = dienst.wunsch_erfassen(ki, projekt, titel="T", beschreibung="b")
    ort = dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="V", pfad=r"W:\A")
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": a.pk, "wahl": "setzen"}),
        {"arbeitsort": ort.pk},
    )
    ort.refresh_from_db()
    assert not ort.bestaetigt


def test_umgebung_und_profil_uebernehmen_binden_den_entwurf(akteur, delta, pc):
    ki = _ki(pc, delta)
    u = akte.umgebung_anlegen(ki, delta, name="Stage", art="test", url="https://a.invalid")
    gesehen = u.revision
    akte.umgebung_aendern(ki, u, erwartete_revision=gesehen, url="https://b.invalid")
    with pytest.raises(dienst.Konflikt):
        akte.umgebung_bestaetigen(akteur, u, erwartete_revision=gesehen)
    akte.profil_vorschlagen(ki, delta, erwartete_revision=0, zweck="Erster Entwurf")
    delta.refresh_from_db()
    gesehen_hash = akte.entwurf_hash(delta)
    akte.profil_vorschlagen(ki, delta, erwartete_revision=0, zweck="Untergeschoben")
    with pytest.raises(dienst.Konflikt):
        akte.profil_entwurf_uebernehmen(
            akteur, delta, uebernehmen=True, erwarteter_entwurf=gesehen_hash
        )
    delta.refresh_from_db()
    assert delta.zweck == ""


# Befund 3: Der Melder stempelt beobachtet_am; Nachlieferung verdrängt keinen neueren Stand.
def test_melder_stempelt_beobachtungszeit_vor_dem_einreihen(tmp_path, akteur):
    bericht = tmp_path / "b.json"
    bericht.write_text(json.dumps({"meldung_id": "q-1", "version": "v1", "quelle": "CI"}))
    lauf = subprocess.run(
        [
            sys.executable,
            str(MELDER),
            "senden",
            str(bericht),
            "--warteschlange",
            str(tmp_path / "s"),
            "--versuche",
            "1",
            "--zeit",
            "2",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "KIPM_URL": "http://127.0.0.1:9", "KIPM_SCHLUESSEL": "kipm_x_y"},
        timeout=60,
    )
    assert lauf.returncode == 3
    gespeichert = json.loads((tmp_path / "s" / "q-1.json").read_text(encoding="utf-8"))
    # Bericht unverändert (gleicher Inhalt bei Wiederholung), Zeit des ersten Versuchs daneben.
    assert "beobachtet_am" not in gespeichert["bericht"] and gespeichert["erster_versuch"]


# Befund 4: Funktionsnachweise ohne Meldevertrag erscheinen nicht als Projektstand.
def test_funktionsnachweise_kein_falscher_projektstand(akteur, delta):
    f1 = akte.funktion_anlegen(akteur, delta, titel="Eins")
    f2 = akte.funktion_anlegen(akteur, delta, titel="Zwei")
    for f, ergebnis in ((f1, "bestanden"), (f2, "fehlgeschlagen")):
        akte.nachweis_melden(
            akteur,
            delta,
            art="test",
            ergebnis=ergebnis,
            version="abc",
            quelle="q",
            umfang="u",
            funktion=f,
        )
    gesamt = akte.modulstaende(delta)[0]
    assert gesamt["tests"] == [] and not any("widersprüchlich" in x for x in gesamt["luecken"])


# Befund 5: Tests mit verschiedenem Umfang widersprechen sich nicht; alle bleiben sichtbar.
def test_tests_je_umfang_getrennt(akteur, delta):
    akte.stand_melden(
        akteur,
        delta,
        meldung_id="m-1",
        version="v1",
        quelle="CI",
        modul=delta.module.get(),
        umsetzung={},
        tests=[
            {"umfang": "Unit", "ergebnis": "fehlgeschlagen", "umgebung": "lokal"},
            {"umfang": "E2E", "ergebnis": "bestanden", "umgebung": "lokal"},
        ],
    )
    ober = akte.modulstaende(delta)[1]
    assert sorted(t["n"].ergebnis for t in ober["tests"]) == ["bestanden", "fehlgeschlagen"]
    assert not any(t["widerspruch"] for t in ober["tests"])


# Befund 6: Korrektur erfasst Installationen; bestätigte Meldungen korrigiert nur ein Mensch.
def test_korrektur_ersetzt_falsche_installation(akteur, delta, pc):
    ki = _ki(pc, delta)
    modul = delta.module.get()
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-1",
        version="v3",
        quelle="CI",
        modul=modul,
        installation={"umgebung": "produktion", "komponente": "app"},
    )
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-2",
        version="v3",
        quelle="CI",
        modul=modul,
        umsetzung={"umfang": "nur Umsetzung, Installation war falsch"},
        korrigiert="m-1",
    )
    alt = Installation.objects.get(meldung="m-1")
    assert alt.zustand == Installation.ERSETZT and "korrigiert durch m-2" in alt.bemerkung
    akte.stand_melden(ki, delta, meldung_id="m-3", version="v4", quelle="CI", umsetzung={})
    n = Nachweis.objects.get(meldung="m-3")
    akte.nachweis_bestaetigen(akteur, n)
    with pytest.raises(dienst.KeinZugriff):
        akte.stand_melden(
            ki, delta, meldung_id="m-4", version="v4", quelle="CI", umsetzung={}, korrigiert="m-3"
        )


# Befund 7: Wiederholung ohne beobachtet_am und revision ist eine Wiederholung, kein Konflikt.
def test_wiederholung_ohne_zeitangabe(client, akteur, delta):
    _, schluessel = dienst.bindung_anlegen(akteur, delta, name="Chat")
    bericht = {k: v for k, v in _bericht("m-1", "v1").items()}
    assert not _werkzeug(client, schluessel, "kipm_stand_melden", **bericht)[0]
    fehler, daten = _werkzeug(client, schluessel, "kipm_stand_melden", **bericht)
    assert not fehler and daten["wiederholt"] is True


# Befund 8: Eine abgewiesene Meldung bleibt unter ihrer ID abgewiesen (dokumentiert); eine neue
# meldung_id nach Behebung wirkt. Allgemein freigeben ist nicht sicher, weil manche Aufrufe vor
# einem späteren 4xx schon gewirkt haben können (siehe tests/test_befunde.py).
def test_abgewiesene_meldung_braucht_neue_id(client, akteur, delta, pc):
    ki = _ki(pc, delta)
    akte.umgebung_anlegen(ki, delta, name="Neu", art="test")
    _, schluessel = dienst.bindung_anlegen(akteur, delta, name="Chat")
    bericht = {
        **_bericht("m-1", "v1"),
        "tests": [{"umfang": "u", "ergebnis": "bestanden", "umgebung": "neu"}],
        "anfrage_kennung": "m-1",
    }
    fehler, daten = _werkzeug(client, schluessel, "kipm_stand_melden", **bericht)
    assert fehler and "Entwurf" in daten["fehler"]
    u = delta.umgebungen.get(kennung="neu")
    akte.umgebung_bestaetigen(akteur, u, erwartete_revision=u.revision)
    fehler, daten = _werkzeug(client, schluessel, "kipm_stand_melden", **bericht)
    assert fehler and daten["wiederholt"] is True
    neu = {**bericht, "meldung_id": "m-1b", "anfrage_kennung": "m-1b"}
    fehler, daten = _werkzeug(client, schluessel, "kipm_stand_melden", **neu)
    assert not fehler, daten
    assert not Nachweis.objects.filter(meldung="m-1").exists()


# Befund 9: Veröffentlichte Releases archiviert nur Fred.
def test_ki_archiviert_kein_veroeffentlichtes_release(akteur, delta, pc):
    r = akte.release_anlegen(
        akteur, delta, kennung="1.0", komponenten=[{"komponente": "a", "commit": "abc"}]
    )
    r = akte.release_zustand(akteur, r, "veroeffentlicht")
    with pytest.raises(dienst.KeinZugriff):
        akte.release_aendern(_ki(pc, delta), r, erwartete_revision=r.revision, archiviert=True)


# Befund 10: Kreisschutz prüft das Elternmodul frisch aus der Datenbank.
def test_kreisschutz_mit_veraltetem_elternobjekt(akteur, projekt):
    a = dienst.modul_anlegen(akteur, projekt, kennung="a", name="A")
    b = dienst.modul_anlegen(akteur, projekt, kennung="b", name="B")
    a_veraltet = Modul.objects.get(pk=a.pk)
    dienst.modul_aendern(akteur, a, eltern=b)
    with pytest.raises(dienst.Konflikt):
        dienst.modul_aendern(akteur, b, eltern=a_veraltet)
    b.refresh_from_db()
    assert b.eltern is None


def test_einstellungen_zeigen_nur_bestaetigte_orte_zum_setzen(owner_client, projekt, pc):
    ki = _ki(pc, projekt)
    a = dienst.wunsch_erfassen(ki, projekt, titel="T", beschreibung="b")
    dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="Nur Vorschlag", pfad=r"W:\V")
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})).content
    assert "Nur Vorschlag" not in seite.decode()
    assert Arbeitsort.objects.filter(name="Nur Vorschlag", bestaetigt=False).exists()


# --- Nachprüfung --------------------------------------------------------------------------


def _melder(url, schluessel, *argumente, schlange):
    return subprocess.run(
        [
            sys.executable,
            str(MELDER),
            *argumente,
            "--warteschlange",
            str(schlange),
            "--versuche",
            "1",
            "--zeit",
            "5",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "KIPM_URL": url, "KIPM_SCHLUESSEL": schluessel},
        timeout=60,
    )


# Befund A: neu geschriebener Bericht mit gleichem Inhalt ist eine Wiederholung.
# Befund B: Uhr des Melders vorgehend: wird auf „jetzt“ begrenzt, nicht abgewiesen.
@pytest.mark.django_db(transaction=True)
def test_melder_neu_geschriebener_bericht_und_vorgehende_uhr(live_server, akteur, tmp_path):
    p = dienst.projekt_registrieren(
        akteur, kennung="delta", name="Delta", module=[("oberflaeche", "O")]
    )
    akte.umgebung_anlegen(akteur, p, name="Lokal", art="lokal")
    _, schluessel = dienst.bindung_anlegen(akteur, p, name="CI")
    bericht = tmp_path / "b.json"
    inhalt = json.dumps(_bericht("ci-1", "v1"))
    bericht.write_text(inhalt, encoding="utf-8")
    assert (
        _melder(
            live_server.url, schluessel, "senden", str(bericht), schlange=tmp_path / "s"
        ).returncode
        == 0
    )
    os.utime(bericht, (1_900_000_000, 1_900_000_000))  # neu geschrieben, Zeit weit in der Zukunft
    bericht.write_text(inhalt, encoding="utf-8")
    zweit = _melder(live_server.url, schluessel, "senden", str(bericht), schlange=tmp_path / "s")
    assert zweit.returncode == 0, zweit.stdout
    assert json.loads(zweit.stdout)["wiederholt"] is True
    neu = tmp_path / "c.json"
    neu.write_text(json.dumps(_bericht("ci-2", "v2")), encoding="utf-8")
    os.utime(neu, (1_900_000_000, 1_900_000_000))
    dritt = _melder(live_server.url, schluessel, "senden", str(neu), schlange=tmp_path / "s")
    assert dritt.returncode == 0, dritt.stdout


def test_ersatzzeit_aus_kopfzeile_zaehlt_nicht_zum_fingerabdruck(client, akteur, delta):
    _, schluessel = dienst.bindung_anlegen(akteur, delta, name="Chat")
    c = client
    pfad = "/api/stand/melden/"
    rumpf = {**_bericht("m-1", "v1"), "anfrage_kennung": "a-1"}
    kopf = {
        "HTTP_AUTHORIZATION": f"Bearer {schluessel}",
        "HTTP_X_KIPM_BEOBACHTET": "2026-11-01T08:00:00+00:00",
    }
    erst = c.post(pfad, data=json.dumps(rumpf), content_type="application/json", **kopf)
    assert erst.status_code == 201, erst.content
    n = Nachweis.objects.get(meldung="m-1", art="umsetzung")
    assert n.beobachtet_am.isoformat().startswith("2026-11-01T08:00")
    kopf["HTTP_X_KIPM_BEOBACHTET"] = "2026-11-01T09:00:00+00:00"
    rumpf["anfrage_kennung"] = "a-2"
    zweit = c.post(pfad, data=json.dumps(rumpf), content_type="application/json", **kopf)
    assert zweit.status_code == 200 and zweit.json()["wiederholt"] is True


# Befund C: Kontextzeile nennt den Testumfang.
def test_kontextzeile_nennt_testumfang(akteur, delta):
    akte.stand_melden(
        akteur,
        delta,
        meldung_id="m-1",
        version="v1",
        quelle="CI",
        modul=delta.module.get(),
        umsetzung={},
        tests=[
            {"umfang": "Unit", "ergebnis": "fehlgeschlagen", "umgebung": "lokal"},
            {"umfang": "E2E", "ergebnis": "bestanden", "umgebung": "lokal"},
        ],
    )
    text = akte.kontext_erzeugen(akteur, delta).teile[0]
    assert "Test Lokal (Unit): fehlgeschlagen" in text and "Test Lokal (E2E): bestanden" in text


# Befund D: Bearbeiten bestätigt nicht still; „Speichern und übernehmen“ bestätigt ausdrücklich.
def test_bearbeiten_bestaetigt_nur_ausdruecklich(owner_client, projekt, pc):
    ki = _ki(pc, projekt)
    ort = dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="V", pfad=r"W:\V")
    owner_client.post(
        reverse(
            "arbeitsort_pflegen", kwargs={"kennung": "synth-a", "pk": ort.pk, "wahl": "aendern"}
        ),
        {"revision": ort.revision, f"ort{ort.pk}-name": "Umbenannt", f"ort{ort.pk}-pfad": r"W:\V"},
    )
    ort.refresh_from_db()
    assert ort.name == "Umbenannt" and not ort.bestaetigt
    a = dienst.wunsch_erfassen(ki, projekt, titel="T", beschreibung="b")
    dienst.arbeitsort_setzen(ki, a, ort)
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": a.pk, "wahl": "aendern"}),
        {
            "arbeitsort": ort.pk,
            "revision": ort.revision,
            "ort-name": "Umbenannt",
            "ort-pfad": r"W:\V",
        },
    )
    ort.refresh_from_db()
    assert ort.bestaetigt


# Befund E (Runde 3 überarbeitet): Korrektur einer falschen Installation macht nichts
# automatisch wieder aktuell, sondern zeigt die Lücke; N1: Freds Urteil ist für die KI tabu;
# N2: keine Doppelkorrektur; N5: Version und Ereignis für die korrigierte Installation.
def test_korrektur_zeigt_luecke_statt_wiederherstellen(akteur, delta, pc):
    from steuerung.models import Ereignis, Objektversion

    ki = _ki(pc, delta)
    modul = delta.module.get()
    p = akte.installation_melden(
        akteur,
        delta,
        umgebung=delta.umgebungen.get(kennung="produktion"),
        komponente="app",
        version="v2",
        herkunft="Fred",
        installiert_am="2026-11-01T08:00",
    )
    akte.installation_bewerten(akteur, p, bestaetigt=True)
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-1",
        version="v3",
        quelle="CI",
        modul=modul,
        installation={"umgebung": "produktion", "komponente": "app"},
        beobachtet_am="2026-11-02T08:00",
    )
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-2",
        version="v3",
        quelle="CI",
        modul=modul,
        umsetzung={},
        korrigiert="m-1",
    )
    p.refresh_from_db()
    falsch = Installation.objects.get(meldung="m-1")
    assert p.zustand == Installation.ERSETZT and falsch.korrigiert_durch == "m-2"
    ober = akte.modulstaende(delta)[1]
    assert any("korrigiert" in x and "neu melden" in x for x in ober["luecken"])
    assert Objektversion.objects.filter(objekt_typ="Installation", objekt_id=falsch.pk).count() >= 2
    assert Ereignis.objects.filter(
        objekt_typ="Installation", ereignis__contains="korrigiert"
    ).exists()
    with pytest.raises(dienst.Konflikt):
        akte.stand_melden(
            ki, delta, meldung_id="m-3", version="v3", quelle="CI", umsetzung={}, korrigiert="m-1"
        )


def test_ki_korrigiert_keine_von_fred_beurteilte_installation(akteur, delta, pc):
    ki = _ki(pc, delta)
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-f",
        version="v1",
        quelle="CI",
        installation={"umgebung": "produktion", "komponente": "app"},
    )
    f = Installation.objects.get(meldung="m-f")
    akte.installation_bewerten(akteur, f, bestaetigt=False)
    with pytest.raises(dienst.KeinZugriff):
        akte.stand_melden(
            ki, delta, meldung_id="m-f2", version="v1", quelle="CI", umsetzung={}, korrigiert="m-f"
        )
    f.refresh_from_db()
    assert f.zustand == Installation.FEHLGESCHLAGEN


# N4: erneutes „senden“ behält die Zeit des ersten Versuchs aus der Warteschlange.
def test_erneutes_senden_behaelt_ersten_versuch(tmp_path):
    bericht = tmp_path / "b.json"
    bericht.write_text(json.dumps({"meldung_id": "q-1", "version": "v1", "quelle": "CI"}))
    schlange = tmp_path / "s"
    schlange.mkdir()
    (schlange / "q-1.json").write_text(
        json.dumps(
            {"bericht": {"meldung_id": "q-1"}, "erster_versuch": "2026-11-01T08:00:00+00:00"}
        )
    )
    _melder("http://127.0.0.1:9", "kipm_x_y", "senden", str(bericht), schlange=schlange)
    eintrag = json.loads((schlange / "q-1.json").read_text(encoding="utf-8"))
    assert eintrag["erster_versuch"] == "2026-11-01T08:00:00+00:00"


# B1: Eine korrigierte Installation ist kein Maßstab für den jüngsten Stand.
def test_korrektur_mit_frueherer_richtiger_zeit_wird_aktuell(akteur, delta, pc):
    ki = _ki(pc, delta)
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-1",
        version="v3",
        quelle="CI",
        installation={"umgebung": "produktion", "komponente": "app"},
        beobachtet_am="2026-11-02T08:00",
    )
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-2",
        version="v3",
        quelle="CI",
        installation={"umgebung": "produktion", "komponente": "app"},
        beobachtet_am="2026-11-02T07:30",
        korrigiert="m-1",
    )
    aktuell = Installation.objects.exclude(zustand=Installation.ERSETZT).get(komponente="app")
    assert aktuell.meldung == "m-2"


# B2: Die Lücke schließt sich auch, wenn der Stand ohne Modul neu gemeldet wird.
def test_luecke_schliesst_sich_bei_neumeldung_ohne_modul(akteur, delta, pc):
    ki = _ki(pc, delta)
    modul = delta.module.get()
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-1",
        version="v3",
        quelle="CI",
        modul=modul,
        installation={"umgebung": "produktion", "komponente": "app"},
    )
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-2",
        version="v3",
        quelle="CI",
        modul=modul,
        umsetzung={},
        korrigiert="m-1",
    )
    akte.installation_melden(
        akteur,
        delta,
        umgebung=delta.umgebungen.get(kennung="produktion"),
        komponente="app",
        version="v2",
        herkunft="Fred",
    )
    ober = akte.modulstaende(delta)[1]
    assert not any("neu melden" in x for x in ober["luecken"])


# Runde 5: Eine fehlgeschlagene Installation ist kein gültiger Stand und schließt die Lücke nicht.
def test_fehlgeschlagene_installation_schliesst_luecke_nicht(akteur, delta, pc):
    ki = _ki(pc, delta)
    prod = delta.umgebungen.get(kennung="produktion")
    a = akte.installation_melden(
        akteur,
        delta,
        umgebung=prod,
        komponente="app",
        version="v1",
        herkunft="Fred",
        installiert_am="2026-11-01T08:00",
    )
    akte.installation_bewerten(akteur, a, bestaetigt=False)
    modul = delta.module.get()
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-1",
        version="v2",
        quelle="CI",
        modul=modul,
        installation={"umgebung": "produktion", "komponente": "app"},
    )
    akte.stand_melden(
        ki,
        delta,
        meldung_id="m-2",
        version="v2",
        quelle="CI",
        modul=modul,
        umsetzung={},
        korrigiert="m-1",
    )
    ober = akte.modulstaende(delta)[1]
    assert any("neu melden" in x for x in ober["luecken"])

"""Regressionstests zu den Befunden der unabhängigen Gegenprüfung (Verbesserungsliste und A-2)."""

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Arbeitsort, Modul
from tests.test_mcp import _werkzeug


@pytest.fixture
def pc(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Assistent")
    b.klartext = schluessel
    return b


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


# Befund 3: Der Melder stempelt beobachtet_am; Nachlieferung verdrängt keinen neueren Stand.
# Befund 4: Funktionsnachweise ohne Meldevertrag erscheinen nicht als Projektstand.
# Befund 5: Tests mit verschiedenem Umfang widersprechen sich nicht; alle bleiben sichtbar.
# Befund 6: Korrektur erfasst Installationen; bestätigte Meldungen korrigiert nur ein Mensch.
# Befund 7: Wiederholung ohne beobachtet_am und revision ist eine Wiederholung, kein Konflikt.
# Befund 8: Eine abgewiesene Meldung bleibt unter ihrer ID abgewiesen (dokumentiert); eine neue
# meldung_id nach Behebung wirkt. Allgemein freigeben ist nicht sicher, weil manche Aufrufe vor
# einem späteren 4xx schon gewirkt haben können (siehe tests/test_befunde.py).
# Befund 9: Veröffentlichte Releases archiviert nur Fred.
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


# Befund A: neu geschriebener Bericht mit gleichem Inhalt ist eine Wiederholung.
# Befund B: Uhr des Melders vorgehend: wird auf „jetzt“ begrenzt, nicht abgewiesen.
# Befund C: Kontextzeile nennt den Testumfang.
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
# N4: erneutes „senden“ behält die Zeit des ersten Versuchs aus der Warteschlange.
# B1: Eine korrigierte Installation ist kein Maßstab für den jüngsten Stand.
# B2: Die Lücke schließt sich auch, wenn der Stand ohne Modul neu gemeldet wird.
# Runde 5: Eine fehlgeschlagene Installation ist kein gültiger Stand und schließt die Lücke nicht.

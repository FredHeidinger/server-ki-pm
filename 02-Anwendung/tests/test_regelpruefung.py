import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from steuerung.models import Abhaengigkeit, Auftrag, Meldung, Revision
from tests.conftest import arbeitspaket
from tests.test_mcp import _werkzeug

pytestmark = pytest.mark.django_db


def test_regelpruefung_meldet_altbestand_mit_belegen_ohne_einen_schreibzugriff(
    client, projekt, akteur, bindung
):
    ort = projekt.arbeitsorte.get()
    ort.pfad = r"W:\Pruefkopie"
    ort.save(update_fields=["pfad"])
    a = arbeitspaket(akteur, projekt, "Oberfläche", "frontend/**")
    b = arbeitspaket(akteur, projekt, "Kern", "backend/**")
    falscher_bedarf = f"Abschluss benötigt den Export aus {a.nummer}."
    Revision.objects.filter(auftrag=b, nummer=b.revision_nr).update(
        abnahmekriterien=falscher_bedarf
    )
    Abhaengigkeit.objects.create(auftrag=a, vorgaenger=b)
    Auftrag.objects.filter(pk__in=[a.pk, b.pk]).update(
        zustand=Auftrag.IN_ARBEIT,
        bearbeiter_bindung=bindung,
        bearbeiter_name=bindung.name,
    )
    for _ in range(3):
        Meldung.objects.create(
            auftrag=b,
            revision_nr=b.revision_nr,
            art=Meldung.SITZUNGSENDE,
            text="Chat beendet, Ergebnis fehlt.",
            akteur_name=bindung.name,
            bindung=bindung,
        )
    Meldung.objects.create(
        auftrag=b,
        revision_nr=b.revision_nr,
        art=Meldung.FORTSCHRITT,
        text="Seit dem letzten Sitzungsende erneut in Arbeit.",
        akteur_name=bindung.name,
        bindung=bindung,
    )
    with CaptureQueriesContext(connection) as sql:
        fehler, bericht = _werkzeug(
            client,
            bindung.klartext,
            "kipm_regelpruefung",
            projekt=projekt.kennung,
            fred_sagte=None,
        )
    assert not fehler, bericht
    assert bericht["projekt"] == projekt.kennung
    assert not [
        q["sql"]
        for q in sql.captured_queries
        if q["sql"].lstrip().split(maxsplit=1)[0].upper()
        in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
    ]
    nach_code = {r["code"]: r for r in bericht["befunde"]}
    assert nach_code["abhaengigkeitskreis"]["weg"] == [a.nummer, b.nummer, a.nummer]
    assert nach_code["abhaengigkeitskreis"]["status"] == "hinweis_altbestand"
    assert nach_code["kriterium_nicht_erfuellbar"]["text"] == falscher_bedarf
    assert nach_code["ordner_doppelt_belegt"]["auftraege"] == [a.nummer, b.nummer]
    assert nach_code["festgefahren"]["auftraege"] == [b.nummer]
    assert nach_code["festgefahren"]["status"] == "hinweis_altbestand"


def test_regelpruefung_respektiert_projektgrenze_und_unbekannten_ordner(
    client, projekt, projekt_b, bindung, akteur
):
    with CaptureQueriesContext(connection) as sql:
        fehler, _ = _werkzeug(
            client,
            bindung.klartext,
            "kipm_regelpruefung",
            projekt=projekt_b.kennung,
            fred_sagte=None,
        )
    assert fehler
    assert not any(q["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ")) for q in sql)
    from steuerung import dienst

    _, pc = dienst.bindung_anlegen(akteur, None, name="Lesen am PC")
    with CaptureQueriesContext(connection) as sql:
        fehler, _ = _werkzeug(
            client, pc, "kipm_regelpruefung", ordner=r"W:\Unbekannt", fred_sagte=None
        )
    assert fehler
    assert not any(q["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ")) for q in sql)

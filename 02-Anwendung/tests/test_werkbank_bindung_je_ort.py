"""WB06 (Umsetzungsplan Drei Wege, Stück 2): eine Werkbank-Bindung je Arbeitsort statt je Projekt.

Ein KI-PM-Projekt mit mehreren Repositories (je Repository ein bestätigter Arbeitsort) bekam bisher
genau eine Bindung je Werkbank, und die nahm den ersten Arbeitsort. Damit konnte ein Auftrag nie
zwei Repositories desselben Projekts als Verbund umfassen. Jetzt hat jeder aktive, bestätigte
Arbeitsort seine eigene Bindung mit eigenem Kopplungscode; eine alte Bindung ohne Ort bleibt die
des ersten Orts.
"""

import base64
import json

import pytest
from django.db import IntegrityError, transaction

from steuerung import dienst, werkbank, werkbank_verbund
from steuerung.models import Auftrag, WerkbankProjektbindung

pytestmark = pytest.mark.django_db


def _code(text: str) -> dict:
    roh = text.split(":", 1)[1]
    return json.loads(base64.urlsafe_b64decode(roh + "=" * (-len(roh) % 4)).decode("utf-8"))


def _ort(akteur, projekt, kennung, pfad, **kw):
    return dienst.arbeitsort_anlegen(
        akteur, projekt, kennung=kennung, name=kennung.capitalize(), rechner="PC", pfad=pfad, **kw
    )


def test_jeder_bestaetigte_arbeitsort_bekommt_seine_bindung_mit_eigenem_code(
    akteur, projekt, w_instanz
):
    erste = w_instanz.projekte.get()
    backend = _ort(akteur, projekt, "backend", "C:/Shop/Backend")
    frontend = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    bindungen = list(w_instanz.projekte.order_by("id"))
    assert [b.arbeitsort_id for b in bindungen] == [erste.arbeitsort_id, backend.pk, frontend.pk]
    assert erste in bindungen and erste.version == 1, "die bestehende Bindung bleibt unverändert"
    codes = [
        _code(werkbank.kopplungscode(w_instanz, None, "https://a/", bindung=b)) for b in bindungen
    ]
    assert [c["projekte"][0]["arbeitsort"] for c in codes] == [
        None,
        "C:/Shop/Backend",
        "C:/Shop/Frontend",
    ]
    assert len({c["projekte"][0]["projektbindung_id"] for c in codes}) == 3
    alle = _code(werkbank.kopplungscode(w_instanz, "geheim", "https://a/"))
    assert sorted(p["arbeitsort"] or "" for p in alle["projekte"]) == [
        "",
        "C:/Shop/Backend",
        "C:/Shop/Frontend",
    ]


def test_ein_vorschlag_bekommt_erst_mit_der_bestaetigung_eine_bindung(akteur, projekt, w_instanz):
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    vorschlag = _ort(
        dienst.Akteur.aus_bindung(pc, projekt=projekt), projekt, "frontend", "C:/Shop/Frontend"
    )
    assert not vorschlag.bestaetigt and w_instanz.projekte.count() == 1
    dienst.arbeitsort_bestaetigen(akteur, vorschlag, erwartete_revision=vorschlag.revision)
    assert w_instanz.projekte.filter(arbeitsort=vorschlag).count() == 1


def test_eine_alte_bindung_ohne_ort_steht_fuer_den_ersten_ort_und_bekommt_keine_zweite(
    akteur, projekt, w_instanz
):
    alt = w_instanz.projekte.get()
    WerkbankProjektbindung.objects.filter(pk=alt.pk).update(arbeitsort=None)
    werkbank.bindungen_nachziehen(projekt)
    assert w_instanz.projekte.count() == 1, "der erste Ort ist schon durch die alte Bindung belegt"
    zweiter = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    assert (
        w_instanz.projekte.count() == 2 and w_instanz.projekte.filter(arbeitsort=zweiter).exists()
    )
    alt.refresh_from_db()
    assert werkbank.bindungsort(alt) == projekt.arbeitsorte.order_by("id").first()


def test_je_ort_und_werkbank_hoechstens_eine_bindung(akteur, projekt, w_instanz):
    ort = projekt.arbeitsorte.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        WerkbankProjektbindung.objects.create(instanz=w_instanz, projekt=projekt, arbeitsort=ort)
    WerkbankProjektbindung.objects.create(instanz=w_instanz, projekt=projekt)  # eine ohne Ort
    with pytest.raises(IntegrityError), transaction.atomic():
        WerkbankProjektbindung.objects.create(instanz=w_instanz, projekt=projekt)


def test_ein_auftrag_kann_zwei_repositories_desselben_projekts_als_verbund_umfassen(
    akteur, projekt, w_instanz
):
    backend = _ort(akteur, projekt, "backend", "C:/Shop/Backend")
    frontend = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Preise anzeigen", beschreibung="Preise im Shop"
    )
    ziel = w_instanz.projekte.get(arbeitsort=backend)
    werkbank.zuweisen(akteur, a, ziel)
    werkbank_verbund.verbund_setzen(akteur, a, [w_instanz.projekte.get(arbeitsort=frontend)])
    z = Auftrag.objects.get(pk=a.pk).zuweisung
    snap = werkbank_verbund.snapshot_v2(Auftrag.objects.get(pk=a.pk), z)
    assert [m["arbeitsort"]["name"] for m in snap["zielverbund"]] == ["Backend", "Frontend"]
    assert [m["fuehrend"] for m in snap["zielverbund"]] == [True, False]
    assert {m["projekt"] for m in snap["zielverbund"]} == {projekt.kennung}


def test_eine_fremde_bindung_desselben_projekts_ist_keine_quelle_der_zuweisung(
    akteur, projekt, w_instanz
):
    # Gegenprüfung WB06 (F1): mit mehreren Bindungen je Projekt prüft die Quelle, dass sie das Ziel der Zuweisung ist
    from tests.werkbank_hilfen import vor_quelle

    backend = _ort(akteur, projekt, "backend", "C:/Shop/Backend")
    frontend = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Preise anzeigen", beschreibung="Preise im Shop"
    )
    ziel = w_instanz.projekte.get(arbeitsort=backend)
    werkbank.zuweisen(akteur, a, ziel)
    a = Auftrag.objects.get(pk=a.pk)
    assert werkbank.quelle_pruefen(w_instanz, vor_quelle(w_instanz, ziel, a)).projektbindung == ziel
    with pytest.raises(werkbank.WerkbankFehler) as exc:
        werkbank.quelle_pruefen(
            w_instanz, vor_quelle(w_instanz, w_instanz.projekte.get(arbeitsort=frontend), a)
        )
    assert exc.value.status == 404


def test_eine_alte_bindung_ohne_ort_wird_beim_naechsten_ort_fest_und_wandert_nicht(
    akteur, projekt, w_instanz
):
    # Gegenprüfung WB06 (F2): „erster bestätigter Ort“ verschiebt sich (späte Bestätigung, Archiv) -- die alte Bindung
    # bekommt darum beim Nachziehen den Ort, für den sie gerade steht, fest eingetragen
    erster = projekt.arbeitsorte.get()
    alt = w_instanz.projekte.get()
    WerkbankProjektbindung.objects.filter(pk=alt.pk).update(arbeitsort=None)
    zweiter = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    alt.refresh_from_db()
    assert alt.arbeitsort_id == erster.pk and alt.version == 1, (
        "fest auf dem Ort, für den sie stand"
    )
    dienst.arbeitsort_aendern(akteur, erster, erwartete_revision=erster.revision, aktiv=False)
    werkbank.bindungen_nachziehen(projekt)
    alt.refresh_from_db()
    assert alt.arbeitsort_id == erster.pk, (
        "ein archivierter erster Ort verschiebt die Bindung nicht"
    )
    assert list(w_instanz.projekte.filter(arbeitsort=zweiter)) != [alt]


def test_die_zuweisung_schlaegt_die_bindung_des_geplanten_arbeitsorts_vor(
    owner_client, akteur, projekt, w_instanz
):
    # Gegenprüfung WB06 (R1): bei mehreren Bindungen ist der Ort der Auftragsrevision vorgewählt, nicht die erste in der Liste
    from django.urls import reverse

    _ort(akteur, projekt, "backend", "C:/Shop/Backend")
    frontend = _ort(akteur, projekt, "frontend", "C:/Shop/Frontend")
    a = dienst.wunsch_erfassen(
        akteur, projekt, titel="Preise anzeigen", beschreibung="Preise im Shop"
    )
    dienst.arbeitsort_setzen(akteur, a, frontend)
    html = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})).content.decode()
    ziel = w_instanz.projekte.get(arbeitsort=frontend)
    assert f'<option value="{ziel.pk}" selected>' in html, html.split('name="ziel"', 1)[-1][:600]

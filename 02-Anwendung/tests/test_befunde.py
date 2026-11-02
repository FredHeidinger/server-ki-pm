"""Regressionstests zu den Befunden der unabhängigen Prüfung (Wettläufe, Pause, Identität)."""

import json
import threading

import pytest
from django.db import connection
from django.test import Client

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Meldung
from tests.conftest import arbeitspaket


def _api(schluessel: str) -> Client:
    return Client(headers={"Authorization": f"Bearer {schluessel}"})


def _post(c: Client, pfad: str, **daten):
    return c.post(pfad, data=json.dumps(daten), content_type="application/json")


@pytest.mark.django_db(transaction=True)
def test_gleiche_kennung_gleichzeitig_nur_eine_wirkung(owner):
    akteur = dienst.Akteur.aus_benutzer(owner)
    projekt = dienst.projekt_registrieren(akteur, kennung="synth-w", name="Wettlauf")
    dienst.arbeitsort_anlegen(akteur, projekt, kennung="repo", name="Repo")
    auftrag = arbeitspaket(akteur, projekt, umfang="w/**")
    dienst.auftrag_freigeben(akteur, auftrag)
    bindung, schluessel = dienst.bindung_anlegen(akteur, projekt, name="Chat W")
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), auftrag, 2)
    pfad = f"/api/auftraege/{auftrag.pk}/fortschritt/"
    antworten: list[int] = []
    schranke = threading.Barrier(2)

    def anfrage():
        try:
            schranke.wait(timeout=5)
            antworten.append(
                _post(
                    _api(schluessel), pfad, revision=2, text="doppelt?", anfrage_kennung="k"
                ).status_code
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=anfrage) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(antworten) == 2 and 200 in antworten
    assert Meldung.objects.filter(auftrag=auftrag, text="doppelt?").count() == 1
    # Nach Abschluss liefert dieselbe Kennung die gespeicherte Antwort
    wieder = _post(_api(schluessel), pfad, revision=2, text="doppelt?", anfrage_kennung="k")
    assert wieder.status_code == 200 and wieder.json()["wiederholt"] is True
    assert Meldung.objects.filter(auftrag=auftrag, text="doppelt?").count() == 1


@pytest.mark.django_db
def test_kennung_wird_bei_fehler_freigegeben(freigegeben, bindung):
    c = _api(bindung.klartext)
    pfad = f"/api/auftraege/{freigegeben.pk}/fortschritt/"
    # Erster Versuch scheitert fachlich (kein Bearbeiter): 403, Kennung trägt diese Antwort
    erste = _post(c, pfad, revision=2, text="x", anfrage_kennung="f")
    assert erste.status_code == 403
    zweite = _post(c, pfad, revision=2, text="x", anfrage_kennung="f")
    assert zweite.status_code == 403 and zweite.json()["wiederholt"] is True


@pytest.mark.django_db
def test_pause_gibt_schreibumfang_nicht_frei(akteur, projekt, freigegeben, bindung):
    dienst.auftrag_uebernehmen(dienst.Akteur.aus_bindung(bindung), freigegeben, 2)
    dienst.auftrag_pausieren(akteur, freigegeben, "kurz warten")
    zweiter = arbeitspaket(akteur, projekt, titel="Zweiter", umfang="src/auth/x.py")
    with pytest.raises(dienst.Konflikt, match="Überschneidung"):
        dienst.auftrag_freigeben(akteur, zweiter)
    dienst.auftrag_fortsetzen(akteur, freigegeben)
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT


@pytest.mark.django_db
def test_hindernis_wechselt_zustand_im_selben_commit_mit_ereignis(
    akteur, projekt, freigegeben, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.hindernis_melden(chat, freigegeben, 2, "Datei fehlt")
    assert freigegeben.zustand == Auftrag.BLOCKIERT
    ereignis = Ereignis.objects.filter(objekt_kennung=freigegeben.nummer).first()
    assert ereignis.ereignis == "Hindernis gemeldet, Auftrag Blockiert"


@pytest.mark.django_db
def test_chat_schlaegt_entscheidung_nur_fuer_eigenes_projekt_vor(projekt, projekt_b, bindung):
    c = _api(bindung.klartext)
    antwort = _post(
        c,
        "/api/entscheidungen/vorschlagen/",
        titel="T",
        text="X",
        projekte=["synth-a", "synth-b", "geraten"],
        anfrage_kennung="e",
    )
    assert antwort.status_code == 201
    assert antwort.json()["entscheidung"]["betroffen"] == ["synth-a"]


@pytest.mark.django_db
def test_doppelter_vorgaenger_ist_verstaendlicher_fehler(akteur, projekt):
    a = arbeitspaket(akteur, projekt, titel="A", umfang="a/**")
    b = arbeitspaket(akteur, projekt, titel="B", umfang="b/**")
    dienst.vorgaenger_eintragen(akteur, b, a)
    with pytest.raises(dienst.DienstFehler, match="bereits als Vorgänger"):
        dienst.vorgaenger_eintragen(akteur, b, a)


@pytest.mark.django_db
def test_vorschlag_allein_macht_keinen_bearbeiter(akteur, projekt, freigegeben, bindung):
    chat_a = dienst.Akteur.aus_bindung(bindung)
    zweite, _ = dienst.bindung_anlegen(akteur, projekt, name="Chat Zwei")
    chat_zwei = dienst.Akteur.aus_bindung(zweite)
    dienst.vorschlag_einreichen(chat_zwei, freigegeben, 2, "Idee")
    dienst.auftrag_uebernehmen(chat_a, freigegeben, 2)
    with pytest.raises(dienst.KeinZugriff):
        dienst.fortschritt_melden(chat_zwei, freigegeben, 2, "ich auch")
    # Der frühere Bearbeiter wird an seiner Identität erkannt, nicht am Namen
    dienst.bearbeitung_unklar(akteur, freigegeben, "verschwunden")
    with pytest.raises(dienst.Konflikt, match="historisch"):
        dienst.fortschritt_melden(chat_a, freigegeben, 2, "bin wieder da")
    gleichnamig, _ = dienst.bindung_anlegen(akteur, projekt, name="Chat A")
    with pytest.raises(dienst.KeinZugriff):
        dienst.fortschritt_melden(
            dienst.Akteur.aus_bindung(gleichnamig), freigegeben, 2, "ich bin Chat A"
        )


@pytest.mark.django_db
def test_bearbeiten_mit_veraltetem_objekt_wird_abgewiesen(akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    veraltet = Auftrag.objects.get(pk=freigegeben.pk)  # Stand vor dem Einreichen
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="x", dateien="a@1", pruefungen="p"
    )
    veraltet.zustand = Auftrag.IN_ARBEIT  # was der Aufrufer noch zu sehen glaubt
    with pytest.raises(dienst.Konflikt, match="inzwischen"):
        dienst.auftrag_bearbeiten(
            akteur,
            veraltet,
            titel="T",
            beschreibung="",
            ziel="Z",
            nicht_ziele="keine",
            arbeitsort=None,
            schreibumfang="",
            quellen="",
            abnahmekriterien="K",
            prioritaet="hoch",
            modul=None,
        )
    assert freigegeben.revisionen.count() == 2


@pytest.mark.parametrize(
    ("a", "b"),
    [("Src/x.py", "src/x.py"), ("./docs/**", "docs/a.md"), ("docs\\hilfe\\**", "docs/hilfe/x.md")],
)
def test_muster_normalisierung(a, b):
    assert dienst.muster_ueberschneiden(a, b)

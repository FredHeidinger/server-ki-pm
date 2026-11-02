"""Oberfläche: Seiten rendern, Formulare wirken, Aktionen laufen über den Dienst, kein HTML-Durchgriff."""

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag, Entscheidung, Pruefung
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def test_alle_bereiche_rendern(owner_client, projekt, freigegeben):
    for name in (
        "uebersicht",
        "auftraege",
        "pruefung",
        "entscheidungen",
        "verlauf",
        "wunsch_neu",
        "entscheidung_neu",
    ):
        assert owner_client.get(reverse(name)).status_code == 200, name
    assert (
        owner_client.get(reverse("projekt_detail", kwargs={"kennung": "synth-a"})).status_code
        == 200
    )
    assert (
        owner_client.get(
            reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
        ).status_code
        == 200
    )
    assert (
        owner_client.get(
            reverse("auftrag_bearbeiten", kwargs={"auftrag_id": freigegeben.pk})
        ).status_code
        == 200
    )


def test_wunsch_formular_und_auftragsseite(owner_client, projekt):
    antwort = owner_client.post(
        reverse("wunsch_neu"),
        {
            "projekt": projekt.pk,
            "titel": "Schneller starten",
            "beschreibung": "Dauert zu lange.",
            "prioritaet": "mittel",
        },
    )
    assert antwort.status_code == 302
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "Wunsch – unvollständig" in seite and "Freigeben – nicht möglich" in seite
    assert "Ziel vorhanden" in seite and "✘" in seite


def test_konkretisieren_und_freigeben_ueber_oberflaeche(owner_client, owner, projekt):
    auftrag = dienst.wunsch_erfassen(
        dienst.Akteur.aus_benutzer(owner), projekt, titel="T", beschreibung="B"
    )
    antwort = owner_client.post(
        reverse("auftrag_bearbeiten", kwargs={"auftrag_id": auftrag.pk}),
        {
            "titel": "T",
            "prioritaet": "hoch",
            "modul": "",
            "beschreibung": "B",
            "ziel": "Ziel",
            "nicht_ziele": "keine",
            "arbeitsort": projekt.arbeitsorte.first().pk,
            "schreibumfang": "src/**",
            "quellen": "",
            "abnahmekriterien": "K1\nK2",
            "pruefer": owner.pk,
            "anlass": "konkretisiert",
        },
    )
    assert antwort.status_code == 302
    auftrag.refresh_from_db()
    assert auftrag.revision_nr == 2 and auftrag.pruefer == owner
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "Arbeitspaket – freigabefähig" in seite
    antwort = owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": auftrag.pk, "aktion": "freigeben"})
    )
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.FREIGEGEBEN


def test_vorgaenger_knopf_bei_ueberschneidung(owner_client, akteur, projekt, freigegeben):
    zweiter = arbeitspaket(akteur, projekt, titel="Zweiter", umfang="src/auth/x.py")
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": zweiter.pk})
    ).content.decode()
    assert f"{freigegeben.nummer} als Vorgänger eintragen" in seite
    owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": zweiter.pk, "aktion": "vorgaenger"}),
        {"vorgaenger": freigegeben.pk, "grund": "Überschneidung"},
    )
    assert zweiter.abhaengigkeiten.count() == 1


def test_pruefung_ueber_oberflaeche(owner_client, akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    e = dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="fertig", dateien="a@1", pruefungen="p ok"
    )
    liste = owner_client.get(reverse("pruefung")).content.decode()
    assert freigegeben.nummer in liste
    seite = owner_client.get(
        reverse("pruefung_detail", kwargs={"ergebnis_id": e.pk})
    ).content.decode()
    assert "Hinweis nennt nächsten Schritt" in seite
    antwort = owner_client.post(
        reverse("pruefung_detail", kwargs={"ergebnis_id": e.pk}),
        {
            "k0": "ERFUELLT",
            "a0": "",
            "k1": "NICHT_ERFUELLT",
            "a1": "noch Code",
            "entscheidung": "ABGENOMMEN",
        },
    )
    assert antwort.status_code == 200 and "nicht erfüllt" in antwort.content.decode()
    antwort = owner_client.post(
        reverse("pruefung_detail", kwargs={"ergebnis_id": e.pk}),
        {"k0": "ERFUELLT", "a0": "", "k1": "ERFUELLT", "a1": "", "entscheidung": "ABGENOMMEN"},
    )
    assert antwort.status_code == 302
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ABGENOMMEN and Pruefung.objects.count() == 1


def test_selbstabnahme_in_oberflaeche_gesperrt(
    owner_client, owner, akteur, projekt, zweiter_mensch
):
    auftrag = arbeitspaket(akteur, projekt, pruefer=zweiter_mensch)
    dienst.auftrag_freigeben(akteur, auftrag)
    owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": auftrag.pk, "aktion": "selbst"})
    )
    auftrag.refresh_from_db()
    assert auftrag.bearbeiter_benutzer == owner
    antwort = owner_client.post(
        reverse("eigenes_ergebnis", kwargs={"auftrag_id": auftrag.pk}),
        {"zusammenfassung": "fertig", "dateien": "a@1", "pruefungen": "p", "luecken": ""},
    )
    assert antwort.status_code == 302
    e = auftrag.ergebnisse.first()
    seite = owner_client.get(
        reverse("pruefung_detail", kwargs={"ergebnis_id": e.pk})
    ).content.decode()
    assert "Selbstabnahme ist nicht erlaubt" in seite
    antwort = owner_client.post(
        reverse("pruefung_detail", kwargs={"ergebnis_id": e.pk}),
        {"k0": "ERFUELLT", "a0": "", "k1": "ERFUELLT", "a1": "", "entscheidung": "ABGENOMMEN"},
    )
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ZUR_PRUEFUNG


def test_bindung_anlegen_zeigt_schluessel_einmal(owner_client, projekt):
    antwort = owner_client.post(
        reverse("bindung_neu", kwargs={"kennung": projekt.kennung}),
        {"bindung-name": "Chat A", "bindung-gueltig_tage": 30},
    )
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "kipm_" in seite and "wird nur jetzt angezeigt" in seite
    nochmal = owner_client.get(
        reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert "kipm_" not in nochmal
    bindung = projekt.bindungen.get()
    owner_client.post(reverse("bindung_widerrufen", kwargs={"bindung_id": bindung.pk}))
    bindung.refresh_from_db()
    assert bindung.widerrufen_am is not None


def test_entscheidung_ueber_oberflaeche(owner_client, projekt, projekt_b):
    antwort = owner_client.post(
        reverse("entscheidung_neu"),
        {
            "titel": "Fehlertext-Richtlinie",
            "text": "Jeder Fehler nennt den nächsten Schritt.",
            "verweis": "",
            "projekte": [projekt.pk, projekt_b.pk],
            "sofort_entscheiden": "on",
        },
    )
    assert antwort.status_code == 302
    e = Entscheidung.objects.get()
    assert e.zustand == Entscheidung.ENTSCHIEDEN and e.stand == 1
    owner_client.post(
        reverse("entscheidung_aktion", kwargs={"entscheidung_id": e.pk, "aktion": "bestaetigen"}),
        {"projekt": projekt.pk},
    )
    liste = owner_client.get(reverse("entscheidungen")).content.decode()
    assert "synth-a: ✔" in liste and "synth-b: offen" in liste
    antwort = owner_client.post(
        reverse(
            "entscheidung_aktion", kwargs={"entscheidung_id": e.pk, "aktion": "umsetzungsauftrag"}
        ),
        {"projekt": projekt_b.pk},
    )
    assert antwort.status_code == 302
    auftrag = Auftrag.objects.get(projekt=projekt_b)
    assert auftrag.entscheidung == e and auftrag.zustand == Auftrag.ENTWURF


def test_nutzertexte_werden_nicht_ausgefuehrt(owner_client, owner, projekt, bindung):
    a = dienst.wunsch_erfassen(
        dienst.Akteur.aus_benutzer(owner),
        projekt,
        titel="<script>x()</script>",
        beschreibung="<img src=x onerror=alert(1)>",
    )
    dienst.vorschlag_einreichen(dienst.Akteur.aus_bindung(bindung), a, 1, "<b>fett</b>")
    for url in (
        reverse("auftraege"),
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}),
        reverse("verlauf"),
    ):
        seite = owner_client.get(url).content.decode()
        assert "<script>x()</script>" not in seite and "<img src=x" not in seite
        assert "<b>fett</b>" not in seite
    detail = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk})
    ).content.decode()
    assert "&lt;img src=x onerror=alert(1)&gt;" in detail and "&lt;b&gt;fett&lt;/b&gt;" in detail


def test_anmeldung_wird_nach_fehlversuchen_begrenzt(client, owner):
    from django.core.cache import cache

    cache.clear()
    for _ in range(8):
        client.post(reverse("anmelden"), {"username": "owner-test", "password": "falsch"})
    antwort = client.post(
        reverse("anmelden"), {"username": "owner-test", "password": "nur-fuer-tests-123456"}
    )
    assert antwort.status_code == 200 and "Zu viele Fehlversuche" in antwort.content.decode()
    cache.clear()
    assert (
        client.post(
            reverse("anmelden"), {"username": "owner-test", "password": "nur-fuer-tests-123456"}
        ).status_code
        == 302
    )


def test_verlauf_filter(owner_client, projekt, freigegeben):
    seite = owner_client.get(
        reverse("verlauf") + "?objekt=Auftrag&kennung=" + freigegeben.nummer
    ).content.decode()
    assert "Freigegeben" in seite and "Projekt registriert" not in seite

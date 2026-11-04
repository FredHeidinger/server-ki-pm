"""Oberfläche: Seiten rendern, Formulare wirken, Aktionen laufen über den Dienst, kein HTML-Durchgriff."""

import html

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag, Meldung, Pruefung
from tests.conftest import arbeitspaket

pytestmark = pytest.mark.django_db


def test_alle_bereiche_rendern(owner_client, projekt, freigegeben):
    for name in ("start", "projekte", "wunsch_neu", "verbindungen"):
        assert owner_client.get(reverse(name)).status_code == 200, name
    for name in (
        "projekt_detail",
        "projekt_auftraege",
        "akte_wissen",
        "projekt_verlauf",
        "projekt_einstellungen",
        "akte_profil",
        "unterlage_neu",
    ):
        seite = owner_client.get(reverse(name, kwargs={"kennung": "synth-a"}))
        assert seite.status_code == 200, name
    for name in ("auftrag_detail", "auftrag_bearbeiten"):
        assert (
            owner_client.get(reverse(name, kwargs={"auftrag_id": freigegeben.pk})).status_code
            == 200
        )


def test_wunsch_formular_und_auftragsseite(owner_client, projekt):
    antwort = owner_client.post(
        reverse("wunsch_neu"),
        {
            "typ": "auftrag",
            "projekt": projekt.pk,
            "titel": "Schneller starten",
            "beschreibung": "Dauert zu lange.",
            "prioritaet": "mittel",
        },
    )
    assert antwort.status_code == 302
    seite = owner_client.get(antwort["Location"]).content.decode()
    # Ungeplant: kein Freigeben-Knopf, stattdessen ein Satz, was zu tun ist.
    assert "Noch nicht geplant" in seite and "/aktion/freigeben/" not in seite


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
            "anlass": "konkretisiert",
        },
    )
    assert antwort.status_code == 302
    auftrag.refresh_from_db()
    assert auftrag.revision_nr == 2
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "/aktion/freigeben/" in seite and ">Freigeben</button>" in seite
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


def test_ergebnis_erledigt_ohne_abnahme_in_oberflaeche(
    owner_client, akteur, projekt, freigegeben, bindung
):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.ergebnis_einreichen(
        chat, freigegeben, 2, zusammenfassung="fertig", dateien="a@1", pruefungen="pytest: grün"
    )
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
    ).content.decode()
    assert "Erledigt" in seite and "pytest: grün" in seite
    # Abnahme-Knöpfe gibt es nicht; die frühere Prüfaktion ist weg.
    assert "Prüfen und abnehmen" not in seite and "Abnehmen" not in seite
    assert "ohne automatische Tests" not in seite
    antwort = owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": freigegeben.pk, "aktion": "pruefen"}),
        {"k0": "ERFUELLT", "a0": "", "k1": "ERFUELLT", "a1": "", "entscheidung": "ABGENOMMEN"},
    )
    assert antwort.status_code == 404
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.ERLEDIGT and Pruefung.objects.count() == 0
    board = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert 'id="spalte-erledigt"' in board and 'id="spalte-blockiert"' in board
    assert "Zur Prüfung" not in board and ">Prüfung<" not in board


def test_eigenes_ergebnis_ohne_tests_und_mit_fehlschlag(
    owner_client, owner, akteur, projekt, zweiter_mensch
):
    auftrag = arbeitspaket(akteur, projekt)
    dienst.auftrag_freigeben(akteur, auftrag)
    owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": auftrag.pk, "aktion": "selbst"})
    )
    auftrag.refresh_from_db()
    assert auftrag.bearbeiter_benutzer == owner
    antwort = owner_client.post(
        reverse("eigenes_ergebnis", kwargs={"auftrag_id": auftrag.pk}),
        {
            "zusammenfassung": "fertig",
            "dateien": "a@1",
            "pruefungen": "pytest: 3 bestanden",
            "fehlgeschlagen": "ruff: 2 Befunde",
            "luecken": "",
        },
    )
    assert antwort.status_code == 302
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.BLOCKIERT and auftrag.bearbeiter_benutzer == owner
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": auftrag.pk})
    ).content.decode()
    assert "Prüfung fehlgeschlagen: ruff: 2 Befunde" in seite
    assert ">Antworten</button>" in seite and "fehlgeschlagen</span> ruff" in seite
    wartet = owner_client.get(
        reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert "Wartet auf dich" in wartet and "Prüfung fehlgeschlagen" in wartet
    # Nachgebessert ohne automatische Tests: erledigt mit sichtbarem Vermerk.
    antwort = owner_client.post(
        reverse("eigenes_ergebnis", kwargs={"auftrag_id": auftrag.pk}),
        {"zusammenfassung": "behoben", "dateien": "a@2", "luecken": ""},
    )
    assert antwort.status_code == 302
    auftrag.refresh_from_db()
    assert auftrag.zustand == Auftrag.ERLEDIGT and not auftrag.hat_bearbeiter
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": auftrag.pk})
    ).content.decode()
    assert "ohne automatische Tests" in seite and "Prüfen und abnehmen" not in seite


def test_link_wird_einmal_angezeigt_und_ist_widerrufbar(owner_client):
    antwort = owner_client.post(reverse("zugang_neu"), {"programm": "anderes"})
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "nur jetzt angezeigt" in seite and "http://testserver/mcp/kipm_" in seite
    assert "Streamable HTTP" in seite
    nochmal = owner_client.get(reverse("verbindungen")).content.decode()
    assert "/mcp/kipm_" not in nochmal and "Welches Programm?" in nochmal
    from steuerung.models import ChatBindung

    bindung = ChatBindung.objects.get(name="Anderes KI-Programm")
    assert bindung.art == "pc" and bindung.projekt is None
    owner_client.post(reverse("bindung_widerrufen", kwargs={"bindung_id": bindung.pk}))
    bindung.refresh_from_db()
    assert bindung.widerrufen_am is not None


def test_nutzertexte_werden_nicht_ausgefuehrt(owner_client, owner, projekt, bindung):
    a = dienst.wunsch_erfassen(
        dienst.Akteur.aus_benutzer(owner),
        projekt,
        titel="<script>x()</script>",
        beschreibung="<img src=x onerror=alert(1)>",
    )
    dienst.arbeitsort_setzen(dienst.Akteur.aus_benutzer(owner), a, projekt.arbeitsorte.first())
    Meldung.objects.create(
        auftrag=a, revision_nr=1, art=Meldung.FORTSCHRITT, text="<b>fett</b>", akteur_name="Chat"
    )
    for url in (
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung}),
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}),
        reverse("projekt_verlauf", kwargs={"kennung": projekt.kennung}),
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
        reverse("projekt_verlauf", kwargs={"kennung": projekt.kennung}) + "?art=Auftrag"
    ).content.decode()
    assert "Freigegeben" in seite and "Projekt registriert" not in seite


def test_board_und_projektkarten_rendern(owner_client, projekt, freigegeben):
    url = reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    board = owner_client.get(url).content.decode()
    assert 'class="board"' in board and freigegeben.nummer in board and "Freigegeben" in board
    assert 'class="reiter"' in board
    liste = owner_client.get(url + "?ansicht=liste").content.decode()
    assert "<table" in liste and freigegeben.nummer in liste
    auswahl = owner_client.get(reverse("projekte")).content.decode()
    assert "projektwahl" in auswahl and projekt.name in auswahl
    assert "1 offen · <span>0 wartet auf dich</span> · Version unbekannt" in auswahl


def test_plus_listen_und_kuerzel_automatisch(owner_client):
    antwort = owner_client.post(
        reverse("projekt_neu"),
        {
            "name": "Nexus Projekt",
            "beschreibung": "",
            "kennung": "",
            "module_eintrag": ["Core", "", "Standards"],
        },
    )
    assert antwort.status_code == 302 and antwort["Location"].endswith("/projekt/nexus-projekt/")
    seite = owner_client.get(
        reverse("projekt_einstellungen", kwargs={"kennung": "nexus-projekt"})
    ).content.decode()
    assert "Core" in seite and "Standards" in seite
    owner_client.post(
        reverse("modul_neu", kwargs={"kennung": "nexus-projekt"}), {"modul-name": "Web Oberfläche"}
    )
    owner_client.post(
        reverse("arbeitsort_neu", kwargs={"kennung": "nexus-projekt"}),
        {"ort-name": "Repository Nexus", "ort-rechner": "PC", "ort-pfad": ""},
    )
    from steuerung.models import Arbeitsort, Modul

    assert Modul.objects.get(name="Web Oberfläche").kennung == "web-oberflache"
    assert Arbeitsort.objects.get(kennung="repository-nexus").name == "Repository Nexus"


def test_eine_seite_ki_verbinden(owner_client, settings):
    seite = owner_client.get(reverse("verbindungen")).content.decode()
    assert "Welches Programm?" in seite and "Claude" in seite and "ChatGPT" in seite
    for alt in ("Kommandozeile", "Wo gespeichert", "HTTP-Schnittstelle", "certutil"):
        assert alt not in seite
    assert owner_client.post(reverse("zugang_neu"), {"programm": "gibts-nicht"}).status_code == 302

    antwort = owner_client.post(reverse("zugang_neu"), {"programm": "claude"})
    seite = owner_client.get(antwort["Location"]).content.decode()
    zeile = html.unescape(seite.split('<pre id="zeile">')[1].split("</pre>")[0])
    assert zeile.startswith('cmd /c "curl.exe -fsSk -o %TEMP%')
    assert "certutil -user -addstore Root" in zeile
    assert (
        "(claude mcp remove kipm --scope user >nul 2>&1 & claude mcp add --transport http" in zeile
    )
    assert "http://testserver/mcp/kipm_" in zeile
    assert "Rechtsklick" in seite and "Zertifikat installieren" in seite
    from steuerung.models import ChatBindung

    pc = ChatBindung.objects.get(name="Claude Desktop")
    pruefen = owner_client.get(
        reverse("zugang_pruefen", kwargs={"bindung_id": pc.pk})
    ).content.decode()
    assert "noch nie" in pruefen

    antwort = owner_client.post(reverse("zugang_neu"), {"programm": "codex"})
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "codex mcp add kipm --url http://testserver/mcp/kipm_" in seite

    settings.KIPM_OEFFENTLICHE_URL = "https://kipm.example.net"
    antwort = owner_client.post(reverse("zugang_neu"), {"programm": "claude"})
    seite = owner_client.get(antwort["Location"]).content.decode()
    assert "https://kipm.example.net/mcp/kipm_" in seite and "certutil" not in seite


def test_wartet_auf_dich_im_projekt(owner_client, akteur, projekt, projekt_b, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.wunsch_erfassen(chat, projekt, titel="Idee vom Chat", beschreibung="bitte prüfen")
    dienst.wunsch_erfassen(
        dienst.Akteur.aus_bindung(dienst.bindung_anlegen(akteur, projekt_b, name="Chat B")[0]),
        projekt_b,
        titel="Fremdes Thema",
        beschreibung="B",
    )
    geplant = arbeitspaket(akteur, projekt, titel="Geplant vom Chat", umfang="docs/**")
    dienst.auftrag_uebernehmen(chat, freigegeben, 2)
    dienst.rueckfrage_melden(chat, freigegeben, 2, "Welche Sprache für den Hinweis?")
    url = reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
    seite = owner_client.get(url).content.decode()
    teil = seite.split("Wartet auf dich")[1].split("</section>")[0]
    # Ungeplante Idee wartet auf einen Chat, nicht auf Fred; die geplante trägt „Freigeben“.
    assert "Idee vom Chat" not in teil and "Geplant vom Chat" in teil
    assert f"/auftraege/{geplant.pk}/aktion/freigeben/" in teil
    assert "Rückfrage" in teil and "Welche Sprache" in teil and "Fremdes Thema" not in seite
    assert teil.index("Welche Sprache") < teil.index("Geplant vom Chat")
    antwort = owner_client.post(
        reverse("antwort", kwargs={"auftrag_id": freigegeben.pk}),
        {"text": "Deutsch", "zurueck": url},
    )
    assert antwort.status_code == 302 and antwort["Location"] == url
    freigegeben.refresh_from_db()
    assert freigegeben.zustand == Auftrag.IN_ARBEIT
    assert (
        "Welche Sprache"
        not in owner_client.get(url)
        .content.decode()
        .split("Wartet auf dich")[-1]
        .split("</section>")[0]
    )


def test_datei_link_mit_repository(akteur, projekt):
    from steuerung.templatetags.steuerung_extras import datei_link

    ort = projekt.arbeitsorte.first()
    assert str(datei_link("src/a.py@9c2e7b1", ort)) == "<code>src/a.py@9c2e7b1</code>"
    ort.repository = "https://github.com/FredHeidinger/nexus/"
    html = str(datei_link("src/a.py@9c2e7b1", ort))
    assert "https://github.com/FredHeidinger/nexus/blob/9c2e7b1/src/a.py" in html
    assert "https://github.com/FredHeidinger/nexus/commit/9c2e7b1" in html
    assert str(datei_link("ohne-commit", ort)) == "<code>ohne-commit</code>"


def test_neu_menue_issues_und_filter(owner_client, akteur, projekt):
    auswahl = owner_client.get(reverse("projekte")).content.decode()
    assert "+ Neu" in auswahl and "<strong>Projekt</strong>" in auswahl
    assert "<strong>Funktion</strong>" not in auswahl
    projektseite = owner_client.get(
        reverse("projekt_detail", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    for wort in ("Auftrag", "Issue", "Text", "Link"):
        assert f"<strong>{wort}</strong>" in projektseite
    for wort in ("Funktion", "Unterlage", "Entscheidung", "Release"):
        assert f"<strong>{wort}</strong>" not in projektseite
    assert f"art=issue&amp;projekt={projekt.kennung}" in projektseite

    formular = owner_client.get(reverse("wunsch_neu") + "?art=issue").content.decode()
    assert "Issue anlegen" in formular
    antwort = owner_client.post(
        reverse("wunsch_neu"),
        {
            "typ": "issue",
            "projekt": projekt.pk,
            "titel": "Absturz beim Speichern",
            "beschreibung": "Seite bleibt weiß.",
            "prioritaet": "hoch",
        },
    )
    assert antwort.status_code == 302
    assert Auftrag.objects.get(titel="Absturz beim Speichern").typ == "issue"
    dienst.wunsch_erfassen(akteur, projekt, titel="Export nach CSV", beschreibung="B")

    url = reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    board = owner_client.get(url).content.decode()
    assert '<span class="issue">Issue</span>' in board
    issues = owner_client.get(url + "?ansicht=liste&art=issue").content.decode()
    assert "Absturz beim Speichern" in issues and "Export nach CSV" not in issues
    auftraege = owner_client.get(url + "?art=auftrag").content.decode()
    assert "Export nach CSV" in auftraege.split('class="board"')[1]
    assert "Absturz beim Speichern" not in auftraege.split('class="board"')[1]


def test_module_in_einstellungen_und_filter(owner_client, akteur, projekt):
    kern = projekt.module.get(kennung="kern")
    dienst.wunsch_erfassen(
        akteur, projekt, titel="Absturz", beschreibung="B", modul=kern, typ="issue"
    )
    dienst.wunsch_erfassen(akteur, projekt, titel="Export", beschreibung="B", modul=kern)
    dienst.wunsch_erfassen(akteur, projekt, titel="Ohne Modul", beschreibung="B")
    url = reverse("projekt_einstellungen", kwargs={"kennung": projekt.kennung})
    seite = owner_client.get(url).content.decode()
    assert 'id="module"' in seite and "Kern" in seite
    ueberblick = owner_client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    assert "Alle Aufträge (3 offen)" in ueberblick.content.decode()

    owner_client.post(
        reverse("modul_aendern", kwargs={"kennung": projekt.kennung, "modul": "kern"}),
        {"name": "Kernfunktionen"},
    )
    kern.refresh_from_db()
    assert kern.name == "Kernfunktionen" and kern.kennung == "kern"
    antwort = owner_client.post(
        reverse("modul_archivieren", kwargs={"kennung": projekt.kennung, "modul": "kern"}),
        {"aktiv": "0"},
    )
    assert antwort["Location"].endswith("#module")
    kern.refresh_from_db()
    assert not kern.aktiv
    assert "Archiv: 1 Module" in owner_client.get(url).content.decode()

    liste = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
        + "?ansicht=liste&modul=kern"
    ).content.decode()
    assert "Absturz" in liste and "Ohne Modul" not in liste


def test_assistent_darf_module_pflegen(akteur, projekt):
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Claude Desktop")
    ki = dienst.Akteur.aus_bindung(pc)
    modul = projekt.module.get(kennung="kern")
    dienst.modul_aendern(ki, modul, name="Kern neu")
    dienst.modul_archivieren(ki, modul, aktiv=False)
    modul.refresh_from_db()
    assert modul.name == "Kern neu" and not modul.aktiv

"""Gestaltung D (mit Fred festgelegt, 14.11.2026): FHEngineering-Farben und -Schriften, Kopfleiste
mit Logo statt Seitenleiste, Startseite nur mit den Projekten, Board mit Signalleiste, Überblick in
zwei Spalten. Funktionen bleiben erhalten."""

import re
from pathlib import Path

from django.urls import reverse

from steuerung import dienst
from tests.conftest import arbeitspaket
from tests.test_klickwege import _Seite

STATIC = Path(__file__).resolve().parents[1] / "steuerung" / "static" / "steuerung"
VORLAGEN = Path(__file__).resolve().parents[1] / "steuerung" / "templates" / "steuerung"


def _leiste(html: str) -> str:
    return html.split('<nav class="hauptleiste"')[1].split("</nav>")[0]


def _in_arbeit(akteur, projekt, bindung, titel="Suche beschleunigen", stand="Index steht"):
    a = arbeitspaket(akteur, projekt, titel=titel, umfang=f"{titel[:4].lower()}/**")
    dienst.auftrag_freigeben(akteur, a)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    if stand:
        dienst.fortschritt_melden(chat, a, a.revision_nr, stand)
    a.refresh_from_db()
    return a


# --- Farben, Schriften, Logo ----------------------------------------------------------------


def test_stil_nutzt_die_ci_farben_und_nicht_mehr_neongruen():
    stil = (STATIC / "stil.css").read_text(encoding="utf-8")
    assert "#102a43" in stil.lower() and "#c6ff00" in stil.lower()
    assert "#39ff88" not in stil.lower()


def test_schriften_liegen_auf_dem_server_kein_fremder_dienst():
    stil = (STATIC / "stil.css").read_text(encoding="utf-8")
    for datei in ("inter-latin-wght-normal.woff2", "space-grotesk-latin-wght-normal.woff2"):
        assert (STATIC / "schriften" / datei).is_file(), datei
        assert f"schriften/{datei}" in stil, datei
    assert "Space Grotesk" in stil and "Inter" in stil
    grund = (VORLAGEN / "grund.html").read_text(encoding="utf-8")
    assert not re.search(r"https?://", grund), "Grundvorlage lädt etwas von außen"


def test_kopfleiste_mit_logo_vier_bereichen_und_kontozugang(owner_client, projekt):
    html = owner_client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    leiste = _leiste(html.content.decode())
    assert 'alt="FHEngineering"' in leiste and "fhe-logo.png" in leiste
    links = [(h, t) for h, t in _Seite(leiste).links]
    bereiche = [t for h, t in links if h.startswith("/projekt/")]
    assert bereiche[:4] == ["Überblick", "Aufträge", "Wissen", "Verlauf"]
    texte = " ".join(t for _, t in links)
    assert "Alle Aufträge" in texte and "Konto & KI verbinden" in texte
    assert (reverse("projekte"), "Projekt wechseln Synthetisches A") in links


# --- Startseite: Wartet auf dich und Läuft gerade -------------------------------------------


def test_start_zeigt_nur_projekte_keine_tagesuebersicht(owner_client, akteur, projekt, bindung):
    """Fred (14.11.2026): „die tagesübersicht brauche ich nicht, ich will nur projekte sehen“."""
    arbeitspaket(akteur, projekt, titel="Wartet auf Freigabe", umfang="w/**")
    _in_arbeit(akteur, projekt, bindung, stand="Index steht")
    html = owner_client.get(reverse("start")).content.decode()
    inhalt = html.split('<main id="inhalt">')[1]
    assert 'class="projektwahl"' in inhalt and "Synthetisches A" in inhalt
    assert "<h1>Projekte</h1>" in inhalt
    for weg in (
        'id="wartet"',
        'id="laeuft-gerade"',
        "Läuft gerade",
        "Guten ",
        "Wartet auf Freigabe",
    ):
        assert weg not in html, weg
    assert 'class="band' not in html


def test_start_projektkarten_bleiben_mit_zahlenzeile(owner_client, akteur, projekt):
    arbeitspaket(akteur, projekt)
    html = owner_client.get(reverse("start")).content.decode()
    karten = html.split('class="projektwahl"')[1]
    assert "Synthetisches A" in karten and "1 offen" in karten and "1 wartet auf dich" in karten


# --- Board und Überblick --------------------------------------------------------------------


def test_board_signalleiste_springt_zu_den_spalten(owner_client, akteur, projekt):
    arbeitspaket(akteur, projekt, titel="Eins", umfang="e/**")
    html = owner_client.get(reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung}))
    html = html.content.decode()
    leiste = html.split('class="zaehlleiste signalleiste"')[1].split("</nav>")[0]
    assert 'href="#spalte-entwurf"' in leiste
    assert 'id="spalte-entwurf"' in html


def test_ueberblick_zwei_spalten_und_kennzahlen_im_band(owner_client, akteur, projekt):
    arbeitspaket(akteur, projekt)
    html = owner_client.get(reverse("projekt_detail", kwargs={"kennung": projekt.kennung}))
    html = html.content.decode()
    band = html.split('class="band"')[1].split("</section>")[0]
    assert "wartet auf dich" in band and "offen" in band
    links = html.split('class="spalte-links"')[1].split('class="spalte-rechts"')[0]
    rechts = html.split('class="spalte-rechts"')[1]
    for kennung in ('id="h-wartet"', 'id="h-naechstes"', 'id="h-erledigt"'):
        assert kennung in links, kennung
    for kennung in ('id="h-version"', 'id="h-meilensteine"', 'id="h-beschreibung"'):
        assert kennung in rechts, kennung

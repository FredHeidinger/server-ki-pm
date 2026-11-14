"""Gestaltung D (mit Fred festgelegt, 14.11.2026): FHEngineering-Farben und -Schriften, Kopfleiste
mit Logo statt Seitenleiste, Startseite mit „Wartet auf dich“ über alle Projekte und „Läuft
gerade“, Board mit Signalleiste, Überblick in zwei Spalten. Funktionen bleiben erhalten."""

import re
from pathlib import Path

from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag
from tests.conftest import arbeitspaket
from tests.test_klickwege import _Klicker, _Seite

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


def test_start_zeigt_wartet_auf_dich_aus_allen_aktiven_projekten(
    owner_client, akteur, projekt, projekt_b
):
    a = arbeitspaket(akteur, projekt, titel="Export als CSV")
    b = arbeitspaket(akteur, projekt_b, titel="Belege zuordnen", umfang="b/**")
    html = owner_client.get(reverse("start")).content.decode()
    wartet = html.split('id="wartet"')[1].split("</section>")[0]
    assert "Export als CSV" in wartet and "Belege zuordnen" in wartet
    aktionen = {f["action"] for f in _Seite(wartet).formulare}
    for x in (a, b):
        assert (
            reverse("auftrag_aktion", kwargs={"auftrag_id": x.pk, "aktion": "freigeben"})
            in aktionen
        )
    assert "Synthetisches A" in wartet and "Synthetisches B" in wartet  # Projekt je Eintrag


def test_start_wartet_ohne_archivierte_projekte(owner_client, akteur, projekt, projekt_b):
    arbeitspaket(akteur, projekt_b, titel="Im Archiv", umfang="b/**")
    dienst.projekt_archivieren(akteur, projekt_b, aktiv=False)
    html = owner_client.get(reverse("start")).content.decode()
    assert "Im Archiv" not in html.split('id="wartet"')[1].split("</section>")[0]


def test_klickweg_freigeben_direkt_auf_der_startseite(owner_client, akteur, projekt):
    entwurf = arbeitspaket(akteur, projekt)
    f = _Klicker(owner_client)
    f.knopf(
        reverse("auftrag_aktion", kwargs={"auftrag_id": entwurf.pk, "aktion": "freigeben"}),
        "Freigeben",
    )
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.FREIGEGEBEN and f.klicks == 1
    assert f.url == reverse("start")


def test_laeuft_gerade_nennt_bearbeiter_und_letzten_stand(
    akteur, projekt, projekt_b, bindung, bindung_b
):
    a = _in_arbeit(akteur, projekt, bindung, stand="Index steht")
    b = _in_arbeit(akteur, projekt_b, bindung_b, titel="Belege zuordnen", stand="")
    zeilen = dienst.laeuft_gerade()
    assert [z["a"].pk for z in zeilen] == [a.pk, b.pk] or [z["a"].pk for z in zeilen] == [
        b.pk,
        a.pk,
    ]
    nach_pk = {z["a"].pk: z for z in zeilen}
    assert nach_pk[a.pk]["stand"].text == "Index steht"
    assert nach_pk[b.pk]["stand"] is None


def test_laeuft_gerade_ohne_archiv_entwuerfe_und_erledigtes(
    akteur, projekt, projekt_b, bindung, bindung_b
):
    arbeitspaket(akteur, projekt, titel="Nur Entwurf", umfang="e/**")
    _in_arbeit(akteur, projekt_b, bindung_b, titel="Archivprojekt")
    Auftrag.objects.filter(projekt=projekt_b).update(zustand=Auftrag.IN_ARBEIT)
    projekt_b.aktiv = False
    projekt_b.save(update_fields=["aktiv"])
    assert dienst.laeuft_gerade() == []


def test_start_zeigt_laeuft_gerade(owner_client, akteur, projekt, bindung):
    _in_arbeit(akteur, projekt, bindung, stand="Index steht, Messung läuft")
    html = owner_client.get(reverse("start")).content.decode()
    laeuft = html.split('id="laeuft-gerade"')[1].split("</section>")[0]
    assert "Suche beschleunigen" in laeuft and "Chat A" in laeuft
    assert "Index steht, Messung läuft" in laeuft


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

"""Ansicht „Alle Aufträge“: Board und Liste über alle aktiven Projekte, Projekte per Schaltfläche ein-
und ausschaltbar, Auswahl bleibt in der Sitzung; dazu das Board mit eigenem Scrollbereich je Spalte."""

import re

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag, Projekt
from tests.conftest import arbeitspaket
from tests.test_klickwege import _Klicker, _Seite

pytestmark = pytest.mark.django_db


def _url(zusatz=""):
    return reverse("alle_auftraege") + zusatz


def _seite(client, zusatz=""):
    antwort = client.get(_url(zusatz))
    assert antwort.status_code == 200
    return antwort.content.decode("utf-8")


def _chips(html):
    """Schaltflächen der Projektauswahl als {Name: (an, href)}."""
    teil = html.split('class="projektchips"')[1].split("</nav>")[0]
    chips = {}
    for m in re.finditer(r'<a class="chip (an|aus)" href="([^"]*)"[^>]*>(.*?)</a>', teil, re.S):
        text = re.sub(r"<[^>]+>", " ", m.group(3))
        chips[" ".join(text.split())] = (m.group(1) == "an", m.group(2).replace("&amp;", "&"))
    return chips


@pytest.fixture
def zwei(akteur, projekt, projekt_b):
    a = arbeitspaket(akteur, projekt, titel="Auftrag in A", umfang="a/**")
    b = arbeitspaket(akteur, projekt_b, titel="Auftrag in B", umfang="b/**")
    return a, b


# --- Ansicht und Filter -------------------------------------------------------------------------


def test_zeigt_auftraege_aller_aktiven_projekte(owner_client, zwei):
    html = _seite(owner_client)
    assert "Auftrag in A" in html and "Auftrag in B" in html
    assert 'class="board"' in html


def test_archivierte_projekte_fehlen(owner_client, zwei, projekt_b):
    Projekt.objects.filter(pk=projekt_b.pk).update(aktiv=False)
    html = _seite(owner_client)
    assert "Auftrag in A" in html and "Auftrag in B" not in html
    assert projekt_b.name not in html.split('class="projektchips"')[1].split("</nav>")[0]


def test_auswahl_filtert_und_markiert_mit_wort(owner_client, zwei, projekt, projekt_b):
    html = _seite(owner_client, "?p=synth-a")
    assert "Auftrag in A" in html and "Auftrag in B" not in html
    chips = _chips(html)
    assert chips[f"✓ {projekt.name} 1"][0] is True
    assert chips[f"{projekt_b.name} 1"][0] is False


def test_chip_schaltet_um_ohne_javascript(owner_client, akteur, zwei, projekt, projekt_b):
    dienst.projekt_registrieren(akteur, kennung="synth-c", name="Synthetisches C")
    chips = _chips(_seite(owner_client, "?p=synth-a"))
    zu_b = chips[f"{projekt_b.name} 1"][1]
    assert "p=synth-a" in zu_b and "p=synth-b" in zu_b and "synth-c" not in zu_b
    weg_a = chips[f"✓ {projekt.name} 1"][1]
    assert "p=-" in weg_a


def test_sind_alle_an_heisst_der_link_alle(owner_client, zwei, projekt_b):
    chips = _chips(_seite(owner_client, "?p=synth-a"))
    assert chips[f"{projekt_b.name} 1"][1].endswith("p=alle")


def test_alle_und_keine(owner_client, zwei):
    html = _seite(owner_client, "?p=-")
    assert "Auftrag in A" not in html and "Auftrag in B" not in html
    assert "Kein Projekt ausgewählt" in html
    links = dict((t, h) for h, t in _Seite(html).links)
    assert "p=alle" in links["Alle"].replace("&amp;", "&")
    html = _seite(owner_client, "?p=alle")
    assert "Auftrag in A" in html and "Auftrag in B" in html


def test_auswahl_bleibt_in_der_sitzung(owner_client, zwei):
    _seite(owner_client, "?p=synth-b")
    html = _seite(owner_client)
    assert "Auftrag in B" in html and "Auftrag in A" not in html
    _seite(owner_client, "?p=alle")
    assert "Auftrag in A" in _seite(owner_client)


def test_unbekannte_projekte_in_der_auswahl_werden_ignoriert(owner_client, zwei):
    html = _seite(owner_client, "?p=gibt-es-nicht&p=synth-a")
    assert "Auftrag in A" in html and "Auftrag in B" not in html


def test_art_und_ansicht_bleiben_beim_umschalten_erhalten(owner_client, zwei, projekt_b):
    chips = _chips(_seite(owner_client, "?ansicht=liste&art=auftrag&p=synth-a"))
    href = chips[f"{projekt_b.name} 1"][1]
    assert "ansicht=liste" in href and "art=auftrag" in href


def test_nur_angemeldete(client, zwei):
    antwort = client.get(_url())
    assert antwort.status_code == 302 and "/anmelden/" in antwort["Location"]


# --- Karten, Liste, Seitenleiste ----------------------------------------------------------------


def test_karte_nennt_das_projekt(owner_client, zwei, projekt):
    html = _seite(owner_client)
    karte = (
        html.split("Auftrag in A")[0].rsplit("<article", 1)[1]
        + html.split("Auftrag in A")[1].split("</article>")[0]
    )
    assert projekt.name in karte


def test_liste_hat_projektspalte(owner_client, zwei, projekt):
    html = _seite(owner_client, "?ansicht=liste")
    assert '<th scope="col">Projekt</th>' in html and projekt.name in html
    assert "Auftrag in A" in html and "Auftrag in B" in html


def test_seitenleiste_hat_alle_auftraege_ueberall(owner_client, zwei, projekt):
    for url in (reverse("start"), reverse("projekt_detail", kwargs={"kennung": projekt.kennung})):
        html = owner_client.get(url).content.decode()
        leiste = html.split('<nav class="seitenleiste"')[1].split("</nav>")[0]
        links = _Seite(leiste).links
        assert (reverse("alle_auftraege"), "Alle Aufträge") in [(h, t.strip()) for h, t in links]


def test_seitenleiste_bleibt_ohne_projekt_auf_der_neuen_seite(owner_client, zwei):
    html = _seite(owner_client, "?p=synth-a")
    leiste = html.split('<nav class="seitenleiste"')[1].split("</nav>")[0]
    assert "Projekt wechseln" not in leiste and 'aria-current="page"' in leiste


def test_klickweg_auftrag_finden_ueber_alle_projekte(owner_client, zwei):
    a, b = zwei
    f = _Klicker(owner_client)
    f.link(reverse("alle_auftraege"), "Alle Aufträge")
    ziel = reverse("auftrag_detail", kwargs={"auftrag_id": b.pk})
    assert any(h == ziel and "Auftrag in B" in t for h, t in _Seite(f.html).links)
    assert f.klicks == 1


def test_verschieben_auswahl_funktioniert_hier_auch(owner_client, zwei):
    html = _seite(owner_client)
    assert 'form="verschiebe-auswahl"' in html and 'id="verschiebe-auswahl"' in html


# --- Board: eigener Scrollbereich je Spalte, Zählleiste -----------------------------------------


def test_jede_spalte_hat_einen_eigenen_scrollbereich(owner_client, akteur, projekt):
    html = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert html.count('class="spaltenkarten"') == 6


def test_alle_karten_bleiben_sichtbar_und_die_zahl_stimmt(owner_client, akteur, projekt):
    for i in range(12):
        arbeitspaket(akteur, projekt, titel=f"Viele {i:02d}", umfang=f"v{i}/**")
    html = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    spalte = html.split('id="spalte-entwurf"')[1].split("</section>")[0]
    assert all(f"Viele {i:02d}" in spalte for i in range(12))
    assert '<span class="anzahl">12</span>' in spalte
    assert "details" not in spalte.split("</h3>")[1].split('class="karte')[0]


def test_zaehlleiste_nennt_die_spalten(owner_client, akteur, projekt):
    arbeitspaket(akteur, projekt, titel="Eins", umfang="e/**")
    zwei_ = arbeitspaket(akteur, projekt, titel="Zwei", umfang="z/**")
    dienst.auftrag_freigeben(akteur, zwei_)
    html = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    leiste = html.split('class="zaehlleiste"')[1].split("</p>")[0]
    assert "1 Entwurf" in leiste and "1 Freigegeben" in leiste and "0 Blockiert" not in leiste
    assert Auftrag.objects.count() == 2


def test_modul_steht_in_der_fusszeile_der_karte(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt, titel="Mit Modul", umfang="m/**")
    Auftrag.objects.filter(pk=a.pk).update(modul=projekt.module.first())
    html = owner_client.get(
        reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})
    ).content.decode()
    assert "kartenmeta" not in html
    fuss = html.split("Mit Modul")[1].split('class="kartenfuss"')[1].split("</div>")[0]
    assert projekt.module.first().name in fuss

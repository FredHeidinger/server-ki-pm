"""Auftrag als ganze Seite: „Zurück zu den Aufträgen“, Kopf mit genau einem passenden Knopf, dann die
Abschnitte Auftrag, Ergebnis, Verlauf; Bearbeiten und Abbrechen unter „Weitere Aktionen“."""

from django.urls import reverse

from steuerung import dienst
from tests.conftest import arbeitspaket


def _seite(client, auftrag):
    return client.get(reverse("auftrag_detail", kwargs={"auftrag_id": auftrag.pk})).content.decode()


def _kopf(html):
    return html.split('class="box auftragskopf"')[1].split("</header>")[0]


def test_ganze_seite_mit_abschnitten_und_zurueck(owner_client, akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    html = _seite(owner_client, a)
    assert "← Zurück zu den Aufträgen" in html
    assert f'href="{reverse("projekt_auftraege", kwargs={"kennung": projekt.kennung})}"' in html
    reihenfolge = [html.index(s) for s in ('id="auftrag"', 'id="ergebnis"', 'id="verlauf"')]
    assert reihenfolge == sorted(reihenfolge) and html.index("auftragskopf") < reihenfolge[0]
    for feld in ("Ziel", "Nicht-Ziele", "Arbeitsort", "Schreibumfang", "Kriterien"):
        assert f"<dt>{feld}</dt>" in html
    # Kein Seitenpanel mehr, keine Reiter, keine Abnahme.
    for alt in ('id="panel"', "data-panel", "data-reiter", "Abnehmen", "Prüfen und abnehmen"):
        assert alt not in html


def test_kopf_hat_genau_einen_passenden_knopf(owner_client, akteur, projekt, bindung):
    a = arbeitspaket(akteur, projekt)
    kopf = _kopf(_seite(owner_client, a))
    vorne = kopf.split('<details class="weitere-aktionen">')[0]
    assert vorne.count("<button") == 1 and ">Freigeben</button>" in vorne
    weitere = kopf.split('<details class="weitere-aktionen">')[1]
    assert ">Bearbeiten</a>" in weitere and "Abbrechen" in weitere
    assert "Planen" not in kopf

    dienst.auftrag_freigeben(akteur, a)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.rueckfrage_melden(chat, a, a.revision_nr, "Welche Sprache?")
    kopf = _kopf(_seite(owner_client, a))
    vorne = kopf.split('<details class="weitere-aktionen">')[0]
    assert vorne.count("<button") == 1 and ">Antworten</button>" in vorne
    assert "Welche Sprache?" in vorne


def test_ohne_moeglichen_knopf_ein_satz(owner_client, akteur, projekt, freigegeben):
    wunsch = dienst.wunsch_erfassen(akteur, projekt, titel="Nur Idee", beschreibung="b")
    kopf = _kopf(_seite(owner_client, wunsch))
    vorne = kopf.split('<details class="weitere-aktionen">')[0]
    assert "<button" not in vorne and f"Plane {wunsch.nummer}" in vorne
    kopf = _kopf(_seite(owner_client, freigegeben))
    assert "Für dich ist nichts zu tun" in kopf
    assert kopf.split('<details class="weitere-aktionen">')[0].count("<button") == 0


def test_ueberschneidung_nennt_grund_und_ausweg(owner_client, akteur, projekt, freigegeben):
    zweiter = arbeitspaket(akteur, projekt, titel="Zweiter", umfang="src/auth/x.py")
    html = _seite(owner_client, zweiter)
    assert "Freigeben noch nicht möglich" in _kopf(html)
    freigabe = html.split('id="freigabe"')[1].split("</section>")[0]
    assert f"Überschneidung mit {freigegeben.nummer}" in freigabe
    assert f"{freigegeben.nummer} als Vorgänger eintragen" in freigabe


def test_ergebnis_ohne_automatische_tests(owner_client, akteur, projekt, freigegeben, bindung):
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, freigegeben, freigegeben.revision_nr)
    dienst.ergebnis_einreichen(
        chat, freigegeben, freigegeben.revision_nr, zusammenfassung="fertig", dateien="a@1"
    )
    html = _seite(owner_client, freigegeben)
    ergebnis = html.split('id="ergebnis"')[1].split("</section>")[0]
    assert "ohne automatische Tests" in ergebnis and "<code>a@1</code>" in ergebnis
    assert "Erledigt" in _kopf(html)

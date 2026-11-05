"""Oberfläche nach der Tabelle „Oberfläche (mit Fred festgelegt)“: Klickwege von der Startseite,
derselbe Hauptknopf an allen drei Stellen, Projektkarten, vier Bereiche, Überblick, Board, Verlauf.

Ein Klickweg zählt jeden Link und jeden Knopf; Tippen in ein Feld ist kein Klick. Jeder Schritt
muss auf der gerade angezeigten Seite stehen, sonst gilt der Weg als nicht erreichbar."""

from html.parser import HTMLParser

from django.urls import reverse

from steuerung import dienst
from steuerung.models import Auftrag
from tests.conftest import arbeitspaket


class _Seite(HTMLParser):
    """Links und Formulare (Ziel, Knopftexte, Felder) einer Seite."""

    def __init__(self, html: str):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.formulare: list[dict] = []
        self._link: list | None = None
        self._form: dict | None = None
        self._knopf: list | None = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "href" in a:
            self._link = [a["href"], ""]
        elif tag == "form":
            self._form = {
                "action": a.get("action", ""),
                "method": a.get("method", "get"),
                "knoepfe": [],
                "werte": {},
                "felder": {},
                "text": [],
            }
        elif tag == "input" and self._form is not None and a.get("name"):
            self._form["felder"][a["name"]] = (a.get("type", "text"), a.get("value", ""))
        elif tag == "button" and self._form is not None:
            self._knopf = ["", a.get("name"), a.get("value", "")]

    def handle_data(self, data):
        if self._link is not None:
            self._link[1] += " " + data
        if self._knopf is not None:
            self._knopf[0] += " " + data

    def handle_endtag(self, tag):
        if tag == "a" and self._link is not None:
            self.links.append((self._link[0], " ".join(self._link[1].split())))
            self._link = None
        elif tag == "button" and self._knopf is not None and self._form is not None:
            text = " ".join(self._knopf[0].split())
            self._form["knoepfe"].append(text)
            if self._knopf[1]:
                self._form["werte"][text] = (self._knopf[1], self._knopf[2])
            self._knopf = None
        elif tag == "form" and self._form is not None:
            self.formulare.append(self._form)
            self._form = None


class _Klicker:
    """Bedient die Oberfläche wie Fred: Startseite öffnen, dann nur Klicks auf Sichtbares."""

    def __init__(self, client):
        self.client = client
        self.klicks = 0
        self.url = reverse("start")
        self.html = client.get(self.url).content.decode()

    def link(self, href: str, text: str = "") -> None:
        seite = _Seite(self.html)
        treffer = [h for h, t in seite.links if h == href and text in t]
        assert treffer, f"Link {href} ({text}) fehlt auf {self.url}"
        self.klicks += 1
        self.url = href
        self.html = self.client.get(href, follow=True).content.decode()

    def knopf(self, action: str, knopftext: str, **eingaben) -> None:
        seite = _Seite(self.html)
        formulare = [f for f in seite.formulare if f["action"] == action]
        assert formulare, f"Formular {action} fehlt auf {self.url}"
        form = formulare[0]
        assert knopftext in form["knoepfe"], (form["knoepfe"], knopftext)
        daten = {n: w for n, (typ, w) in form["felder"].items() if typ == "hidden"}
        if knopftext in form["werte"]:
            name, wert = form["werte"][knopftext]
            daten[name] = wert
        for name, wert in eingaben.items():
            assert name in form["felder"], f"Feld {name} fehlt in {action}"
            daten[name] = wert
        self.klicks += 1
        antwort = self.client.post(action, daten, follow=True)
        self.url = antwort.redirect_chain[-1][0] if antwort.redirect_chain else action
        self.html = antwort.content.decode()


def _projekt_url(projekt, name="projekt_detail"):
    return reverse(name, kwargs={"kennung": projekt.kennung})


def _aktion(auftrag, aktion="freigeben"):
    return reverse("auftrag_aktion", kwargs={"auftrag_id": auftrag.pk, "aktion": aktion})


def _blockiert(akteur, projekt, bindung, frage="Welche Sprache für den Hinweis?"):
    a = arbeitspaket(akteur, projekt, titel="Mit Rückfrage", umfang="lib/**")
    dienst.auftrag_freigeben(akteur, a)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    dienst.rueckfrage_melden(chat, a, a.revision_nr, frage)
    a.refresh_from_db()
    assert a.zustand == Auftrag.BLOCKIERT
    return a


# --- Klickwege: jede menschliche Aufgabe in höchstens zwei Klicks -------------------------


def test_klickweg_entwurf_freigeben(owner_client, akteur, projekt):
    entwurf = arbeitspaket(akteur, projekt)
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    f.knopf(_aktion(entwurf), "Freigeben")
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.FREIGEGEBEN and f.klicks == 2


def test_klickweg_frage_beantworten(owner_client, akteur, projekt, bindung):
    a = _blockiert(akteur, projekt, bindung)
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    assert "Welche Sprache für den Hinweis?" in f.html
    f.knopf(reverse("antwort", kwargs={"auftrag_id": a.pk}), "Antworten", text="Deutsch")
    a.refresh_from_db()
    assert a.zustand == Auftrag.IN_ARBEIT and f.klicks == 2
    assert a.meldungen.filter(art="ANTWORT", text="Deutsch").exists()


def test_klickweg_auftrag_finden(owner_client, akteur, projekt, freigegeben):
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    f.link(_projekt_url(projekt, "projekt_auftraege"), "Aufträge")
    seite = _Seite(f.html)
    ziel = reverse("auftrag_detail", kwargs={"auftrag_id": freigegeben.pk})
    assert any(h == ziel and freigegeben.titel in t for h, t in seite.links)
    assert f.klicks == 2


def test_klickweg_ki_verbinden(owner_client, projekt):
    f = _Klicker(owner_client)
    f.link(reverse("verbindungen"), "Konto & KI verbinden")
    f.knopf(reverse("zugang_neu"), "Claude Claude-Desktop-App und Claude Code")
    assert "claude mcp add --transport http" in f.html and f.klicks == 2


def test_klickweg_ki_verbinden_auch_aus_dem_projekt(owner_client, projekt):
    f = _Klicker(owner_client)
    f.link(_projekt_url(projekt))
    f.link(reverse("verbindungen"), "Konto & KI verbinden")
    assert "Welches Programm?" in f.html and f.klicks == 2


# --- Derselbe Hauptknopf in „Wartet auf dich“, auf der Karte und im Auftragskopf ----------


def _knoepfe_fuer(html, action):
    return [k for f in _Seite(html).formulare if f["action"] == action for k in f["knoepfe"]]


def test_freigeben_an_allen_drei_stellen_gleich(owner_client, akteur, projekt):
    entwurf = arbeitspaket(akteur, projekt)
    ueberblick = owner_client.get(_projekt_url(projekt)).content.decode()
    board = owner_client.get(_projekt_url(projekt, "projekt_auftraege")).content.decode()
    seite = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": entwurf.pk})
    ).content.decode()
    for html in (ueberblick, board, seite):
        assert _knoepfe_fuer(html, _aktion(entwurf)) == ["Freigeben"]
    # Der Knopf auf der Board-Karte führt zurück aufs Board, nicht auf eine andere Seite.
    owner_client.post(_aktion(entwurf), {"zurueck": _projekt_url(projekt, "projekt_auftraege")})
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.FREIGEGEBEN


def test_antworten_an_allen_drei_stellen_gleich(owner_client, akteur, projekt, bindung):
    a = _blockiert(akteur, projekt, bindung)
    ziel = reverse("antwort", kwargs={"auftrag_id": a.pk})
    for url in (
        _projekt_url(projekt),
        _projekt_url(projekt, "projekt_auftraege"),
        reverse("auftrag_detail", kwargs={"auftrag_id": a.pk}),
    ):
        html = owner_client.get(url).content.decode()
        assert _knoepfe_fuer(html, ziel) == ["Antworten"], url
    board = owner_client.get(_projekt_url(projekt, "projekt_auftraege")).content.decode()
    assert '<summary class="knopf klein">Antworten</summary>' in board


def test_kein_planen_knopf_in_wartet_auf_dich(owner_client, akteur, projekt, bindung):
    """Freds Problemfall: ein fertig geplanter Entwurf bot nur „Planen“."""
    arbeitspaket(akteur, projekt)
    dienst.wunsch_erfassen(akteur, projekt, titel="Nur eine Idee", beschreibung="b")
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    wartet = html.split('id="h-wartet"')[1].split("</section>")[0]
    assert "Planen" not in wartet and "Nur eine Idee" not in wartet
    eintraege = wartet.count('<li class="eintrag')
    assert eintraege == 1 and wartet.count(">Freigeben</button>") == 1


def test_auftragskopf_steht_vor_dem_inhalt(owner_client, akteur, projekt):
    """Der Hauptknopf steht im Kopf vor allen Abschnitten (bei 390 px ohne Scrollen sichtbar,
    belegt im Bildschirmfoto des Nachweises)."""
    entwurf = arbeitspaket(akteur, projekt)
    html = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": entwurf.pk})
    ).content.decode()
    assert html.index(">Freigeben</button>") < html.index('id="auftrag"')
    assert html.index("Zurück zu den Aufträgen") < html.index('class="box auftragskopf"')


# --- Startseite, Bereiche, Überblick, Board, Verlauf, „+ Neu“ -----------------------------


def test_startseite_projektzeilen(owner_client, akteur, projekt, projekt_b, bindung):
    arbeitspaket(akteur, projekt)
    _blockiert(akteur, projekt, bindung)
    dienst.wunsch_erfassen(akteur, projekt, titel="Idee", beschreibung="b")
    html = owner_client.get(reverse("start")).content.decode()
    assert html.count('class="projektzeile"') == 2
    assert '3 offen · <span class="wartet">2 wartet auf dich</span> · Version unbekannt' in html
    assert "0 offen · <span>0 wartet auf dich</span> · Version unbekannt" in html
    zeile = html.split(f'title="{projekt.name}"')[1].split("</a>")[0]
    assert projekt.kuerzel in zeile and projekt.name in zeile


def test_seitenleiste_vier_bereiche_und_einstellungen(owner_client, projekt):
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    leiste = html.split('<nav class="seitenleiste"')[1].split("</nav>")[0]
    bereiche = [t for h, t in _Seite(leiste).links if h.startswith("/projekt/")]
    assert bereiche == ["Überblick", "Aufträge", "Wissen", "Verlauf"]
    assert (
        leiste.rstrip().endswith("</p>")
        and "Konto &amp; KI verbinden" in leiste.split("Verlauf")[1]
    )
    einstellungen = _projekt_url(projekt, "projekt_einstellungen")
    assert f'href="{einstellungen}"' in html
    seite = owner_client.get(einstellungen).content.decode()
    assert 'id="module"' in seite and 'id="arbeitsorte"' in seite


def test_ueberblick_reihenfolge(owner_client, akteur, projekt, bindung):
    fertig = _blockiert(akteur, projekt, bindung)
    dienst.rueckfrage_beantworten(akteur, fertig, "ok")
    dienst.ergebnis_einreichen(
        dienst.Akteur.aus_bindung(bindung),
        fertig,
        fertig.revision_nr,
        zusammenfassung="fertig",
        dateien="a@1",
        pruefungen="pytest: grün",
    )
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    teile = ["Wartet auf dich", "Version", "Meilensteine", "Zuletzt erledigt", "Beschreibung"]
    stellen = [html.index(f">{t}") for t in teile]
    assert stellen == sorted(stellen)
    assert "Produktion: unbekannt" in html
    erledigt = html.split('id="h-erledigt"')[1].split("</section>")[0]
    assert fertig.titel in erledigt


def test_zuletzt_erledigt_hoechstens_fuenf(owner_client, akteur, projekt):
    for i in range(7):
        a = dienst.wunsch_erfassen(akteur, projekt, titel=f"Erledigt {i}", beschreibung="b")
        Auftrag.objects.filter(pk=a.pk).update(zustand=Auftrag.ERLEDIGT)
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    erledigt = html.split('id="h-erledigt"')[1].split("</section>")[0]
    assert erledigt.count("<li>") == 5


def test_gruppen_und_abgebrochene_zugeklappt(owner_client, akteur, projekt):
    weg = dienst.wunsch_erfassen(akteur, projekt, titel="Verworfen", beschreibung="b")
    dienst.auftrag_abbrechen(akteur, weg, "nicht nötig")
    url = _projekt_url(projekt, "projekt_auftraege")
    board = owner_client.get(url).content.decode()
    assert "Verworfen" in board.split('id="gruppe-abgebrochen"')[1]
    assert "Verworfen" not in board.split('id="gruppe-abgebrochen"')[0]
    # Modulbaum links als Filter
    assert 'class="baumspalte"' in board and "Gesamtes Projekt" in board


def test_verlauf_neuestes_oben_und_filter_nach_art(owner_client, akteur, projekt):
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Später", beschreibung="b")
    html = owner_client.get(_projekt_url(projekt, "projekt_verlauf")).content.decode()
    assert html.index(f"Auftrag {a.nummer}") < html.index("Projekt registriert")
    nur = owner_client.get(_projekt_url(projekt, "projekt_verlauf") + "?art=Auftrag")
    nur = nur.content.decode()
    assert f"Auftrag {a.nummer}" in nur and "Projekt registriert" not in nur
    assert "letzten Besuch" not in nur and "gesehen" not in nur


def test_neu_bietet_auftrag_issue_text_link(owner_client, projekt):
    html = owner_client.get(_projekt_url(projekt)).content.decode()
    menue = html.split('<div class="menue" role="menu">')[1].split("</div>")[0]
    assert [t.split(" ")[0] for _, t in _Seite(menue).links] == ["Auftrag", "Issue", "Text", "Link"]
    for art in ("text", "link"):
        seite = owner_client.get(_projekt_url(projekt, "unterlage_neu") + f"?art={art}")
        assert seite.status_code == 200

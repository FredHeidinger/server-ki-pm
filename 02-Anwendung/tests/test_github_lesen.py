"""GitHub verbinden und Versionszeile (A-7): Lese-Schlüssel, Repository je Projekt, Version.

Fachlogik gegen die ``Attrappe``, der Transport gegen ``github_nachbildung`` (lokaler HTTP-Server).
Beides zeigt nicht, dass GitHub selbst so antwortet; den echten Abruf prüft erst Freds Schlüssel.
"""

import importlib
import json
import logging
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import serializers
from django.urls import reverse
from django.utils import timezone

from steuerung import abgleich, dienst, github, mcp
from steuerung.github import GitHubFehler
from steuerung.models import Ereignis, GitHubZugang, RepoVerbindung, VersionStand
from tests import github_nachbildung as nachbild
from tests.conftest import PASSWORT
from tests.github_attrappe import Attrappe
from tests.test_mcp import _rpc

pytestmark = pytest.mark.django_db

# Zusammengesetzt, damit die Spiegelprüfung kein Geheimnis-Literal sieht.
SCHLUESSEL = "github" + "_pat_" + "A1b2C3d4" * 8


@pytest.fixture
def gh():
    attrappe = Attrappe()
    github.adapter_setzen(attrappe)
    yield attrappe
    github.adapter_setzen(None)


@pytest.fixture
def verbunden(akteur, gh):
    return abgleich.github_verbinden(akteur, SCHLUESSEL)


@pytest.fixture
def mit_repo(akteur, projekt, verbunden, gh):
    return abgleich.repository_waehlen(akteur, projekt, "fred/test")


@pytest.fixture
def server(settings):
    laufend = nachbild.starten()
    settings.KIPM_GITHUB_API = nachbild.adresse(laufend)
    yield laufend
    laufend.shutdown()
    laufend.server_close()


def _u(name, projekt=None, **kw):
    if projekt is not None:
        kw["kennung"] = projekt.kennung
    return reverse(name, kwargs=kw or None)


def _ueberblick(client, projekt):
    return client.get(_u("projekt_detail", projekt)).content.decode()


def _start(client):
    return client.get(reverse("start")).content.decode()


# --- GitHub verbinden: einfügen, prüfen, trennen -------------------------------------------------


def test_einfuegen_speichert_verschluesselt_und_zeigt_verbunden(owner_client, gh):
    leer = owner_client.get(_u("verbindungen")).content.decode()
    assert "GitHub verbinden" in leer and "Speichern und prüfen" in leer
    antwort = owner_client.post(_u("github_verbinden"), {"g-schluessel": SCHLUESSEL}, follow=True)
    seite = antwort.content.decode()
    assert "verbunden als fred, 2 Repositorys lesbar" in seite
    assert SCHLUESSEL not in seite and 'name="g-schluessel"' in seite
    assert 'value="' + SCHLUESSEL not in seite
    zugang = GitHubZugang.objects.get()
    assert SCHLUESSEL not in zugang.schluessel and zugang.konto == "fred"


def test_pruefen_liest_neu_und_trennen_loescht_alles(owner_client, akteur, projekt, mit_repo, gh):
    gh.repo_liste.append({"id": 9, "name": "fred/drittes", "standardbranch": "main"})
    seite = owner_client.post(_u("github_pruefen"), follow=True).content.decode()
    assert "verbunden als fred, 3 Repositorys lesbar" in seite
    assert VersionStand.objects.filter(projekt=projekt).exists()
    seite = owner_client.post(_u("github_trennen"), follow=True).content.decode()
    assert not GitHubZugang.objects.exists() and not VersionStand.objects.exists()
    assert "verbunden als fred" not in seite and "Speichern und prüfen" in seite
    assert "Version unbekannt" in _start(owner_client)
    assert "GitHub verbinden" in _ueberblick(owner_client, projekt)
    assert [e.ereignis for e in Ereignis.objects.filter(objekt_kennung="Lese-Schlüssel")] == [
        "Lese-Schlüssel gelöscht",
        "Lese-Schlüssel gespeichert",
    ]


def test_ersetzen_laesst_nur_einen_schluessel_zurueck(akteur, verbunden, gh):
    alt = verbunden.schluessel
    abgleich.github_verbinden(akteur, SCHLUESSEL + "x")
    assert GitHubZugang.objects.count() == 1 and GitHubZugang.objects.get().schluessel != alt


@pytest.mark.parametrize("eingabe", ["", "   ", "mit leerzeichen", "zeile\numbruch", "x" * 1001])
def test_unbrauchbare_eingabe_abgewiesen(owner_client, akteur, gh, eingabe):
    with pytest.raises(dienst.DienstFehler):
        abgleich.github_verbinden(akteur, eingabe)
    owner_client.post(_u("github_verbinden"), {"g-schluessel": eingabe})
    assert not GitHubZugang.objects.exists()


@pytest.mark.parametrize(
    ("status", "text", "erwartet"),
    [
        (401, "Bad credentials", "lehnt den Schlüssel ab"),
        (403, "Forbidden", "Kein Zugriff"),
        (0, "TimeoutError", "nicht erreichbar"),
        (500, "Boom", "Störung"),
        (418, "seltsam", "unerwartet"),
    ],
)
def test_pruefen_fehler_wird_eine_zeile(owner_client, verbunden, gh, status, text, erwartet):
    gh.fehler["konto"] = GitHubFehler(status, text)
    antwort = owner_client.post(_u("github_pruefen"), follow=True)
    seite = antwort.content.decode()
    assert antwort.status_code == 200 and erwartet in seite and text not in seite


def test_abgelehnter_schluessel_verliert_konto_zeitueberschreitung_nicht(akteur, verbunden, gh):
    gh.fehler["konto"] = GitHubFehler(0, "Zeit")
    zugang = abgleich.github_pruefen(akteur)
    assert zugang.konto == "fred" and len(zugang.repositorys) == 2 and not zugang.abgelehnt
    gh.fehler["konto"] = GitHubFehler(401, "Bad credentials")
    zugang = abgleich.github_pruefen(akteur)
    assert zugang.konto == "" and zugang.repositorys == [] and zugang.abgelehnt
    zugang = abgleich.github_pruefen(akteur)
    assert zugang.konto == "fred" and zugang.fehler == "" and not zugang.abgelehnt


def test_anfragegrenze_nennt_wartezeit(akteur, verbunden, gh):
    gh.fehler["konto"] = GitHubFehler(403, "API rate limit exceeded", warten=600)
    assert "in etwa 10 minuten" in abgleich.github_pruefen(akteur).fehler.lower()


def test_ein_repository_steht_in_der_einzahl(owner_client, gh):
    gh.repo_liste = gh.repo_liste[:1]
    seite = owner_client.post(
        _u("github_verbinden"), {"g-schluessel": SCHLUESSEL}, follow=True
    ).content.decode()
    assert "verbunden als fred, 1 Repository lesbar" in seite
    assert "Verbunden als fred, 1 Repository lesbar." in seite


def test_klassischer_schluessel_mit_schreibrecht_bekommt_hinweis(owner_client, akteur, gh):
    gh.konto_daten["bereiche"] = ["repo", "read:user"]
    owner_client.post(_u("github_verbinden"), {"g-schluessel": SCHLUESSEL})
    assert "darf mehr als lesen (repo)" in owner_client.get(_u("verbindungen")).content.decode()
    gh.konto_daten["bereiche"] = []
    abgleich.github_pruefen(akteur)
    assert "darf mehr als lesen" not in owner_client.get(_u("verbindungen")).content.decode()


def test_geaendertes_servergeheimnis_ergibt_eine_zeile(owner_client, verbunden, gh, settings):
    settings.SECRET_KEY = "ein-anderes-geheimnis-" + "x" * 40
    owner_client.logout()
    owner_client.force_login(get_user_model().objects.get(username="owner-test"))
    seite = owner_client.post(_u("github_pruefen"), follow=True).content.decode()
    assert "nicht mehr lesbar" in seite
    # Neu einfügen behebt es.
    seite = owner_client.post(
        _u("github_verbinden"), {"g-schluessel": SCHLUESSEL}, follow=True
    ).content.decode()
    assert "verbunden als fred" in seite


# --- Repository je Projekt und Umgebungen --------------------------------------------------------


def test_einstellungen_ohne_verbindung_fuehren_zum_verbinden(owner_client, projekt):
    seite = owner_client.get(_u("projekt_einstellungen", projekt)).content.decode()
    assert "noch nicht verbunden" in seite and "#github" in seite and "<select id=" not in seite
    assert "+ Umgebung" not in seite and "GitHub App" not in seite


def test_repository_waehlbar_und_umgebungen_aus_bereitstellungen(owner_client, projekt, verbunden):
    url = _u("projekt_einstellungen", projekt)
    seite = owner_client.get(url).content.decode()
    assert '<option value="fred/test">' in seite and '<option value="fred/zweites">' in seite
    assert "Noch keine gelesen" in seite
    owner_client.post(_u("repo_waehlen", projekt), {"repository": "fred/test"})
    v = RepoVerbindung.objects.get(projekt=projekt)
    assert v.github_id == 4711 and not v.schreibend and v.aktiv
    seite = owner_client.get(url).content.decode()
    assert '<option value="fred/test" selected>' in seite
    assert "<strong>Produktion</strong> · v1.2" in seite
    assert Ereignis.objects.filter(projekt=projekt, ereignis="GitHub-Repository gewählt").exists()


def test_anderes_repository_loest_das_erste_ab_und_zurueck(akteur, projekt, mit_repo, gh):
    zweites = abgleich.repository_waehlen(akteur, projekt, "fred/zweites")
    assert abgleich.aktives_repo(projekt) == zweites
    mit_repo.refresh_from_db()
    assert not mit_repo.aktiv
    wieder = abgleich.repository_waehlen(akteur, projekt, "fred/test")
    assert wieder.pk == mit_repo.pk and wieder.aktiv
    assert projekt.repos.filter(widerrufen_am=None).count() == 1
    assert abgleich.repository_waehlen(akteur, projekt, "") is None
    assert abgleich.aktives_repo(projekt) is None
    assert not VersionStand.objects.filter(projekt=projekt).exists()


def test_dasselbe_repository_in_zwei_projekten_lesend(akteur, projekt, projekt_b, mit_repo, gh):
    b = abgleich.repository_waehlen(akteur, projekt_b, "fred/test")
    assert b.aktiv and abgleich.aktives_repo(projekt).name == "fred/test"


@pytest.mark.parametrize(
    ("status", "erwartet"),
    [(404, "fehlt"), (403, "Kein Zugriff"), (0, "nicht erreichbar"), (401, "lehnt")],
)
def test_wahl_scheitert_mit_zeile_und_aendert_nichts(
    owner_client, projekt, mit_repo, gh, status, erwartet
):
    gh.fehler["repository"] = GitHubFehler(status, "roh")
    antwort = owner_client.post(
        _u("repo_waehlen", projekt), {"repository": "fred/zweites"}, follow=True
    )
    seite = antwort.content.decode()
    assert erwartet in seite and "roh" not in seite
    assert abgleich.aktives_repo(projekt).name == "fred/test"


def test_wahl_ohne_verbindung_und_unbekanntes_repository(akteur, projekt, gh):
    with pytest.raises(dienst.DienstFehler, match="nicht verbunden"):
        abgleich.repository_waehlen(akteur, projekt, "fred/test")
    abgleich.github_verbinden(akteur, SCHLUESSEL)
    with pytest.raises(dienst.DienstFehler, match="fehlt"):
        abgleich.repository_waehlen(akteur, projekt, "fred/gibt-es-nicht")


# --- Versionszeile -------------------------------------------------------------------------------


def _stand(projekt):
    return abgleich.version_anzeige(projekt)


def test_ohne_eintrag_unbekannt_und_hinweis_auf_naechsten_schritt(owner_client, projekt, gh):
    seite = _ueberblick(owner_client, projekt)
    assert "Produktion: unbekannt · neueste: unbekannt" in seite and "GitHub verbinden" in seite
    assert "Version unbekannt" in _start(owner_client)
    abgleich.github_verbinden(
        dienst.Akteur.aus_benutzer(get_user_model().objects.get(username="owner-test")), SCHLUESSEL
    )
    seite = _ueberblick(owner_client, projekt)
    assert "kein Repository gewählt" in seite and "Jetzt prüfen" not in seite


def test_zeile_nicht_aktuell_mit_was_warum_und_startkarte(owner_client, projekt, mit_repo, gh):
    seite = _ueberblick(owner_client, projekt)
    assert "Produktion: v1.2 · neueste: v1.3 → nicht aktuell" in seite
    assert "Produktion läuft v1.2, neueste ist v1.3." in seite
    assert "Release v1.3 ist dort nicht bereitgestellt" in seite
    assert '<span class="wartet">Version nicht aktuell</span>' in _start(owner_client)


def test_aktuell_ueber_ref_und_ueber_commit(owner_client, akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste[0] |= {"ref": "v1.3", "sha": "c" * 40}
    abgleich.version_abrufen(akteur, projekt)
    assert _stand(projekt)["zeile"] == "Produktion: v1.3 · neueste: v1.3 → aktuell"
    assert _stand(projekt)["abweichungen"] == []
    assert "· Version aktuell" in _start(owner_client)
    # Bereitgestellt über einen Branch, aber genau der Commit des Tags.
    gh.bereitstellungs_liste[0] |= {"ref": "main", "sha": "c" * 40}
    abgleich.version_abrufen(akteur, projekt)
    assert _stand(projekt)["zeile"] == "Produktion: v1.3 · neueste: v1.3 → aktuell"


def test_commit_ohne_tag_ist_nicht_aktuell_mit_begruendung(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste[0] |= {"ref": "main", "sha": "e" * 40}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert stand["zeile"] == "Produktion: Commit eeeeeee · neueste: v1.3 → nicht aktuell"
    assert "keinem Release" in stand["abweichungen"][0]["warum"]


def test_release_ohne_bereitstellung_und_umgekehrt_und_gar_nichts(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = []
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert stand["zeile"] == "Produktion: unbekannt · neueste: v1.3"
    assert (
        stand["zustand"] == "unbekannt"
        and "keine Bereitstellung" in stand["abweichungen"][0]["was"]
    )
    gh.release = None
    abgleich.version_abrufen(akteur, projekt)
    assert _stand(projekt)["zeile"] == "Produktion: unbekannt · neueste: unbekannt"
    assert _stand(projekt)["abweichungen"] == []
    gh.bereitstellungs_liste = [
        {
            "id": 1,
            "umgebung": "Produktion",
            "ref": "v1.2",
            "sha": "b" * 40,
            "erstellt": "2026-10-01T10:00:00Z",
            "produktion": True,
        }
    ]
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert stand["zeile"] == "Produktion: v1.2 · neueste: unbekannt"
    assert "kein veröffentlichtes Release" in stand["abweichungen"][0]["was"]


def _bereitstellung(kennung, umgebung, ref, tag, produktion=False):
    return {
        "id": kennung,
        "umgebung": umgebung,
        "ref": ref,
        "sha": "",
        "erstellt": f"2026-10-{tag:02d}T10:00:00Z",
        "produktion": produktion,
    }


def test_produktion_entscheidet_test_wird_nur_gezeigt(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [
        _bereitstellung(1, "production", "v1.3", 1),
        _bereitstellung(2, "Test", "v1.2", 2),
    ]
    gh.zustaende = {1: "success", 2: "success"}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert stand["zeile"] == "production: v1.3 · Test: v1.2 · neueste: v1.3 → aktuell"
    assert stand["abweichungen"] == []


def test_ohne_produktion_entscheiden_alle(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [
        _bereitstellung(1, "Test", "v1.3", 1),
        _bereitstellung(2, "Vorschau", "v1.2", 2),
    ]
    gh.zustaende = {1: "success", 2: "success"}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert (
        stand["zustand"] == "nicht aktuell"
        and "Vorschau läuft v1.2" in stand["abweichungen"][0]["was"]
    )


def test_fehlgeschlagene_bereitstellung_zaehlt_nicht(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [
        _bereitstellung(1, "Produktion", "v1.2", 1, produktion=True),
        _bereitstellung(2, "Produktion", "v1.3", 2, produktion=True),
    ]
    gh.zustaende = {1: "success", 2: "failure"}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert stand["zeile"] == "Produktion: v1.2 · neueste: v1.3 → nicht aktuell"
    assert "fehlgeschlagen" in stand["abweichungen"][0]["was"]


def test_laufende_und_abgeloeste_bereitstellung(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [
        _bereitstellung(1, "Produktion", "v1.2", 1, produktion=True),
        _bereitstellung(2, "Produktion", "v1.3", 2, produktion=True),
    ]
    gh.zustaende = {1: "success", 2: "in_progress"}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert "Produktion: v1.2" in stand["zeile"] and "läuft noch" in stand["abweichungen"][0]["was"]
    # Nur abgelöste Einträge: die Umgebung gibt es nicht mehr.
    gh.zustaende = {1: "inactive", 2: "inactive"}
    abgleich.version_abrufen(akteur, projekt)
    assert _stand(projekt)["umgebungen"] == []


def test_bereitstellung_ohne_zustand_zaehlt_mit_hinweis(akteur, projekt, mit_repo, gh):
    gh.zustaende = {}
    abgleich.version_abrufen(akteur, projekt)
    stand = _stand(projekt)
    assert "Produktion: v1.2" in stand["zeile"]
    assert any("keine Erfolgsmeldung" in a["was"] for a in stand["abweichungen"])


def test_viele_umgebungen_werden_gekuerzt(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [_bereitstellung(i, f"U{i}", "v1.3", i) for i in range(1, 7)]
    gh.zustaende = {i: "success" for i in range(1, 7)}
    abgleich.version_abrufen(akteur, projekt)
    assert "U4: v1.3 · 2 weitere · neueste: v1.3" in _stand(projekt)["zeile"]


@pytest.mark.parametrize(
    ("status", "text", "erwartet"),
    [
        (401, "Bad credentials", "lehnt den Schlüssel ab"),
        (403, "Forbidden", "Kein Zugriff: Der Schlüssel darf Repository fred/test nicht lesen"),
        (404, "Not Found", "Repository fred/test fehlt"),
        (0, "URLError", "nicht erreichbar"),
        (503, "Boom", "Störung"),
    ],
)
def test_fehler_ohne_altdaten_zeile_statt_fehlerseite(
    owner_client, akteur, projekt, verbunden, gh, status, text, erwartet
):
    abgleich.repository_waehlen(akteur, projekt, "fred/test")
    VersionStand.objects.all().delete()
    gh.fehler["repository"] = GitHubFehler(status, text)
    antwort = owner_client.get(_u("projekt_detail", projekt))
    seite = antwort.content.decode()
    assert antwort.status_code == 200 and erwartet in seite and text not in seite
    assert "Produktion: unbekannt" in seite and "Version unbekannt" in _start(owner_client)


def test_fehler_laesst_zuletzt_gelesenes_stehen(owner_client, akteur, projekt, mit_repo, gh):
    gh.fehler["release"] = GitHubFehler(0, "Zeit")
    seite = owner_client.post(_u("version_pruefen", projekt), follow=True).content.decode()
    assert "nicht erreichbar" in seite and "Die Zeile zeigt den Stand vom" in seite
    assert "Produktion: v1.2 · neueste: v1.3 → nicht aktuell" in seite
    # Erholt sich GitHub, verschwindet die Zeile.
    seite = owner_client.post(_u("version_pruefen", projekt), follow=True).content.decode()
    assert "nicht erreichbar" not in seite


def test_abgelehnter_schluessel_beim_abruf_steht_auch_in_verbinden(
    owner_client, akteur, projekt, mit_repo, gh
):
    gh.fehler["repository"] = GitHubFehler(401, "Bad credentials")
    abgleich.version_abrufen(akteur, projekt)
    seite = owner_client.get(_u("verbindungen")).content.decode()
    assert "lehnt den Schlüssel ab" in seite and "verbunden als fred" not in seite


def test_umbenanntes_und_ersetztes_repository(akteur, projekt, mit_repo, gh):
    gh.repository = lambda name: {"id": 4711, "name": "fred/neu", "standardbranch": "main"}
    abgleich.version_abrufen(akteur, projekt)
    mit_repo.refresh_from_db()
    assert mit_repo.name == "fred/neu" and not _stand(projekt)["fehler"]
    gh.repository = lambda name: {"id": 1, "name": "fred/neu", "standardbranch": "main"}
    abgleich.version_abrufen(akteur, projekt)
    assert "fehlt" in _stand(projekt)["fehler"]


def test_abruf_schreibt_kein_ereignis(akteur, projekt, mit_repo, gh):
    vor = Ereignis.objects.count()
    abgleich.version_abrufen(akteur, projekt)
    gh.fehler["repository"] = GitHubFehler(0, "Zeit")
    abgleich.version_abrufen(akteur, projekt)
    assert Ereignis.objects.count() == vor


# --- Auffrischen beim Seitenaufruf ---------------------------------------------------------------


def test_seiten_lesen_frisches_nicht_neu_und_veraltetes_schon(owner_client, projekt, mit_repo, gh):
    gh.lesezugriffe.clear()
    _ueberblick(owner_client, projekt)
    _start(owner_client)
    assert gh.lesezugriffe == []
    VersionStand.objects.update(abgerufen_am=timezone.now() - timedelta(minutes=11))
    _start(owner_client)
    assert gh.lesezugriffe == ["fred/test"]


def test_netzfehler_haelt_die_seite_nicht_fuer_jedes_projekt_auf(
    owner_client, akteur, projekt, projekt_b, mit_repo, gh
):
    abgleich.repository_waehlen(akteur, projekt_b, "fred/zweites")
    VersionStand.objects.all().delete()
    gh.lesezugriffe.clear()
    gh.fehler["repository"] = GitHubFehler(0, "Zeit")
    assert owner_client.get(reverse("start")).status_code == 200
    assert len(gh.lesezugriffe) == 1


def test_zeitgrenze_beendet_das_auffrischen(
    owner_client, akteur, projekt, projekt_b, mit_repo, gh, monkeypatch
):
    abgleich.repository_waehlen(akteur, projekt_b, "fred/zweites")
    VersionStand.objects.all().delete()
    gh.lesezugriffe.clear()
    monkeypatch.setattr(abgleich, "ANZEIGE_SEKUNDEN", -1)
    assert owner_client.get(reverse("start")).status_code == 200
    assert len(gh.lesezugriffe) == 1


def test_abgelehnter_schluessel_wird_nicht_wiederholt(owner_client, akteur, projekt, mit_repo, gh):
    gh.fehler["repository"] = GitHubFehler(401, "Bad credentials")
    abgleich.version_abrufen(akteur, projekt)
    VersionStand.objects.update(abgerufen_am=timezone.now() - timedelta(hours=1))
    gh.lesezugriffe.clear()
    _start(owner_client)
    assert gh.lesezugriffe == []
    # Nach bestandener Prüfung wird wieder gelesen.
    abgleich.github_pruefen(akteur)
    _start(owner_client)
    assert gh.lesezugriffe == ["fred/test"]


def test_getrenntes_repository_wird_nicht_gelesen(owner_client, akteur, projekt, mit_repo, gh):
    abgleich.repository_waehlen(akteur, projekt, "")
    gh.lesezugriffe.clear()
    _ueberblick(owner_client, projekt)
    assert gh.lesezugriffe == []


# --- Transport gegen die Nachbildung -------------------------------------------------------------


def _lesen(server, schluessel=nachbild.GUELTIG, **kw):
    return github.GitHubLesen(schluessel, api=nachbild.adresse(server), **kw)


def test_transport_liest_alles_nur_mit_get(server):
    gh = _lesen(server)
    assert gh.konto() == {"login": "fred-test", "bereiche": []}
    namen = [r["name"] for r in gh.repositorys()]
    assert len(namen) == len(nachbild.REPOS) and "fred/leer" in namen  # zweite Seite
    assert gh.repository("fred/nexus") == {"id": 1, "name": "fred/nexus", "standardbranch": "main"}
    assert gh.neuestes_release("fred/nexus")["tag"] == "v1.3"
    assert gh.neuestes_release("fred/leer") is None
    assert {"name": "v1.2", "sha": "b" * 40} in gh.tags("fred/nexus")
    assert gh.bereitstellungen("fred/nexus")[0]["umgebung"] == "Produktion"
    assert gh.bereitstellung_zustand("fred/nexus", 11) == "success"
    assert gh.bereitstellung_zustand("fred/nexus", 99) == ""
    assert {a["methode"] for a in server.anfragen} == {"GET"}
    assert all(a["schluessel"] == nachbild.GUELTIG for a in server.anfragen)
    assert not any(nachbild.GUELTIG in a["pfad"] for a in server.anfragen)
    assert "per_page=100" in server.anfragen[1]["pfad"]


def test_transport_kuerzt_ref_und_erkennt_unbrauchbares(server, monkeypatch):
    gh = _lesen(server)
    eintrag = {"id": 1, "environment": "P", "ref": "refs/tags/v1.3", "sha": "c" * 40}
    monkeypatch.setattr(gh, "_liste", lambda pfad, max_seiten=3: [eintrag])
    assert gh.bereitstellungen("fred/nexus")[0]["ref"] == "v1.3"
    monkeypatch.setattr(gh, "_objekt", lambda pfad: ({"full_name": "fred/nexus"}, {}))
    with pytest.raises(GitHubFehler) as fehler:
        gh.repository("fred/nexus")
    assert fehler.value.status == 502
    assert github._zahl("Wed, 21 Oct 2026 07:28:00 GMT") == 0 and github._zahl("60") == 60


def test_transport_hat_keine_schreibende_methode():
    oeffentlich = {n for n in dir(github.GitHubLesen) if not n.startswith("_")}
    assert oeffentlich == {
        "konto",
        "repositorys",
        "repository",
        "neuestes_release",
        "tags",
        "bereitstellungen",
        "bereitstellung_zustand",
    }


@pytest.mark.parametrize(
    ("aufruf", "status"),
    [
        (lambda g: g.neuestes_release("fred/gesperrt"), 403),
        (lambda g: g.repository("fred/weg"), 404),
        (lambda g: g.repository("fred/kaputt"), 502),
        (lambda g: g.repository("../boese"), 404),
    ],
)
def test_transport_fehlerstatus(server, aufruf, status):
    with pytest.raises(GitHubFehler) as fehler:
        aufruf(_lesen(server))
    assert fehler.value.status == status


def test_transport_ungueltiger_schluessel_und_zeitueberschreitung(server):
    with pytest.raises(GitHubFehler) as fehler:
        _lesen(server, "falsch").konto()
    assert fehler.value.status == 401
    with pytest.raises(GitHubFehler) as fehler:
        _lesen(server, zeit=0.2).neuestes_release("fred/langsam")
    assert fehler.value.status == 0
    with pytest.raises(GitHubFehler) as fehler:
        github.GitHubLesen(nachbild.GUELTIG, api="http://127.0.0.1:9", zeit=1).konto()
    assert fehler.value.status == 0


def test_umleitung_auf_anderen_host_nimmt_den_schluessel_nicht_mit(server):
    fremd = nachbild.starten()
    try:
        server.fremdes_ziel = nachbild.adresse(fremd).replace("127.0.0.1", "localhost") + "/user"
        with pytest.raises(GitHubFehler) as fehler:
            _lesen(server)._holen(nachbild.adresse(server) + "/umgeleitet/fremd")
        assert fehler.value.status == 301 and fremd.anfragen == []
        status, daten, _ = _lesen(server)._holen(nachbild.adresse(server) + "/umgeleitet/hier")
        assert status == 200 and daten["login"] == "fred-test"
    finally:
        fremd.shutdown()
        fremd.server_close()


def test_durchgang_ueber_die_oberflaeche_gegen_die_nachbildung(owner_client, projekt, server):
    seite = owner_client.post(
        _u("github_verbinden"), {"g-schluessel": nachbild.GUELTIG}, follow=True
    ).content.decode()
    assert f"verbunden als fred-test, {len(nachbild.REPOS)} Repositorys lesbar" in seite
    owner_client.post(_u("repo_waehlen", projekt), {"repository": "fred/nexus"})
    seite = _ueberblick(owner_client, projekt)
    assert "Produktion: v1.2 · Test: v1.3 · neueste: v1.3 → nicht aktuell" in seite
    assert "Version nicht aktuell" in _start(owner_client)
    seite = owner_client.post(
        _u("repo_waehlen", projekt), {"repository": "fred/weg"}, follow=True
    ).content.decode()
    assert "Repository fred/weg fehlt" in seite
    owner_client.post(_u("repo_waehlen", projekt), {"repository": "fred/gesperrt"})
    assert "Kein Zugriff" in _ueberblick(owner_client, projekt)
    owner_client.post(_u("repo_waehlen", projekt), {"repository": "fred/kaputt"}, follow=True)
    assert {a["methode"] for a in server.anfragen} == {"GET"}
    falsch = owner_client.post(
        _u("github_verbinden"), {"g-schluessel": "nachbildung-falsch"}, follow=True
    ).content.decode()
    assert "lehnt den Schlüssel ab" in falsch


# --- Befunde der Gegenprüfung --------------------------------------------------------------------


def test_abgelehnter_schluessel_zeigt_in_allen_projekten_die_zeile(
    owner_client, akteur, projekt, projekt_b, mit_repo, gh
):
    """Ein abgelaufener Schlüssel darf andere Projekte nicht weiter „aktuell“ nennen."""
    gh.bereitstellungs_liste[0] |= {"ref": "v1.3", "sha": "c" * 40}
    abgleich.version_abrufen(akteur, projekt)
    abgleich.repository_waehlen(akteur, projekt_b, "fred/zweites")
    assert _stand(projekt_b)["zustand"] == "aktuell"
    gh.fehler["konto"] = GitHubFehler(401, "Bad credentials")
    abgleich.github_pruefen(akteur)
    for p in (projekt, projekt_b):
        stand = _stand(p)
        assert "lehnt den Schlüssel ab" in stand["fehler"] and stand["zustand"] == "unbekannt"
        assert "lehnt den Schlüssel ab" in _ueberblick(owner_client, p)
    start = _start(owner_client)
    assert "Version aktuell" not in start and start.count("Version unbekannt") == 2


def test_fehler_macht_die_startkarte_unbekannt_die_zeile_bleibt(
    owner_client, akteur, projekt, mit_repo, gh
):
    gh.bereitstellungs_liste[0] |= {"ref": "v1.3", "sha": "c" * 40}
    abgleich.version_abrufen(akteur, projekt)
    gh.fehler["repository"] = GitHubFehler(403, "Forbidden")
    abgleich.version_abrufen(akteur, projekt)
    assert "Version unbekannt" in _start(owner_client)
    assert "Produktion: v1.3 · neueste: v1.3 → aktuell" in _ueberblick(owner_client, projekt)


def test_neuer_schluessel_loescht_alte_fehlerzeilen(owner_client, akteur, projekt, mit_repo, gh):
    gh.fehler["repository"] = GitHubFehler(401, "Bad credentials")
    abgleich.version_abrufen(akteur, projekt)
    assert "lehnt" in _stand(projekt)["fehler"]
    abgleich.github_verbinden(akteur, SCHLUESSEL + "neu")
    assert _stand(projekt)["fehler"] == ""
    gh.lesezugriffe.clear()
    seite = _ueberblick(owner_client, projekt)
    assert "lehnt" not in seite and gh.lesezugriffe == ["fred/test"]


def test_abgelehnter_ersatz_laesst_den_guten_schluessel_stehen(owner_client, verbunden, gh):
    vorher = GitHubZugang.objects.get().schluessel
    gh.fehler["konto"] = GitHubFehler(401, "Bad credentials")
    seite = owner_client.post(
        _u("github_verbinden"), {"g-schluessel": SCHLUESSEL + "falsch"}, follow=True
    ).content.decode()
    assert "lehnt den Schlüssel ab" in seite and "verbunden als fred" in seite
    assert GitHubZugang.objects.get().schluessel == vorher


@pytest.mark.parametrize("status", [0, 403, 500])
def test_ungeprueften_ersatz_gibt_es_nicht(akteur, verbunden, gh, status):
    vorher = GitHubZugang.objects.get().schluessel
    gh.fehler["konto"] = GitHubFehler(status, "roh")
    with pytest.raises(dienst.DienstFehler, match="bleibt gespeichert"):
        abgleich.github_verbinden(akteur, SCHLUESSEL + "neu")
    assert GitHubZugang.objects.get().schluessel == vorher


def test_abgelehnten_schluessel_ersetzt_auch_ein_ungepruefter(akteur, verbunden, gh):
    """Der alte taugt nichts mehr; dann geht Speichern vor, das Ergebnis zeigt die Prüfung."""
    gh.fehler["konto"] = GitHubFehler(401, "Bad credentials")
    abgleich.github_pruefen(akteur)
    vorher = GitHubZugang.objects.get().schluessel
    gh.fehler["konto"] = GitHubFehler(0, "Zeit")
    zugang = abgleich.github_verbinden(akteur, SCHLUESSEL + "neu")
    assert zugang.schluessel != vorher and "nicht erreichbar" in zugang.fehler


def test_netzausfall_nennt_kein_projekt_mehr_aktuell_und_wartet_nur_einmal(
    owner_client, akteur, projekt, projekt_b, mit_repo, gh
):
    gh.bereitstellungs_liste[0] |= {"ref": "v1.3", "sha": "c" * 40}
    abgleich.version_abrufen(akteur, projekt)
    abgleich.repository_waehlen(akteur, projekt_b, "fred/zweites")
    VersionStand.objects.update(abgerufen_am=timezone.now() - timedelta(minutes=11))
    gh.lesezugriffe.clear()
    gh.fehler["repository"] = GitHubFehler(0, "Zeit")
    start = _start(owner_client)
    assert len(gh.lesezugriffe) == 1
    assert "Version aktuell" not in start and start.count("Version unbekannt") == 2
    for p in (projekt, projekt_b):
        assert "nicht erreichbar" in _stand(p)["fehler"]
    # Der nächste Aufruf wartet nicht erneut auf GitHub; nach zehn Minuten wird neu gelesen.
    _start(owner_client)
    assert len(gh.lesezugriffe) == 1
    VersionStand.objects.update(abgerufen_am=timezone.now() - timedelta(minutes=11))
    start = _start(owner_client)
    assert "Version aktuell" in start and "Version unbekannt" not in start


def test_pruefen_waehrend_stoerung_hebt_die_ablehnung_nicht_auf(akteur, verbunden, gh):
    gh.fehler["konto"] = GitHubFehler(401, "Bad credentials")
    abgleich.github_pruefen(akteur)
    gh.fehler["konto"] = GitHubFehler(0, "Zeit")
    assert abgleich.github_pruefen(akteur).abgelehnt


def test_erfolgreiches_lesen_hebt_die_ablehnung_auf(akteur, projekt, mit_repo, gh):
    gh.fehler["repository"] = GitHubFehler(401, "Bad credentials")
    abgleich.version_abrufen(akteur, projekt)
    assert "lehnt" in _stand(projekt)["fehler"]
    abgleich.version_abrufen(akteur, projekt)
    assert _stand(projekt)["fehler"] == ""
    zugang = GitHubZugang.objects.get()
    assert not zugang.abgelehnt and zugang.fehler == "" and zugang.konto == "fred"


def test_gesamtzeit_begrenzt_einen_abruf(monkeypatch):
    """Viele träge Antworten dürfen den Serverprozess nicht in den Abbruch laufen lassen."""
    gh = github.GitHubLesen(SCHLUESSEL, api="http://127.0.0.1:9")
    monkeypatch.setattr(github, "_anfrage", lambda *a, **k: pytest.fail("Anfrage trotz Zeitende"))
    monkeypatch.setattr(gh, "_ende", 0)
    with pytest.raises(GitHubFehler) as fehler:
        gh.konto()
    assert fehler.value.status == 0


def test_nicht_owner_sieht_konto_und_repositorys_nicht(client, zweiter_mensch, projekt, verbunden):
    client.force_login(zweiter_mensch)
    for url in (_u("verbindungen"), _u("projekt_einstellungen", projekt)):
        seite = client.get(url).content.decode()
        assert "verbunden als fred" not in seite and "fred/zweites" not in seite


def test_abgebrochener_fehlerrumpf_wird_eine_zeile(monkeypatch):
    """Antwortet GitHub mit Fehlerstatus und bricht dann ab, darf die Seite nicht abstürzen."""
    import io
    import urllib.error

    class Abbruch(io.BytesIO):
        def read(self, *a):
            raise TimeoutError

    def oeffnen(anfrage, timeout=None):
        raise urllib.error.HTTPError(anfrage.full_url, 502, "Bad Gateway", {}, Abbruch())

    monkeypatch.setattr(github._oeffner, "open", oeffnen)
    with pytest.raises(GitHubFehler) as fehler:
        github.GitHubLesen(SCHLUESSEL, api="http://127.0.0.1:9").konto()
    assert fehler.value.status == 502


def test_unbrauchbare_bereitstellungen_werden_uebersprungen(server, monkeypatch):
    gh = _lesen(server)
    eintraege = [
        {"id": None, "environment": "A", "ref": "v1", "sha": "x"},
        {"id": 5, "environment": None, "ref": None, "sha": None, "created_at": None},
    ]
    monkeypatch.setattr(gh, "_liste", lambda pfad, max_seiten=3: eintraege)
    assert gh.bereitstellungen("fred/nexus") == [
        {"id": 5, "umgebung": "", "ref": "", "sha": "", "erstellt": "", "produktion": False}
    ]


def test_seitenlink_auf_anderen_host_wird_nicht_verfolgt(monkeypatch):
    gh = github.GitHubLesen(SCHLUESSEL, api="http://127.0.0.1:9")
    aufrufe = []

    def holen(url):
        aufrufe.append(url)
        return 200, [{"id": 1}], {"Link": '<http://fremd.example/user/repos?page=2>; rel="next"'}

    monkeypatch.setattr(gh, "_holen", holen)
    with pytest.raises(GitHubFehler):
        gh._liste("/user/repos")
    assert len(aufrufe) == 1


def test_viele_umgebungen_begrenzen_die_anfragen_produktion_zuerst(akteur, projekt, mit_repo, gh):
    gh.bereitstellungs_liste = [
        _bereitstellung(i, f"U{i:02d}", "v1.3", i % 28 + 1) for i in range(30)
    ]
    gh.bereitstellungs_liste.append(_bereitstellung(99, "Produktion", "v1.3", 1, produktion=True))
    gh.zustaende = {i: "success" for i in [*range(30), 99]}
    gefragt = []
    original = gh.bereitstellung_zustand
    gh.bereitstellung_zustand = lambda n, k: gefragt.append(k) or original(n, k)
    abgleich.version_abrufen(akteur, projekt)
    assert len(gefragt) == abgleich.UMGEBUNGEN_HOECHSTENS and 99 in gefragt
    assert _stand(projekt)["zustand"] == "aktuell"


def test_warum_behauptet_bei_abweichendem_tag_nicht_rueckstand(akteur, projekt, mit_repo, gh):
    """/releases/latest kennt keine Vorabversionen: v1.4 kann neuer sein als das neueste Release."""
    gh.tag_liste.append({"name": "v1.4-rc", "sha": "d" * 40})
    gh.bereitstellungs_liste[0] |= {"ref": "v1.4-rc", "sha": "d" * 40}
    abgleich.version_abrufen(akteur, projekt)
    warum = _stand(projekt)["abweichungen"][0]["warum"]
    assert "noch nicht" not in warum and "älter oder neuer" in warum


def test_einstellungen_nennen_die_produktionsregel_einmal(owner_client, projekt, mit_repo):
    seite = owner_client.get(_u("projekt_einstellungen", projekt)).content.decode()
    assert seite.count("Gibt es eine Produktion") == 1


# --- Der Schlüssel bleibt geheim -----------------------------------------------------------------


def _seiten(client, projekt) -> list[str]:
    namen = ["start", "verbindungen"]
    projektseiten = [
        "projekt_detail",
        "projekt_auftraege",
        "akte_wissen",
        "projekt_verlauf",
        "projekt_einstellungen",
    ]
    return [client.get(_u(n)).content.decode() for n in namen] + [
        client.get(_u(n, projekt)).content.decode() for n in projektseiten
    ]


def test_schluessel_in_keiner_seite_antwort_log_und_keinem_datensatz(
    client, owner, akteur, projekt, server, caplog, settings
):
    """Mit echtem Transport und Fehlern, weil gerade Fehlerwege Geheimnisse ausplaudern."""
    settings.KIPM_GITHUB_ZEIT = 0.3
    geheim = nachbild.GUELTIG
    caplog.set_level(logging.DEBUG)
    client.force_login(owner)
    gesammelt = [
        client.post(_u("github_verbinden"), {"g-schluessel": geheim}, follow=True).content.decode(),
        client.post(_u("github_pruefen"), follow=True).content.decode(),
    ]
    for repo in ("fred/nexus", "fred/gesperrt", "fred/langsam", "fred/kaputt", "fred/weg"):
        gesammelt.append(
            client.post(
                _u("repo_waehlen", projekt), {"repository": repo}, follow=True
            ).content.decode()
        )
        gesammelt.append(client.post(_u("version_pruefen", projekt), follow=True).content.decode())
        gesammelt += _seiten(client, projekt)

    # Antworten der Schnittstelle an KI-Zugänge, je Art: alle Werkzeuge.
    pc, pc_schluessel = dienst.bindung_anlegen(akteur, None, name="PC")
    _, projekt_schluessel = dienst.bindung_anlegen(akteur, projekt, name="Projektchat")
    for zugang in (pc_schluessel, projekt_schluessel):
        for werkzeug, *_ in mcp.WERKZEUGE:
            antwort = _rpc(
                client,
                zugang,
                "tools/call",
                {"name": werkzeug, "arguments": {"projekt": projekt.kennung}},
            )
            gesammelt.append(antwort.content.decode())
        gesammelt.append(_rpc(client, zugang, "tools/list").content.decode())
        gesammelt.append(_rpc(client, zugang, "initialize").content.decode())

    # Falscher Schlüssel über denselben Weg; auch er taucht nirgends auf.
    falsch = "nachbildung-falsch-" + "z" * 20
    gesammelt.append(
        client.post(_u("github_verbinden"), {"g-schluessel": falsch}, follow=True).content.decode()
    )
    client.post(_u("github_verbinden"), {"g-schluessel": geheim})

    assert len(gesammelt) > 60
    for inhalt in gesammelt:
        assert geheim not in inhalt and falsch not in inhalt
    assert geheim not in caplog.text and falsch not in caplog.text

    # Kein Datensatz außerhalb der verschlüsselten Zeile enthält ihn, auch die Zeile nur chiffriert.
    from django.apps import apps

    alles = serializers.serialize(
        "json", [o for m in apps.get_app_config("steuerung").get_models() for o in m.objects.all()]
    )
    assert geheim not in alles and falsch not in alles
    assert geheim not in GitHubZugang.objects.get().schluessel
    # Und GitHub bekam ihn wirklich: er ist nur versteckt, nicht kaputt.
    assert any(a["schluessel"] == geheim for a in server.anfragen)


def test_darstellung_des_zugangs_kennt_keinen_schluessel(akteur, verbunden):
    anzeige = abgleich.zugang_anzeige(akteur)
    assert "schluessel" not in anzeige
    assert SCHLUESSEL not in json.dumps(anzeige, default=str)
    assert verbunden.schluessel not in json.dumps(anzeige, default=str)
    assert SCHLUESSEL not in repr(github.GitHubLesen(SCHLUESSEL))
    assert SCHLUESSEL not in repr(verbunden)


@pytest.mark.parametrize("art", ["projekt", "pc"])
def test_ki_zugaenge_koennen_weder_lesen_noch_setzen(client, akteur, projekt, verbunden, gh, art):
    bindung, klartext = dienst.bindung_anlegen(
        akteur, projekt if art == "projekt" else None, name="KI"
    )
    ki = dienst.Akteur.aus_bindung(bindung, projekt=projekt)
    vorher = GitHubZugang.objects.get().schluessel
    for aufruf in (
        lambda: abgleich.github_verbinden(ki, SCHLUESSEL + "x"),
        lambda: abgleich.github_pruefen(ki),
        lambda: abgleich.github_trennen(ki),
        lambda: abgleich.repository_waehlen(ki, projekt, "fred/test"),
    ):
        with pytest.raises(dienst.KeinZugriff):
            aufruf()
    # Über HTTP: der Bearer-Schlüssel zählt auf diesen Seiten nicht, es geht zur Anmeldung.
    kopf = {"HTTP_AUTHORIZATION": f"Bearer {klartext}"}
    for name, daten in (
        ("github_verbinden", {"g-schluessel": SCHLUESSEL + "x"}),
        ("github_pruefen", {}),
        ("github_trennen", {}),
    ):
        antwort = client.post(_u(name), daten, **kopf)
        assert antwort.status_code == 302 and reverse("anmelden") in antwort["Location"]
    antwort = client.post(_u("repo_waehlen", projekt), {"repository": "fred/test"}, **kopf)
    assert antwort.status_code == 302 and reverse("anmelden") in antwort["Location"]
    assert client.get(_u("verbindungen"), **kopf).status_code == 302
    assert GitHubZugang.objects.get().schluessel == vorher
    assert not RepoVerbindung.objects.filter(projekt=projekt).exists()
    # Kein Werkzeug nennt oder nimmt den Schlüssel.
    liste = _rpc(client, klartext, "tools/list").json()["result"]["tools"]
    for werkzeug in liste:
        text = json.dumps(werkzeug, ensure_ascii=False).lower()
        assert "lese-schlüssel" not in text and "github_verbinden" not in text
        assert "token" not in text and "schluessel" not in text


def test_nur_der_owner_verbindet_von_hand(client, zweiter_mensch, projekt, akteur, verbunden, gh):
    client.force_login(zweiter_mensch)
    vorher = GitHubZugang.objects.get().schluessel
    seite = client.post(
        _u("github_verbinden"), {"g-schluessel": SCHLUESSEL + "x"}, follow=True
    ).content.decode()
    assert "darf nur der Owner" in seite
    client.post(_u("github_trennen"))
    client.post(_u("github_pruefen"))
    client.post(_u("repo_waehlen", projekt), {"repository": "fred/test"})
    assert GitHubZugang.objects.get().schluessel == vorher
    assert not RepoVerbindung.objects.filter(projekt=projekt).exists()


def test_abmelden_sperrt_die_seiten(client, owner, verbunden, gh):
    client.force_login(owner)
    client.logout()
    assert client.post(_u("github_trennen")).status_code == 302
    assert GitHubZugang.objects.exists()
    assert client.login(username="owner-test", password=PASSWORT)


def test_nur_post_und_csrf_auf_den_aenderungswegen(owner, gh, akteur, projekt):
    from django.test import Client

    streng = Client(enforce_csrf_checks=True)
    streng.force_login(owner)
    assert streng.post(_u("github_verbinden"), {"g-schluessel": SCHLUESSEL}).status_code == 403
    assert not GitHubZugang.objects.exists()
    for name in ("github_verbinden", "github_pruefen", "github_trennen"):
        assert streng.get(_u(name)).status_code == 405
    assert streng.get(_u("repo_waehlen", projekt)).status_code == 405
    assert streng.get(_u("version_pruefen", projekt)).status_code == 405


def test_migration_0011_ist_nur_additiv():
    modul = importlib.import_module("steuerung.migrations.0011_github_lesezugang")
    assert {type(o).__name__ for o in modul.Migration.operations} == {"CreateModel"}

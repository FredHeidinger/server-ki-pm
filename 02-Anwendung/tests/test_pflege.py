"""Pflege in Oberfläche und für die KI (Verbesserungsliste 3b, 3c): Umgebungen und Releases
bearbeiten und archivieren, Meilensteine vollständig bearbeiten, Unterlagen Entscheidungen und
Releases zuordnen, GitHub-Issues mit Aufträgen verknüpfen; Profil und Umgebung als Entwurf der
KI, den Fred übernimmt."""

import pytest
from django.urls import reverse
from django.utils import timezone

from steuerung import akte, dienst
from steuerung.models import ExternesObjekt, Release, RepoVerbindung, Umgebung
from tests.test_mcp import _werkzeug


@pytest.fixture
def pc(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Assistent")
    b.klartext = schluessel
    return b


def _ki(pc, projekt):
    return dienst.Akteur.aus_bindung(pc, projekt=projekt)


# --- Umgebungen ---------------------------------------------------------------------------


def test_umgebung_als_entwurf_der_ki_zaehlt_erst_nach_uebernahme(
    client, owner_client, projekt, bindung, pc
):
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_umgebung_vorschlagen",
        projekt="synth-a",
        name="Testserver",
        art="test",
        url="https://test.synth.invalid",
    )
    assert not fehler, daten
    assert daten["umgebung"]["bestaetigt"] is False
    u = Umgebung.objects.get(name="Testserver")
    # Projektchat meldet auf einen Entwurf: abgewiesen.
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_installation_melden",
        umgebung="testserver",
        komponente="app",
        version="abc123",
        herkunft="Test",
    )
    assert fehler and "Entwurf" in daten["fehler"]
    seite = owner_client.get(reverse("projekt_einstellungen", kwargs={"kennung": "synth-a"}))
    assert "Entwurf von Assistent" in seite.content.decode()
    owner_client.post(
        reverse(
            "umgebung_pflegen", kwargs={"kennung": "synth-a", "pk": u.pk, "wahl": "bestaetigen"}
        ),
        {"revision": u.revision},
    )
    u.refresh_from_db()
    assert u.bestaetigt
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_installation_melden",
        umgebung="testserver",
        komponente="app",
        version="abc123",
        herkunft="Test",
    )
    assert not fehler, daten
    # Neue Adresse durch die KI: wieder Entwurf; veraltete Revision: Konflikt.
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_umgebung_aendern",
        projekt="synth-a",
        umgebung="testserver",
        revision=u.revision,
        url="https://neu.synth.invalid",
        grund="Umzug",
    )
    assert not fehler and daten["umgebung"]["bestaetigt"] is False
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_umgebung_aendern",
        projekt="synth-a",
        umgebung="testserver",
        revision=u.revision,
        name="Alt",
    )
    assert fehler and "inzwischen geändert" in daten["fehler"]


def test_umgebung_in_oberflaeche_aendern_und_archivieren(owner_client, akteur, projekt):
    u = akte.umgebung_anlegen(akteur, projekt, name="Betrieb", art="produktion")
    url = "umgebung_pflegen"
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": u.pk, "wahl": "aendern"}),
        {"revision": u.revision, f"u{u.pk}-name": "Betrieb CT 100", f"u{u.pk}-art": "produktion"},
    )
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": u.pk, "wahl": "archivieren"}),
        {"revision": u.revision + 1},
    )
    u.refresh_from_db()
    assert (u.name, u.kennung, u.aktiv, u.revision) == ("Betrieb CT 100", "betrieb", False, 3)
    with pytest.raises(dienst.DienstFehler, match="archiviert"):
        akte.installation_melden(
            akteur, projekt, umgebung=u, komponente="a", version="1", herkunft="x"
        )
    seite = owner_client.get(reverse("projekt_einstellungen", kwargs={"kennung": "synth-a"}))
    assert "Archiv: 1 Umgebung" in seite.content.decode()


def test_projektchat_schlaegt_keine_umgebung_und_kein_profil_vor(client, bindung):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_umgebung_vorschlagen", name="X", art="test"
    )
    assert fehler and "Owner oder sein KI-Assistent" in daten["fehler"]
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_profil_vorschlagen", revision=0, zweck="Fremd"
    )
    assert fehler and not Umgebung.objects.filter(name="X").exists()


# --- Releases -----------------------------------------------------------------------------


def test_release_vorschlagen_aendern_und_veroeffentlicht_fest(client, owner_client, projekt, pc):
    komponenten = [{"komponente": "app", "repository": "b/r", "commit": "abc1234"}]
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_release_vorschlagen",
        projekt="synth-a",
        kennung="1.0.0",
        komponenten=komponenten,
    )
    assert not fehler and daten["release"]["zustand"] == "entwurf"
    r = Release.objects.get(kennung="1.0.0")
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_release_aendern",
        projekt="synth-a",
        release="1.0.0",
        revision=r.revision,
        komponenten=[*komponenten, {"komponente": "doku", "commit": "def5678"}],
    )
    assert not fehler and len(daten["release"]["komponenten"]) == 2
    r.refresh_from_db()
    owner_client.post(
        reverse("release_zustand", kwargs={"kennung": "synth-a", "pk": r.pk}),
        {"zustand": "veroeffentlicht", "revision": r.revision},
    )
    r.refresh_from_db()
    assert r.zustand == "veroeffentlicht"
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_release_aendern",
        projekt="synth-a",
        release="1.0.0",
        revision=r.revision,
        titel="Umgeschrieben",
    )
    assert fehler and "nur Fred" in daten["fehler"]
    with pytest.raises(dienst.Konflikt):
        akte.release_aendern(
            _ki(pc, projekt), r, erwartete_revision=r.revision, komponenten=komponenten
        )


def test_release_in_oberflaeche_bearbeiten_und_archivieren(
    owner_client, akteur, projekt, projekt_b
):
    r = akte.release_anlegen(
        akteur, projekt, kennung="2.0", komponenten=[{"komponente": "app", "commit": "aaa1111"}]
    )
    url = "release_pflegen"
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": r.pk, "wahl": "aendern"}),
        {
            "revision": r.revision,
            "titel": "Zweite",
            "beschreibung": "B",
            "komponenten": "app | b/r | bbb2222\nwerkzeug | b/w | ccc3333",
        },
    )
    r.refresh_from_db()
    assert r.titel == "Zweite" and r.komponenten.count() == 2
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": r.pk, "wahl": "archivieren"}),
        {"revision": r.revision},
    )
    r.refresh_from_db()
    assert r.archiviert
    seite = owner_client.get(reverse("akte_betrieb", kwargs={"kennung": "synth-a"})).content
    assert "Archiv: 1 Release" in seite.decode()
    fremd = akte.release_anlegen(
        akteur, projekt_b, kennung="9", komponenten=[{"komponente": "x", "commit": "f00"}]
    )
    antwort = owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": fremd.pk, "wahl": "archivieren"})
    )
    assert antwort.status_code == 404


# --- Meilensteine -------------------------------------------------------------------------


def test_meilenstein_vollstaendig_bearbeiten(owner_client, akteur, projekt, projekt_b, pc):
    f1 = akte.funktion_anlegen(akteur, projekt, titel="Eins")
    f2 = akte.funktion_anlegen(akteur, projekt, titel="Zwei")
    m = akte.meilenstein_anlegen(akteur, projekt, name="M1", funktionen=[f1])
    owner_client.post(
        reverse("meilenstein_bearbeiten", kwargs={"kennung": "synth-a", "pk": m.pk}),
        {
            "revision": m.revision,
            f"m{m.pk}-name": "Erste Stufe",
            f"m{m.pk}-ziel": "Karten fertig",
            f"m{m.pk}-zielversion": "1.1",
            f"m{m.pk}-termin": "2026-12-01",
            f"m{m.pk}-funktionen": [f2.pk],
        },
    )
    m.refresh_from_db()
    assert (m.name, m.ziel, m.zielversion, str(m.termin)) == (
        "Erste Stufe",
        "Karten fertig",
        "1.1",
        "2026-12-01",
    )
    assert list(m.funktionen.all()) == [f2] and m.revision == 2
    with pytest.raises(dienst.Konflikt):
        akte.meilenstein_bearbeiten(akteur, m, erwartete_revision=1, name="Alt")
    fremd = akte.funktion_anlegen(akteur, projekt_b, titel="Fremd")
    with pytest.raises(dienst.KeinZugriff):
        akte.meilenstein_bearbeiten(akteur, m, erwartete_revision=2, name="X", funktionen=[fremd])
    with pytest.raises(dienst.KeinZugriff):
        akte.meilenstein_bearbeiten(_ki(pc, projekt), m, erwartete_revision=2, name="KI")


# --- Profilentwurf ------------------------------------------------------------------------


def test_profilentwurf_der_ki_fred_uebernimmt_oder_verwirft(
    client, owner_client, akteur, projekt, pc
):
    fehler, daten = _werkzeug(
        client,
        pc.klartext,
        "kipm_profil_vorschlagen",
        projekt="synth-a",
        revision=0,
        zweck="Synthetisches Testprojekt",
        phase="Aufbau",
        grund="aus README",
    )
    assert not fehler, daten
    projekt.refresh_from_db()
    assert projekt.zweck == "" and projekt.profil_revision == 0
    seite = owner_client.get(reverse("projekt_detail", kwargs={"kennung": "synth-a"}))
    assert "Profilentwurf von" in seite.content.decode()
    akte_daten = _werkzeug(client, pc.klartext, "kipm_projektakte", projekt="synth-a")[1]
    assert akte_daten["projekt"]["profilentwurf"]["felder"]["zweck"] == "Synthetisches Testprojekt"
    owner_client.post(
        reverse("profil_entwurf", kwargs={"kennung": "synth-a", "wahl": "uebernehmen"}),
        {"entwurf": akte.entwurf_hash(projekt)},
    )
    projekt.refresh_from_db()
    assert (projekt.zweck, projekt.phase, projekt.profil_revision) == (
        "Synthetisches Testprojekt",
        "Aufbau",
        1,
    )
    assert projekt.profil_entwurf == {}
    # Zwischenzeitliche Änderung durch Fred: Übernahme ist ein Konflikt, Entwurf bleibt.
    akte.profil_vorschlagen(_ki(pc, projekt), projekt, erwartete_revision=1, zweck="KI-Text")
    akte.profil_bearbeiten(akteur, projekt, erwartete_revision=1, zweck="Freds Text")
    with pytest.raises(dienst.Konflikt):
        akte.profil_entwurf_uebernehmen(
            akteur, projekt, uebernehmen=True, erwarteter_entwurf=akte.entwurf_hash(projekt)
        )
    projekt.refresh_from_db()
    assert projekt.zweck == "Freds Text" and projekt.profil_entwurf
    akte.profil_entwurf_uebernehmen(akteur, projekt, uebernehmen=False)
    projekt.refresh_from_db()
    assert projekt.profil_entwurf == {} and projekt.zweck == "Freds Text"
    with pytest.raises(dienst.KeinZugriff):
        akte.profil_entwurf_uebernehmen(_ki(pc, projekt), projekt, uebernehmen=True)


# --- Unterlagen und GitHub ----------------------------------------------------------------


def test_unterlage_entscheidung_und_release_zuordnen(owner_client, akteur, projekt, projekt_b):
    u = akte.text_anlegen(akteur, projekt, titel="Konzept", text="Inhalt")
    e = dienst.entscheidung_vorschlagen(
        akteur, titel="SQLite bleibt", text="Begründung", verweis="", projekte=[projekt]
    )
    r = akte.release_anlegen(
        akteur, projekt, kennung="1.0", komponenten=[{"komponente": "app", "commit": "abc"}]
    )
    url = reverse(
        "unterlage_aktion", kwargs={"kennung": "synth-a", "nummer": u.nummer, "aktion": "zuordnen"}
    )
    seite = owner_client.get(
        reverse("unterlage_detail", kwargs={"kennung": "synth-a", "nummer": u.nummer})
    ).content.decode()
    assert f'value="e:{e.pk}"' in seite and f'value="r:{r.pk}"' in seite
    owner_client.post(url, {"version": 1, "ziel": f"e:{e.pk}", "zweck": "Begründung"})
    owner_client.post(url, {"version": 1, "ziel": f"r:{r.pk}", "zweck": "Freigabeunterlage"})
    v = u.versionen.get(nummer=1)
    assert {(z.entscheidung_id, z.release_id) for z in v.zuordnungen.all()} == {
        (e.pk, None),
        (None, r.pk),
    }
    fremd = dienst.entscheidung_vorschlagen(
        akteur, titel="Fremd", text="t", verweis="", projekte=[projekt_b]
    )
    assert owner_client.post(url, {"version": 1, "ziel": f"e:{fremd.pk}"}).status_code == 404


def test_github_issue_mit_auftrag_verknuepfen(owner_client, akteur, projekt, projekt_b):
    v = RepoVerbindung.objects.create(projekt=projekt, github_id=4711, name="beispiel/synth")
    issue = ExternesObjekt.objects.create(
        verbindung=v,
        typ="issue",
        externe_id="9001",
        nummer=7,
        titel="Karte bricht um",
        zuletzt_gesehen=timezone.now(),
    )
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Umbruch beheben", beschreibung="b")
    url = reverse("github_objekt_verknuepfen", kwargs={"kennung": "synth-a", "pk": issue.pk})
    seite = owner_client.get(reverse("akte_betrieb", kwargs={"kennung": "synth-a"})).content
    assert "Verknüpfen" in seite.decode()
    owner_client.post(url, {"auftrag": a.pk})
    issue.refresh_from_db()
    assert issue.auftrag == a
    a.refresh_from_db()
    assert a.zustand == "ENTWURF"  # Verknüpfen ändert keinen Auftragszustand
    fremd = dienst.wunsch_erfassen(akteur, projekt_b, titel="Fremd", beschreibung="b")
    assert owner_client.post(url, {"auftrag": fremd.pk}).status_code == 404
    owner_client.post(url, {"auftrag": ""})
    issue.refresh_from_db()
    assert issue.auftrag is None

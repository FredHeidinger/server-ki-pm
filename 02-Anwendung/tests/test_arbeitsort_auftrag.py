"""Arbeitsort direkt am Auftrag: Auswahl und „+ Arbeitsort“ in der Freigabeprüfung, Vorschlag
einer KI mit Bestätigung durch Fred (Verbesserungsliste 3d)."""

import pytest
from django.urls import reverse

from steuerung import dienst
from steuerung.models import Arbeitsort, Auftrag
from tests.test_mcp import _werkzeug


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


@pytest.fixture
def ohne_ort(akteur, projekt):
    """Freigabereifer Auftrag, nur der Arbeitsort fehlt."""
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Export", beschreibung="CSV")
    dienst.auftrag_bearbeiten(
        akteur,
        a,
        titel="Export",
        beschreibung="CSV",
        ziel="CSV-Export",
        nicht_ziele="keine",
        arbeitsort=None,
        schreibumfang="src/export/**",
        quellen="",
        abnahmekriterien="Datei lässt sich öffnen",
        prioritaet="mittel",
        modul=None,
    )
    a.refresh_from_db()
    return a


def _offen(a: Auftrag) -> list[str]:
    a.refresh_from_db()
    return [p.text for p in dienst.freigabepruefung(a) if not p.erfuellt]


def test_fehlender_arbeitsort_zeigt_auswahl_und_plus(owner_client, ohne_ort):
    assert _offen(ohne_ort) == ["Arbeitsort angegeben"]
    seite = owner_client.get(reverse("auftrag_detail", kwargs={"auftrag_id": ohne_ort.pk}))
    html = seite.content.decode()
    assert "Arbeitsort wählen" in html and "Repository synth-a" in html
    assert "+ Arbeitsort" in html and 'name="ort-name"' in html


def test_vorhandenen_arbeitsort_setzen_ohne_javascript(owner_client, ohne_ort, projekt):
    ort = projekt.arbeitsorte.get()
    antwort = owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "setzen"}),
        {"arbeitsort": ort.pk},
    )
    assert antwort.status_code == 302
    assert _offen(ohne_ort) == []
    assert ohne_ort.aktuelle_revision.arbeitsort == ort
    assert ohne_ort.aktuelle_revision.schreibumfang == "src/export/**"
    assert ohne_ort.revision_nr == 3


def test_plus_arbeitsort_legt_an_und_setzt(owner_client, ohne_ort):
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "neu"}),
        {"ort-name": "Export-Repo", "ort-pfad": r"W:\Synth\Export", "ort-repository": ""},
    )
    ort = Arbeitsort.objects.get(name="Export-Repo")
    assert ort.bestaetigt and ort.vorgeschlagen_von == ""
    assert _offen(ohne_ort) == []
    assert dienst.projekt_fuer_ordner(r"W:\Synth\Export\src")[1] == ort


def test_fremder_arbeitsort_nicht_setzbar(owner_client, ohne_ort, projekt_b):
    fremd = projekt_b.arbeitsorte.get()
    antwort = owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "setzen"}),
        {"arbeitsort": fremd.pk},
    )
    assert antwort.status_code == 404
    assert _offen(ohne_ort) == ["Arbeitsort angegeben"]


def test_ki_vorschlag_braucht_freds_haekchen(client, owner_client, ohne_ort, projekt, pc_zugang):
    ordner = r"W:\Synth\A-Neu"
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_arbeitsort_vorschlagen",
        projekt="synth-a",
        name="Hauptordner",
        pfad=ordner,
        repository="https://github.com/beispiel/synth-a",
        auftrag=ohne_ort.nummer,
    )
    assert not fehler, daten
    assert daten["arbeitsort"]["bestaetigt"] is False
    assert "Arbeitsort bestätigt" in daten["auftrag"]["fuer_freigabe_fehlt"]
    ort = Arbeitsort.objects.get(name="Hauptordner")
    assert ort.vorgeschlagen_von == "Claude Code auf Freds PC"
    # Vorschlag ordnet keinen Ordner zu und lässt keine Freigabe zu.
    assert dienst.projekt_fuer_ordner(ordner) == (None, None)
    freigabe = owner_client.post(
        reverse("auftrag_aktion", kwargs={"auftrag_id": ohne_ort.pk, "aktion": "freigeben"})
    )
    assert freigabe.status_code == 302
    ohne_ort.refresh_from_db()
    assert ohne_ort.zustand == Auftrag.ENTWURF
    # Die Auftragsseite zeigt den Vorschlag mit Häkchen.
    html = owner_client.get(
        reverse("auftrag_detail", kwargs={"auftrag_id": ohne_ort.pk})
    ).content.decode()
    assert "✔ Übernehmen" in html and "Hauptordner" in html and "Verwerfen" in html
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "bestaetigen"}),
        {"arbeitsort": ort.pk, "revision": ort.revision},
    )
    ort.refresh_from_db()
    assert ort.bestaetigt
    assert _offen(ohne_ort) == []
    assert dienst.projekt_fuer_ordner(ordner + r"\src")[0] == projekt


def test_vorschlag_aendern_oder_verwerfen(owner_client, ohne_ort, projekt, pc_zugang):
    ki = dienst.Akteur.aus_bindung(pc_zugang, projekt=projekt)
    ort = dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="Vorschlag", pfad=r"W:\X")
    dienst.arbeitsort_setzen(ki, ohne_ort, ort)
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "aendern"}),
        {
            "arbeitsort": ort.pk,
            "revision": ort.revision,
            "ort-name": "Korrigiert",
            "ort-pfad": r"W:\Y",
        },
    )
    ort.refresh_from_db()
    assert (ort.name, ort.pfad, ort.bestaetigt) == ("Korrigiert", r"W:\Y", True)
    zweiter = dienst.arbeitsort_anlegen(ki, projekt, kennung="z", name="Zweiter", pfad=r"W:\Z")
    dienst.arbeitsort_setzen(ki, ohne_ort, zweiter)
    owner_client.post(
        reverse("auftrag_arbeitsort", kwargs={"auftrag_id": ohne_ort.pk, "wahl": "verwerfen"}),
        {"arbeitsort": zweiter.pk},
    )
    zweiter.refresh_from_db()
    assert not zweiter.aktiv
    assert _offen(ohne_ort) == ["Arbeitsort angegeben"]


def test_wiederholter_vorschlag_ohne_doppel(client, ohne_ort, pc_zugang):
    for _ in range(2):
        fehler, daten = _werkzeug(
            client,
            pc_zugang.klartext,
            "kipm_arbeitsort_vorschlagen",
            projekt="synth-a",
            name="Gleicher Ordner",
            pfad=r"W:\Synth\Gleich",
        )
        assert not fehler, daten
    assert Arbeitsort.objects.filter(pfad=r"W:\Synth\Gleich").count() == 1


def test_eigene_anfrage_kennung_wirkt_nur_einmal(client, ohne_ort, pc_zugang):
    argumente = {"projekt": "synth-a", "name": "Einmal", "anfrage_kennung": "lauf-1"}
    _werkzeug(client, pc_zugang.klartext, "kipm_arbeitsort_vorschlagen", **argumente)
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_arbeitsort_vorschlagen", **argumente
    )
    assert not fehler and daten["wiederholt"] is True
    assert Arbeitsort.objects.filter(name="Einmal").count() == 1


def test_ki_kann_nicht_bestaetigen_und_aenderung_macht_wieder_vorschlag(projekt, pc_zugang):
    ki = dienst.Akteur.aus_bindung(pc_zugang, projekt=projekt)
    ort = projekt.arbeitsorte.get()
    with pytest.raises(dienst.KeinZugriff):
        dienst.arbeitsort_bestaetigen(ki, ort, erwartete_revision=ort.revision)
    dienst.arbeitsort_aendern(ki, ort, erwartete_revision=ort.revision, pfad=r"W:\Umgeleitet")
    ort.refresh_from_db()
    assert not ort.bestaetigt and ort.vorgeschlagen_von == pc_zugang.name
    assert dienst.projekt_fuer_ordner(r"W:\Umgeleitet") == (None, None)
    with pytest.raises(dienst.Konflikt):
        dienst.arbeitsort_aendern(ki, ort, erwartete_revision=1, name="Alt")


def test_projektchat_und_widerrufener_zugang(client, bindung, pc_zugang, projekt):
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_arbeitsort_vorschlagen", name="Nicht erlaubt"
    )
    assert fehler and "Owner oder sein KI-Assistent" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_arbeitsort_aendern",
        projekt="synth-a",
        arbeitsort="repo",
        revision=1,
        aktiv=False,
    )
    assert not fehler and daten["arbeitsort"]["aktiv"] is False
    pc_zugang.widerrufen_am = pc_zugang.angelegt_am
    pc_zugang.save()
    antwort = client.post(
        reverse("mcp_link", kwargs={"schluessel": pc_zugang.klartext}),
        data='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":'
        '"kipm_arbeitsort_vorschlagen","arguments":{"projekt":"synth-a","name":"x"}}}',
        content_type="application/json",
    )
    assert antwort.status_code == 401


def test_fremdes_projekt_ueber_auftragsnummer_gesperrt(
    client, akteur, projekt, projekt_b, pc_zugang
):
    fremd = dienst.wunsch_erfassen(akteur, projekt_b, titel="B", beschreibung="b")
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_arbeitsort_vorschlagen",
        projekt="synth-a",
        name="Querschuss",
        auftrag=fremd.nummer,
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."
    fremd.refresh_from_db()
    assert fremd.revision_nr == 1
    assert not Arbeitsort.objects.filter(name="Querschuss").exists()


def test_einstellungen_vorschlag_uebernehmen_aendern_archivieren(owner_client, projekt, pc_zugang):
    ki = dienst.Akteur.aus_bindung(pc_zugang, projekt=projekt)
    ort = dienst.arbeitsort_anlegen(ki, projekt, kennung="v", name="KI-Ort", pfad=r"W:\K")
    seite = owner_client.get(reverse("projekt_einstellungen", kwargs={"kennung": "synth-a"}))
    assert "Vorschlag von Claude Code auf Freds PC" in seite.content.decode()
    url = "arbeitsort_pflegen"
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": ort.pk, "wahl": "bestaetigen"}),
        {"revision": ort.revision},
    )
    ort.refresh_from_db()
    assert ort.bestaetigt
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": ort.pk, "wahl": "aendern"}),
        {"revision": ort.revision, f"ort{ort.pk}-name": "Umbenannt", f"ort{ort.pk}-pfad": r"W:\K"},
    )
    owner_client.post(
        reverse(url, kwargs={"kennung": "synth-a", "pk": ort.pk, "wahl": "archivieren"})
    )
    ort.refresh_from_db()
    assert (ort.name, ort.aktiv) == ("Umbenannt", False)


def test_einstellungen_fremder_arbeitsort_nicht_gefunden(owner_client, projekt, projekt_b):
    fremd = projekt_b.arbeitsorte.get()
    antwort = owner_client.post(
        reverse(
            "arbeitsort_pflegen",
            kwargs={"kennung": "synth-a", "pk": fremd.pk, "wahl": "archivieren"},
        )
    )
    assert antwort.status_code == 404
    fremd.refresh_from_db()
    assert fremd.aktiv


def test_arbeitsort_setzen_liest_aktuelle_revision_nicht_veraltete_instanz(akteur, projekt):
    """Befund der Browserprüfung: eine veraltete Auftragsinstanz kopierte Revision 1."""
    a = dienst.wunsch_erfassen(akteur, projekt, titel="Veraltet", beschreibung="alt")
    dienst.auftrag_bearbeiten(
        akteur,
        a,
        titel="Veraltet",
        beschreibung="neu",
        ziel="Ziel aus Revision 2",
        nicht_ziele="keine",
        arbeitsort=None,
        schreibumfang="docs/**",
        quellen="",
        abnahmekriterien="K",
        prioritaet="mittel",
        modul=None,
    )
    assert a.revision_nr == 1  # Instanz ist veraltet
    dienst.arbeitsort_setzen(akteur, a, projekt.arbeitsorte.get())
    a.refresh_from_db()
    rev = a.aktuelle_revision
    assert (a.revision_nr, rev.ziel, rev.schreibumfang) == (3, "Ziel aus Revision 2", "docs/**")


def test_neue_arbeitsordner_erklaert_und_verweist_auf_plus_arbeitsort(owner_client, projekt):
    seite = owner_client.get(reverse("verbindungen")).content.decode()
    assert "nur dann ein Eintrag, wenn eine KI aus einem Ordner arbeitet" in seite
    assert "„+ Arbeitsort“" in seite
    ziel = reverse("projekt_einstellungen", kwargs={"kennung": "synth-a"}) + "#arbeitsort-neu"
    assert ziel in seite
    assert 'id="arbeitsort-neu"' in owner_client.get(ziel.split("#")[0]).content.decode()

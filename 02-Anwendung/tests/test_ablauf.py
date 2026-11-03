"""Ablauf ohne Abnahme über HTTP und MCP: geplant anlegen und auf Freds Anweisung im Chat
freigeben in einem Aufruf, Abweisungen, gleiche Abschlussregel, Überführung des Bestands."""

import pytest
from django.utils import timezone

from steuerung import dienst
from steuerung.models import Auftrag, Ereignis, Meldung
from tests.test_api import api, post
from tests.test_mcp import _werkzeug

GEPLANT = {
    "ziel": "Login endet nach höchstens 3 s",
    "nicht_ziele": "keine",
    "arbeitsort": "repo",
    "schreibumfang": ["src/login/**"],
    "abnahmekriterien": ["Spinner verschwindet", "Fehlertext sichtbar"],
}


def _planung(projekt, umfang: str) -> dict:
    """Planung für den Dienst: Texte statt Listen, Arbeitsort als Objekt."""
    return {
        "ziel": GEPLANT["ziel"],
        "nicht_ziele": "keine",
        "arbeitsort": projekt.arbeitsorte.get(),
        "schreibumfang": umfang,
        "abnahmekriterien": "\n".join(GEPLANT["abnahmekriterien"]),
    }


@pytest.fixture
def pc_zugang(akteur):
    b, schluessel = dienst.bindung_anlegen(akteur, None, name="Claude Code auf Freds PC")
    b.klartext = schluessel
    return b


@pytest.mark.django_db
def test_anlegen_planen_und_freigeben_in_einem_aufruf(client, owner, projekt, pc_zugang):
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Login hängt",
        text="Spinner bleibt stehen",
        pruefer=owner.get_username(),
        freigeben=True,
        **GEPLANT,
    )
    assert not fehler, daten
    a = daten["auftrag"]
    assert a["zustand"] == Auftrag.FREIGEGEBEN and a["revision"] == 1 and a["uebernehmbar"]
    assert a["ziel"] == GEPLANT["ziel"] and a["schreibumfang"] == ["src/login/**"]
    assert a["arbeitsort"]["kennung"] == "repo" and a["fuer_freigabe_fehlt"] == []
    vermerk = Ereignis.objects.get(objekt_kennung=a["nummer"], ereignis="Freigegeben")
    assert vermerk.anlass == "freigegeben auf Freds Anweisung im Chat"
    assert vermerk.akteur_name == pc_zugang.name

    # Ohne freigeben bleibt der fertig geplante Auftrag ein Entwurf.
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Später",
        text="noch nicht",
        pruefer=owner.get_username(),
        **(GEPLANT | {"schreibumfang": ["docs/**"]}),
    )
    assert not fehler and daten["auftrag"]["zustand"] == Auftrag.ENTWURF
    assert daten["auftrag"]["fuer_freigabe_fehlt"] == []


@pytest.mark.django_db
def test_freigabe_mit_fehlender_planung_wird_abgewiesen(client, owner, projekt, pc_zugang):
    vorher = Auftrag.objects.count()
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Halb geplant",
        text="x",
        pruefer=owner.get_username(),
        freigeben=True,
        **{k: v for k, v in GEPLANT.items() if k != "abnahmekriterien"},
    )
    assert fehler and "Freigabe nicht möglich" in daten["fehler"]
    assert "Abnahmekriterium" in daten["fehler"]
    assert Auftrag.objects.count() == vorher  # alles oder nichts


@pytest.mark.django_db
def test_freigabe_mit_ueberschneidendem_umfang_wird_abgewiesen(
    client, owner, projekt, freigegeben, pc_zugang
):
    vorher = Auftrag.objects.count()
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Login-Vorlage",
        text="x",
        pruefer=owner.get_username(),
        freigeben=True,
        **(GEPLANT | {"schreibumfang": ["src/auth/login.py"]}),
    )
    assert fehler and f"Überschneidung mit {freigegeben.nummer}" in daten["fehler"]
    assert Auftrag.objects.count() == vorher
    # Auch nachträgliches Freigeben eines geplanten Entwurfs prüft die Sperre.
    entwurf = dienst.auftrag_anlegen(
        dienst.Akteur.aus_bindung(pc_zugang),
        projekt,
        titel="Login-Vorlage",
        planung=_planung(projekt, "src/auth"),
        pruefer=owner,
    )
    fehler, daten = _werkzeug(
        client, pc_zugang.klartext, "kipm_planen", auftrag=entwurf.nummer, freigeben=True
    )
    assert fehler and "Überschneidung" in daten["fehler"]
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.ENTWURF


@pytest.mark.django_db
def test_freigabe_durch_projektfremden_zugang_wird_abgewiesen(
    client, owner, akteur, projekt, bindung, bindung_b
):
    entwurf = dienst.auftrag_anlegen(
        akteur,
        projekt,
        titel="Geplant",
        planung=_planung(projekt, "x/**"),
        pruefer=owner,
    )
    assert dienst.freigabefaehig(entwurf)
    # Schlüssel eines anderen Projekts: der Auftrag existiert für ihn nicht.
    fehler, daten = _werkzeug(
        client, bindung_b.klartext, "kipm_planen", auftrag=entwurf.nummer, freigeben=True
    )
    assert fehler and daten["fehler"] == "Nicht gefunden."
    antwort = post(
        api(bindung_b.klartext),
        f"/api/auftraege/{entwurf.pk}/planen/",
        freigeben=True,
        anfrage_kennung="f1",
    )
    assert antwort.status_code == 404
    with pytest.raises(dienst.KeinZugriff, match="Nicht gefunden"):
        dienst.auftrag_freigeben(dienst.Akteur.aus_bindung(bindung_b), entwurf)
    # Ein Projektschlüssel ist Bearbeiter, nicht Freds Assistent: er gibt nie frei.
    fehler, daten = _werkzeug(
        client, bindung.klartext, "kipm_planen", auftrag=entwurf.nummer, freigeben=True
    )
    assert fehler and "KI-Assistent" in daten["fehler"]
    fehler, daten = _werkzeug(
        client,
        bindung.klartext,
        "kipm_auftrag_anlegen",
        titel="Eigenmächtig",
        text="x",
        freigeben=True,
        **(GEPLANT | {"schreibumfang": ["y/**"]}),
    )
    assert fehler and "KI-Assistent" in daten["fehler"]
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.ENTWURF
    assert not Auftrag.objects.filter(titel="Eigenmächtig").exists()


@pytest.mark.django_db
def test_freigabe_durch_widerrufenen_zugang_wird_abgewiesen(
    client, owner, akteur, projekt, pc_zugang
):
    entwurf = dienst.auftrag_anlegen(
        akteur,
        projekt,
        titel="Geplant",
        planung=_planung(projekt, "x/**"),
        pruefer=owner,
    )
    dienst.bindung_widerrufen(akteur, pc_zugang)
    pc_zugang.refresh_from_db()
    assert not pc_zugang.aktiv
    antwort = post(
        api(pc_zugang.klartext),
        f"/api/auftraege/{entwurf.pk}/planen/",
        freigeben=True,
        anfrage_kennung="w1",
    )
    assert antwort.status_code == 401
    status, _ = _werkzeug_status(client, pc_zugang.klartext, entwurf.nummer)
    assert status == 401
    # Auch der Dienst selbst weist einen widerrufenen Zugang ab.
    with pytest.raises(dienst.KeinZugriff, match="widerrufen"):
        dienst.auftrag_freigeben(dienst.Akteur.aus_bindung(pc_zugang), entwurf)
    entwurf.refresh_from_db()
    assert entwurf.zustand == Auftrag.ENTWURF


def _werkzeug_status(client, schluessel, nummer):
    from tests.test_mcp import _rpc

    antwort = _rpc(
        client,
        schluessel,
        "tools/call",
        {"name": "kipm_planen", "arguments": {"auftrag": nummer, "freigeben": True}},
    )
    return antwort.status_code, antwort


@pytest.mark.django_db
def test_gleiche_abschlussregel_ueber_http_und_mcp(client, owner, akteur, projekt, pc_zugang):
    def freigegebener(titel, umfang):
        a = dienst.auftrag_anlegen(
            akteur,
            projekt,
            titel=titel,
            planung=_planung(projekt, umfang),
            pruefer=owner,
            freigeben=True,
        )
        assert a.zustand == Auftrag.FREIGEGEBEN
        chat = dienst.Akteur.aus_bindung(pc_zugang, projekt=projekt)
        dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
        return a

    # HTTP: eine fehlgeschlagene Prüfung blockiert mit Grund.
    a = freigegebener("HTTP", "a/**")
    c = api(pc_zugang.klartext)
    antwort = post(
        c,
        f"/api/auftraege/{a.pk}/ergebnis/",
        revision=1,
        zusammenfassung="fast",
        dateien=["a/x.py@1"],
        pruefungen=["ruff: sauber"],
        fehlgeschlagen=["pytest: 1 fehlgeschlagen"],
        anfrage_kennung="e1",
    )
    assert antwort.status_code == 200, antwort.json()
    assert antwort.json()["auftrag"]["zustand"] == Auftrag.BLOCKIERT
    assert antwort.json()["ergebnis"]["fehlgeschlagen"] == ["pytest: 1 fehlgeschlagen"]
    assert Meldung.objects.get(auftrag=a, art=Meldung.FEHLER).text.startswith(
        "Prüfung fehlgeschlagen"
    )

    # MCP: ohne Prüfungen erledigt, mit sichtbarem Vermerk; kipm_lesen zeigt ihn.
    b = freigegebener("MCP", "b/**")
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_ergebnis",
        auftrag=b.nummer,
        revision=1,
        zusammenfassung="Doku",
        dateien=["b/README.md@2"],
    )
    assert not fehler and daten["auftrag"]["zustand"] == Auftrag.ERLEDIGT
    assert daten["ergebnis"]["vermerk"] == "ohne automatische Tests"
    fehler, daten = _werkzeug(client, pc_zugang.klartext, "kipm_lesen", auftrag=b.nummer)
    assert daten["auftrag"]["ergebnis"]["vermerk"] == "ohne automatische Tests"

    # MCP: alle bestanden → erledigt ohne Vermerk.
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_ergebnis",
        auftrag=a.nummer,
        revision=1,
        zusammenfassung="behoben",
        dateien=["a/x.py@2"],
        pruefungen=["pytest: grün"],
    )
    assert not fehler and daten["auftrag"]["zustand"] == Auftrag.ERLEDIGT
    assert daten["ergebnis"]["vermerk"] is None


def test_mcp_anweisungen_beschreiben_den_neuen_ablauf():
    from steuerung.mcp import ANWEISUNGEN, WERKZEUGE

    for teil in ("Nutzerabnahme gibt es nicht", "Freds Anweisung", "freigeben=true", "erledigt"):
        assert teil.lower() in ANWEISUNGEN.lower(), teil
    assert "ohne automatische Tests" in ANWEISUNGEN
    assert "Abnehmen macht Fred" not in ANWEISUNGEN
    ergebnis = next(w for w in WERKZEUGE if w[0] == "kipm_ergebnis")
    assert "fehlgeschlagen" in ergebnis[2] and "pruefungen" not in ergebnis[3]
    anlegen = next(w for w in WERKZEUGE if w[0] == "kipm_auftrag_anlegen")
    assert {"ziel", "schreibumfang", "abnahmekriterien", "freigeben"} <= set(anlegen[2])


def test_migration_0008_ueberfuehrt_bestand(transactional_db):
    """0007 → 0008: Zur Prüfung und Abgenommen werden Erledigt, Nacharbeit Blockiert mit Grund,
    Klärung Entwurf; Revisionen, Ergebnisse und Prüfungen bleiben, jede Umstellung im Verlauf."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate([("steuerung", "0007_meldevertrag")])
    alt = executor.loader.project_state([("steuerung", "0007_meldevertrag")]).apps
    User = alt.get_model("auth", "User")
    Projekt = alt.get_model("steuerung", "Projekt")
    AuftragAlt = alt.get_model("steuerung", "Auftrag")
    Revision = alt.get_model("steuerung", "Revision")
    Bindung = alt.get_model("steuerung", "ChatBindung")
    ErgebnisAlt = alt.get_model("steuerung", "Ergebnis")
    PruefungAlt = alt.get_model("steuerung", "Pruefung")
    fred = User.objects.create(username="fred-alt", is_active=True)
    p = Projekt.objects.create(kennung="bestand", name="Bestand")
    chat = Bindung.objects.create(
        name="Chat X", praefix="p0008", schluessel_hash="h", gueltig_bis=timezone.now()
    )

    def auftrag(titel, zustand, mit_bearbeiter=False, **mehr):
        a = AuftragAlt.objects.create(
            projekt=p,
            titel=titel,
            zustand=zustand,
            revision_nr=2,
            bearbeiter_bindung=chat if mit_bearbeiter else None,
            bearbeiter_name="Chat X" if mit_bearbeiter else "",
            **mehr,
        )
        Revision.objects.create(auftrag=a, nummer=2, ziel="Z", erstellt_von_name="fred")
        return a

    zur_pruefung = auftrag("Wie A-2", "ZUR_PRUEFUNG", mit_bearbeiter=True)
    e = ErgebnisAlt.objects.create(
        auftrag=zur_pruefung, revision_nr=2, zusammenfassung="S", eingereicht_von_name="Chat X"
    )
    abgenommen = auftrag("Fertig", "ABGENOMMEN", abgenommene_revision=2)
    nacharbeit = auftrag("Nachbessern", "NACHARBEIT", mit_bearbeiter=True)
    e2 = ErgebnisAlt.objects.create(
        auftrag=nacharbeit, revision_nr=2, zusammenfassung="S", eingereicht_von_name="Chat X"
    )
    pr = PruefungAlt.objects.create(
        ergebnis=e2, pruefer=fred, entscheidung="NACHARBEIT", begruendung="Kriterium 2 offen"
    )
    klaerung = auftrag("Unklar", "KLAERUNG_ERFORDERLICH", freigegeben_am=timezone.now())
    pausiert = auftrag("Pause", "PAUSIERT", mit_bearbeiter=True, zustand_vor_pause="IN_ARBEIT")
    unberuehrt = auftrag("Läuft", "IN_ARBEIT", mit_bearbeiter=True)

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    from steuerung.models import Auftrag as A
    from steuerung.models import Ereignis as E
    from steuerung.models import Meldung as M
    from steuerung.models import Pruefung as P

    def neu(a):
        return A.objects.get(pk=a.pk)

    assert neu(zur_pruefung).zustand == "ERLEDIGT"
    assert not neu(zur_pruefung).hat_bearbeiter
    assert neu(zur_pruefung).letzter_bearbeiter_bindung_id == chat.pk
    assert neu(zur_pruefung).abgenommene_revision == 2
    assert neu(zur_pruefung).ergebnisse.get().pk == e.pk
    assert neu(abgenommen).zustand == "ERLEDIGT"
    assert neu(nacharbeit).zustand == "BLOCKIERT"
    assert neu(nacharbeit).bearbeiter_bindung_id == chat.pk
    grund = M.objects.get(auftrag_id=nacharbeit.pk)
    assert grund.art == "HINDERNIS" and "Kriterium 2 offen" in grund.text
    assert P.objects.get(pk=pr.pk).begruendung == "Kriterium 2 offen"
    assert neu(klaerung).zustand == "ENTWURF" and neu(klaerung).freigegeben_am is None
    assert neu(pausiert).zustand == "BLOCKIERT" and neu(pausiert).zustand_vor_pause == ""
    assert neu(unberuehrt).zustand == "IN_ARBEIT"
    assert A.objects.count() == 6 and all(neu(x).revision_nr == 2 for x in (abgenommen, klaerung))
    umgestellt = E.objects.filter(akteur_name="Umstellung 0008")
    assert umgestellt.count() == 5
    assert umgestellt.filter(
        objekt_kennung=f"A-{zur_pruefung.pk}", ereignis="Zustand umgestellt: Zur Prüfung → Erledigt"
    ).exists()
    assert not A.objects.filter(
        zustand__in=(
            "ZUR_PRUEFUNG",
            "NACHARBEIT",
            "ABGENOMMEN",
            "KLAERUNG_ERFORDERLICH",
            "PAUSIERT",
        )
    ).exists()


@pytest.mark.django_db
def test_anlegen_prueft_planung_und_freigabe_nur_aus_entwurf(
    client, akteur, owner, projekt, projekt_b, freigegeben, pc_zugang
):
    with pytest.raises(dienst.DienstFehler, match="Arbeitsort gehört nicht"):
        dienst.auftrag_anlegen(akteur, projekt, titel="X", planung=_planung(projekt_b, "x/**"))
    with pytest.raises(dienst.DienstFehler, match="Unbekanntes Planungsfeld: bearbeiter"):
        dienst.auftrag_anlegen(akteur, projekt, titel="X", planung={"bearbeiter": "jemand"})
    with pytest.raises(dienst.Konflikt, match="aus dem Zustand „Freigegeben“"):
        dienst.auftrag_freigeben(akteur, freigegeben)
    fehler, daten = _werkzeug(
        client,
        pc_zugang.klartext,
        "kipm_auftrag_anlegen",
        projekt=projekt.kennung,
        titel="Mit falschem Prüfer",
        text="x",
        pruefer="gibt-es-nicht",
        **GEPLANT,
    )
    assert fehler and "gibt-es-nicht" in daten["fehler"]
    assert not Auftrag.objects.filter(titel="Mit falschem Prüfer").exists()

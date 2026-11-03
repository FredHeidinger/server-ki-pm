"""Regressionen aus der unabhängigen Gegenprüfung der Projektakte."""

import io
import zipfile
import zlib
from datetime import timedelta

import pytest
from django.utils import timezone

from steuerung import abgleich, ablage, akte, dienst
from steuerung.models import ExterneAktion, Installation
from tests.conftest import arbeitspaket
from tests.test_ablage import pdf
from tests.test_github import Attrappe, _bestaetigt

pytestmark = pytest.mark.django_db


def test_abnahme_prueft_einreicher_nicht_den_buchenden(akteur, projekt, bindung, zweiter_mensch):
    """Chat reicht ein, Fred bucht und nimmt ab: erlaubt. Fred reicht ein, ein anderer bucht:
    Fred darf nicht abnehmen."""
    f = akte.funktion_anlegen(akteur, projekt, titel="Export")
    a = arbeitspaket(akteur, projekt, pruefer=zweiter_mensch)
    akte.funktion_auftrag(akteur, f, a, verbinden=True)
    dienst.auftrag_freigeben(akteur, a)
    chat = dienst.Akteur.aus_bindung(bindung)
    dienst.auftrag_uebernehmen(chat, a, a.revision_nr)
    a.refresh_from_db()
    e = dienst.ergebnis_einreichen(
        chat, a, a.revision_nr, zusammenfassung="z", dateien="x.py@abc1", pruefungen="pytest ok"
    )
    akte.ergebnis_als_nachweis(akteur, e, f)
    n = akte.funktion_abnehmen(akteur, f, version="abc1", angenommen=True, kriterien="geprüft")
    assert n.ergebnis == "angenommen"

    g = akte.funktion_anlegen(akteur, projekt, titel="Import")
    b = arbeitspaket(akteur, projekt, titel="Import bauen", umfang="imp/**", pruefer=zweiter_mensch)
    akte.funktion_auftrag(akteur, g, b, verbinden=True)
    dienst.auftrag_freigeben(akteur, b)
    dienst.auftrag_uebernehmen(akteur, b, b.revision_nr)
    b.refresh_from_db()
    e2 = dienst.ergebnis_einreichen(
        akteur, b, b.revision_nr, zusammenfassung="z", dateien="i.py@def2", pruefungen="pytest ok"
    )
    pc, _ = dienst.bindung_anlegen(akteur, None, name="Assistent")
    akte.ergebnis_als_nachweis(dienst.Akteur.aus_bindung(pc, projekt=projekt), e2, g)
    with pytest.raises(dienst.KeinZugriff, match="Selbstabnahme"):
        akte.funktion_abnehmen(akteur, g, version="def2", angenommen=True, kriterien="x")


def test_rueckdatierte_installation_verdraengt_neueren_stand_nicht(akteur, projekt):
    u = akte.umgebung_anlegen(akteur, projekt, name="Test", art="test")
    neu = akte.installation_melden(
        akteur, projekt, umgebung=u, komponente="App", version="1.3", herkunft="heute"
    )
    alt = akte.installation_melden(
        akteur,
        projekt,
        umgebung=u,
        komponente="App",
        version="1.2",
        installiert_am=timezone.now() - timedelta(days=1),
        herkunft="nachgetragen",
    )
    neu.refresh_from_db()
    alt.refresh_from_db()
    assert neu.zustand == Installation.GEMELDET and alt.zustand == Installation.ERSETZT
    assert [z["i"].version for z in akte.installationen_aktuell(projekt)] == ["1.3"]
    assert "Nachgetragen" in alt.bemerkung


def test_bewertung_setzt_ersetzte_installation_nicht_zurueck(akteur, projekt):
    u = akte.umgebung_anlegen(akteur, projekt, name="Test", art="test")
    i = akte.installation_melden(
        akteur, projekt, umgebung=u, komponente="App", version="1", herkunft="h"
    )
    veraltet = Installation.objects.get(pk=i.pk)
    akte.installation_melden(
        akteur, projekt, umgebung=u, komponente="App", version="2", herkunft="h"
    )
    with pytest.raises(dienst.Konflikt):
        akte.installation_bewerten(akteur, veraltet, bestaetigt=True)
    assert Installation.objects.get(pk=i.pk).zustand == Installation.ERSETZT


def test_link_ohne_ki_freigabe_nicht_im_kontext(akteur, projekt):
    akte.link_anlegen(akteur, projekt, titel="Ablage", uri="https://cloud.example/s/abc?x=1")
    assert "cloud.example" not in akte.kontext_erzeugen(akteur, projekt).vollstaendig_text


def test_teilung_haelt_datenbloecke_geschlossen(akteur, projekt):
    a = arbeitspaket(akteur, projekt)
    dienst.auftrag_bearbeiten(
        akteur,
        a,
        titel=a.titel,
        beschreibung="",
        ziel="Zeile Ziel.\n" * 3000,
        nicht_ziele="keine",
        arbeitsort=projekt.arbeitsorte.first(),
        schreibumfang="src/**",
        quellen="",
        abnahmekriterien="K",
        prioritaet="mittel",
        modul=None,
    )
    a.refresh_from_db()
    k = akte.kontext_erzeugen(akteur, projekt, auftrag=a, budget=12_000)
    assert len(k.teile) >= 3
    for teil in k.teile:
        assert teil.count("```") % 2 == 0, teil[-200:]
        assert len(teil) <= 12_000 + 700
    assert k.vollstaendig_text.count("Zeile Ziel.") == 3000


def test_kontextteil_bleibt_gleich_trotz_tageswechsel(akteur, projekt, monkeypatch):
    u = akte.umgebung_anlegen(akteur, projekt, name="Test", art="test")
    akte.installation_melden(
        akteur,
        projekt,
        umgebung=u,
        komponente="App",
        version="1",
        installiert_am=timezone.now() - timedelta(days=2),
        herkunft="h",
    )
    k = akte.kontext_erzeugen(akteur, projekt)
    spaeter = timezone.now() + timedelta(days=3)
    monkeypatch.setattr(timezone, "now", lambda: spaeter)
    k2, _ = akte.kontext_teil(akteur, projekt, k.kontext_id, 1)
    assert k2.hash == k.hash


@pytest.mark.parametrize(
    "inhalt",
    [
        pdf().replace(
            b"/Type /Catalog", b"/Type /Catalog /OpenAction << /S /J#61vaScript /JS<6162> >>"
        ),
        pdf().replace(b"/Type /Catalog", b"/Type /Catalog /AA << /O 9 0 R >>")
        + b"\n9 0 obj\n<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode >>\nstream\n"
        + zlib.compress(b"<< /S /JavaScript /JS (app.alert(1)) >>")
        + b"\nendstream\nendobj\n",
    ],
)
def test_pdf_aktive_inhalte_auch_verschleiert(tmp_path, inhalt):
    datei = tmp_path / "x.pdf"
    datei.write_bytes(inhalt)
    with pytest.raises(ablage.Abgewiesen, match="aktive Inhalte"):
        ablage.inhalt_pruefen(datei, ".pdf")


def test_docx_externe_beziehung_in_einfachen_anfuehrungszeichen(tmp_path):
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document xmlns:w='x'/>")
        z.writestr(
            "word/_rels/settings.xml.rels",
            "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
            "<Relationship Id='r1' Type='http://x/attachedTemplate' Target='http://boese/t.dotm'"
            " TargetMode='External'/></Relationships>",
        )
    datei = tmp_path / "x.docx"
    datei.write_bytes(puffer.getvalue())
    with pytest.raises(ablage.Abgewiesen, match="externe Inhalte"):
        ablage.inhalt_pruefen(datei, ".docx")


def test_teilweise_ausgefuehrte_aktion_nicht_aenderbar(akteur, projekt):
    gh = Attrappe()
    from steuerung import github

    github.adapter_setzen(gh)
    try:
        v = abgleich.verbindung_anlegen(
            akteur,
            projekt,
            name="fred/test",
            schreibend=True,
            aktionen="dokument_pr",
            pfade="docs/**",
        )
        a = _bestaetigt(
            akteur,
            abgleich.aktion_vorbereiten(
                akteur, v, "dokument_pr", {"pfad": "docs/a.md", "inhalt": "eins\n"}
            ),
        )
        gh.fehler["pr_anlegen"] = github.GitHubFehler(422, "kaputt")
        abgleich.aktion_ausfuehren(akteur, a)
        a.refresh_from_db()
        assert a.zustand == ExterneAktion.TEILWEISE
        ExterneAktion.objects.filter(pk=a.pk).update(zustand=ExterneAktion.KONFLIKT)
        with pytest.raises(dienst.Konflikt, match="neue Aktion"):
            abgleich.aktion_aendern(
                akteur, a, {"pfad": "docs/a.md", "inhalt": "zwei\n"}, erwartete_revision=a.revision
            )
    finally:
        github.adapter_setzen(None)


def _mit_strom(woerterbuch: bytes, daten: bytes) -> bytes:
    return pdf() + b"\n9 0 obj\n" + woerterbuch + b"\nstream\n" + daten + b"\nendstream\nendobj\n"


def test_pdf_bildstroeme_mit_zufallsbytes_bleiben_erlaubt(tmp_path):
    """Bild- und Inhaltsströme werden nicht durchsucht: Zufallsbytes wie /JS sind kein Befund."""
    bild = zlib.compress(b"\x00/JS \x01" * 1000 + b"/JavaScript ")
    datei = tmp_path / "scan.pdf"
    datei.write_bytes(
        _mit_strom(b"<< /Type /XObject /Subtype /Image /Filter /FlateDecode >>", bild)
    )
    assert ablage.inhalt_pruefen(datei, ".pdf") == "application/pdf"


def test_pdf_scheinstrom_mit_javascript_abgewiesen(tmp_path):
    """Objekt hinter einem auskommentierten „stream“: ein Leser sieht es, der Scan auch."""
    inhalt = pdf().replace(b"/Type /Catalog", b"/Type /Catalog /OpenAction 9 0 R") + (
        b"\n%stream\n9 0 obj<</S/JavaScript/JS(app.alert(1))>>endobj\n%endstream\n"
    )
    datei = tmp_path / "x.pdf"
    datei.write_bytes(inhalt)
    with pytest.raises(ablage.Abgewiesen, match="aktive Inhalte"):
        ablage.inhalt_pruefen(datei, ".pdf")


def test_pdf_eingebettete_datei_nur_ueber_struktur_erkannt(tmp_path):
    """/EF ohne /Type /EmbeddedFile findet nur die Strukturprüfung im Kindprozess."""
    inhalt = pdf().replace(b"/Type /Catalog", b"/Type /Catalog /Anhang << /EF << /F 5 0 R >> >>")
    datei = tmp_path / "x.pdf"
    datei.write_bytes(inhalt)
    with pytest.raises(ablage.Abgewiesen, match="aktive Inhalte"):
        ablage.inhalt_pruefen(datei, ".pdf")


def test_pdf_unlesbar_wird_nicht_als_unauffaellig_gewertet(tmp_path):
    datei = tmp_path / "x.pdf"
    datei.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 50)
    with pytest.raises(ablage.Abgewiesen, match="nicht prüfen"):
        ablage.inhalt_pruefen(datei, ".pdf")


def test_pdf_verschluesselt_nicht_pruefbar(tmp_path):
    datei = tmp_path / "x.pdf"
    datei.write_bytes(pdf().replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 7 0 R"))
    with pytest.raises(ablage.Abgewiesen, match="verschlüsselt"):
        ablage.inhalt_pruefen(datei, ".pdf")


def test_nachtrag_nach_fehlgeschlagener_installation_wird_nicht_aktuell(akteur, projekt):
    u = akte.umgebung_anlegen(akteur, projekt, name="Test", art="test")
    jetzt = timezone.now()
    akte.installation_melden(
        akteur,
        projekt,
        umgebung=u,
        komponente="App",
        version="1.2",
        installiert_am=jetzt - timedelta(days=2),
        herkunft="h",
    )
    neu = akte.installation_melden(
        akteur,
        projekt,
        umgebung=u,
        komponente="App",
        version="1.3",
        installiert_am=jetzt - timedelta(days=1),
        herkunft="h",
    )
    akte.installation_bewerten(akteur, neu, bestaetigt=False)
    alt = akte.installation_melden(
        akteur,
        projekt,
        umgebung=u,
        komponente="App",
        version="1.1",
        installiert_am=jetzt - timedelta(days=3),
        herkunft="nachgetragen",
    )
    alt.refresh_from_db()
    assert alt.zustand == Installation.ERSETZT
    assert "1.1" not in [z["i"].version for z in akte.installationen_aktuell(projekt)]


def test_veraltet_haengt_am_kontextzeitpunkt(akteur, projekt, monkeypatch):
    f = akte.funktion_anlegen(akteur, projekt, titel="Export")
    alt = timezone.now() - timedelta(days=akte.VERALTET_TAGE - 1)
    akte.nachweis_melden(
        akteur,
        projekt,
        art="umsetzung",
        ergebnis="gemeldet",
        version="v1",
        quelle="q",
        funktion=f,
        beobachtet_am=alt,
    )
    akte.nachweis_melden(
        akteur,
        projekt,
        art="test",
        ergebnis="bestanden",
        version="v1",
        quelle="q",
        umfang="u",
        funktion=f,
        beobachtet_am=alt,
    )
    k = akte.kontext_erzeugen(akteur, projekt)
    spaeter = timezone.now() + timedelta(days=5)
    monkeypatch.setattr(timezone, "now", lambda: spaeter)
    k2, _ = akte.kontext_teil(akteur, projekt, k.kontext_id, 1)
    assert k2.hash == k.hash


def _strukturpruefung(tmp_path, anpassen):
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    from steuerung import extrakt

    w = PdfWriter()
    seite = w.add_blank_page(200, 200)
    anpassen(w, seite, ArrayObject, DictionaryObject, NameObject, TextStringObject)
    datei = tmp_path / "s.pdf"
    w.write(datei)
    return extrakt.pdf_pruefen(str(datei))


def test_struktur_indirekter_aktionsname(tmp_path):
    def anpassen(w, seite, ArrayObject, DictionaryObject, NameObject, TextStringObject):
        start = w._add_object(NameObject("/Launch"))
        aktion = DictionaryObject(
            {NameObject("/S"): start, NameObject("/F"): TextStringObject("x")}
        )
        w._root_object[NameObject("/OpenAction")] = w._add_object(aktion)

    ergebnis = _strukturpruefung(tmp_path, anpassen)
    assert not ergebnis["ok"] and "Launch" in ergebnis["grund"]


def test_struktur_feld_nur_ueber_parent_erreichbar(tmp_path):
    def anpassen(w, seite, ArrayObject, DictionaryObject, NameObject, TextStringObject):
        feld = w._add_object(
            DictionaryObject(
                {
                    NameObject("/FT"): NameObject("/Tx"),
                    NameObject("/AA"): DictionaryObject(),
                }
            )
        )
        widget = w._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Annot"),
                    NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/Parent"): feld,
                }
            )
        )
        seite[NameObject("/Annots")] = ArrayObject([widget])

    ergebnis = _strukturpruefung(tmp_path, anpassen)
    assert not ergebnis["ok"] and "/AA" in ergebnis["grund"]


def test_struktur_normales_pdf_unauffaellig(tmp_path):
    ergebnis = _strukturpruefung(tmp_path, lambda *a: None)
    assert ergebnis == {"ok": True, "grund": ""}

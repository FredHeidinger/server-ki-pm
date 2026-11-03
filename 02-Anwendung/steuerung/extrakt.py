"""Textauszug aus PDF, DOCX oder XLSX in einem eigenen Prozess (``python -I extrakt.py PFAD TYP``).

Liest nur, führt nichts aus: keine Makros, keine Formeln (nur zwischengespeicherte Werte), keine
eingebetteten Objekte. Gibt JSON mit ``zustand`` (vollstaendig, teilweise, nicht_auswertbar),
``text`` mit Seiten- oder Blattbezug und ``hinweis`` aus. Ressourcengrenzen setzt der Aufrufer.
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

GRENZE = 200_000
TEILGRENZE = 8 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xml(z: zipfile.ZipFile, name: str):
    with z.open(name) as f:
        daten = f.read(TEILGRENZE + 1)
    if len(daten) > TEILGRENZE:
        raise ValueError("Dokumentteil zu groß")
    if b"<!DOCTYPE" in daten or b"<!ENTITY" in daten:
        raise ValueError("DTD im Dokument")
    return ET.fromstring(daten)


def docx(pfad: str) -> dict:
    with zipfile.ZipFile(pfad) as z:
        wurzel = _xml(z, "word/document.xml")
    absaetze = []
    for p in wurzel.iter(W + "p"):
        text = "".join(t.text or "" for t in p.iter(W + "t"))
        if text.strip():
            absaetze.append(text)
    text = "\n".join(absaetze)
    return {
        "zustand": "vollstaendig" if text else "nicht_auswertbar",
        "text": text,
        "hinweis": "" if text else "kein Text gefunden",
    }


def xlsx(pfad: str) -> dict:
    with zipfile.ZipFile(pfad) as z:
        namen = z.namelist()
        gemeinsam = []
        if "xl/sharedStrings.xml" in namen:
            for si in _xml(z, "xl/sharedStrings.xml").iter(S + "si"):
                gemeinsam.append("".join(t.text or "" for t in si.iter(S + "t")))
        blaetter = sorted(
            (n for n in namen if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        teile = []
        for nr, name in enumerate(blaetter, start=1):
            zeilen = []
            for zeile in _xml(z, name).iter(S + "row"):
                werte = []
                for zelle in zeile.iter(S + "c"):
                    v = zelle.find(S + "v")
                    if v is None or v.text is None:
                        inline = zelle.find(S + "is")
                        werte.append(
                            "".join(t.text or "" for t in inline.iter(S + "t"))
                            if inline is not None
                            else ""
                        )
                    elif zelle.get("t") == "s":
                        i = int(v.text)
                        werte.append(gemeinsam[i] if i < len(gemeinsam) else "")
                    else:
                        werte.append(v.text)
                if any(w.strip() for w in werte):
                    zeilen.append("\t".join(werte))
            teile.append(f"[Blatt {nr}]\n" + "\n".join(zeilen))
    text = "\n\n".join(teile)
    return {
        "zustand": "vollstaendig" if text.strip() else "nicht_auswertbar",
        "text": text,
        "hinweis": "Formeln nicht ausgewertet, nur gespeicherte Werte",
    }


def pdf(pfad: str) -> dict:
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"zustand": "nicht_auswertbar", "text": "", "hinweis": "PDF-Leser nicht installiert"}
    leser = PdfReader(pfad)
    seiten, leer = [], 0
    for nr, seite in enumerate(leser.pages, start=1):
        text = (seite.extract_text() or "").strip()
        if not text:
            leer += 1
        seiten.append(f"[Seite {nr}]\n{text}")
    gesamt = len(leser.pages)
    if gesamt and leer == gesamt:
        return {
            "zustand": "nicht_auswertbar",
            "text": "",
            "hinweis": "keine Textebene (vermutlich gescannt); OCR folgt später",
        }
    zustand = "teilweise" if leer else "vollstaendig"
    hinweis = f"{leer} von {gesamt} Seiten ohne Text" if leer else ""
    return {"zustand": zustand, "text": "\n\n".join(seiten), "hinweis": hinweis}


AKTIVE_SCHLUESSEL = {"/JS", "/JavaScript", "/EmbeddedFiles", "/EF", "/RichMedia", "/XFA", "/AA"}
AKTIVE_AKTIONEN = {
    "/JavaScript",
    "/Launch",
    "/GoToE",
    "/ImportData",
    "/RichMediaExecute",
    "/SubmitForm",
}
MAX_OBJEKTE = 200_000


def pdf_pruefen(pfad: str) -> dict:
    """Struktur so lesen wie ein PDF-Leser (Objektströme, Filter, Prädiktoren, reparierte
    Querverweise) und alle vom Katalog aus erreichbaren Objekte auf aktive Inhalte prüfen.
    Nicht lesbar oder verschlüsselt heißt: nicht prüfbar."""
    try:
        from pypdf import PdfReader
        from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject
    except ImportError:
        return {"ok": False, "grund": "PDF-Prüfung nicht installiert"}
    try:
        leser = PdfReader(pfad, strict=False)
        if leser.is_encrypted:
            return {"ok": False, "grund": "verschlüsselt"}
        stapel = [leser.trailer["/Root"]]
        gesehen: set[tuple[int, int]] = set()
        besucht = 0
        while stapel:
            obj = stapel.pop()
            if isinstance(obj, IndirectObject):
                schluessel = (obj.idnum, obj.generation)
                if schluessel in gesehen:
                    continue
                gesehen.add(schluessel)
                obj = obj.get_object()
            besucht += 1
            if besucht > MAX_OBJEKTE:
                return {"ok": False, "grund": "zu viele Objekte"}
            if isinstance(obj, NameObject):
                # Auch indirekt abgelegte Namen (z. B. /S 5 0 R mit 5 = /Launch) zählen.
                if obj in AKTIVE_AKTIONEN or obj == "/EmbeddedFile":
                    return {"ok": False, "grund": f"Aktion {obj}"}
            elif isinstance(obj, DictionaryObject):
                for name in obj:
                    if name in AKTIVE_SCHLUESSEL:
                        return {"ok": False, "grund": f"aktiver Inhalt {name}"}
                # dict.values() löst Verweise nicht auf; aufgelöst wird beim Besuch. /Parent
                # wird mit durchlaufen (Formularfelder erben Aktionen über die Elternkette).
                stapel.extend(obj.values())
            elif isinstance(obj, ArrayObject):
                stapel.extend(obj)
    except Exception as fehler:  # noqa: BLE001 - jeder Lesefehler heißt „nicht prüfbar“
        return {"ok": False, "grund": f"nicht lesbar ({type(fehler).__name__})"}
    return {"ok": True, "grund": ""}


def main() -> None:
    if sys.argv[2] == "pdf-pruefen":
        sys.stdout.buffer.write(json.dumps(pdf_pruefen(sys.argv[1])).encode("ascii"))
        return
    pfad, typ = sys.argv[1], sys.argv[2]
    try:
        if typ == "application/pdf":
            ergebnis = pdf(pfad)
        elif typ.endswith("wordprocessingml.document"):
            ergebnis = docx(pfad)
        elif typ.endswith("spreadsheetml.sheet"):
            ergebnis = xlsx(pfad)
        else:
            ergebnis = {
                "zustand": "nicht_auswertbar",
                "text": "",
                "hinweis": "Typ nicht auswertbar",
            }
    except Exception as fehler:  # noqa: BLE001 - jedes Lesefehlerbild wird kontrolliert gemeldet
        ergebnis = {
            "zustand": "nicht_auswertbar",
            "text": "",
            "hinweis": f"Lesefehler: {type(fehler).__name__}",
        }
    if len(ergebnis["text"]) > GRENZE:
        ergebnis = {
            **ergebnis,
            "zustand": "teilweise",
            "text": ergebnis["text"][:GRENZE],
            "hinweis": (ergebnis.get("hinweis", "") + f"; gekürzt auf {GRENZE} Zeichen").strip(
                "; "
            ),
        }
    # -I ignoriert PYTHONIOENCODING; ASCII-JSON als Bytes ist unabhängig von der Codepage.
    sys.stdout.buffer.write(json.dumps(ergebnis).encode("ascii"))


if __name__ == "__main__":
    main()

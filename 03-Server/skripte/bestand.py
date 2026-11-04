"""Bestandszahlen für die Probemigration in installieren.sh (nur Standardbibliothek).

``zaehlen <db>``: Integrität prüfen und Zeilenzahlen aller Bestandstabellen ausgeben; auch die
Tabellen früherer Stufen, die kein Code mehr nutzt, deren Daten aber bleiben. Eine Tabelle, die es
in der Datenbank noch nicht gibt, zählt 0. ``vergleichen "<vorher>" "<nachher>"``: Exit 0, wenn
nichts verloren ging. Verlauf (``ereignis``) und Meldungen (``meldung``) sind nur anhängend und
dürfen wachsen, etwa wenn eine Migration eine Umstellung festhält (0008); alle anderen Zahlen müssen
gleich bleiben. Sonst Exit 4 mit der betroffenen Tabelle.
"""

import sqlite3
import sys

TABELLEN = (
    "projekt",
    "modul",
    "arbeitsort",
    "chatbindung",
    "ordneranfrage",
    "anfragekennung",
    "auftrag",
    "revision",
    "abhaengigkeit",
    "meldung",
    "ergebnis",
    "pruefung",
    "kriteriumsbewertung",
    "entscheidung",
    "uebernahmebestaetigung",
    "ereignis",
    "objektversion",
    "kontextbezug",
    "meilenstein",
    "funktion",
    "umgebung",
    "release",
    "releasekomponente",
    "installation",
    "nachweis",
    "unterlage",
    "unterlagenversion",
    "unterlagenzuordnung",
    "repoverbindung",
    "abrufstand",
    "externesobjekt",
    "externeaktion",
    "besuchsmarke",
)
NUR_ANHAENGEND = {"ereignis", "meldung"}


def zaehlen(pfad) -> list[int]:
    with sqlite3.connect(pfad) as z:
        if z.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("Datenbank nicht integer")
        vorhanden = {
            zeile[0] for zeile in z.execute("select name from sqlite_master where type = 'table'")
        }
        return [
            z.execute(f"select count(*) from steuerung_{t}").fetchone()[0]
            if f"steuerung_{t}" in vorhanden
            else 0
            for t in TABELLEN
        ]


def abweichungen(vorher: list[int], nachher: list[int]) -> list[str]:
    return [
        f"{t} {v} → {n}"
        for t, v, n in zip(TABELLEN, vorher, nachher, strict=True)
        if (n < v if t in NUR_ANHAENGEND else n != v)
    ]


def ohne_verlust(vorher: list[int], nachher: list[int]) -> bool:
    return not abweichungen(vorher, nachher)


if __name__ == "__main__":
    if sys.argv[1:2] == ["zaehlen"]:
        print(" ".join(map(str, zaehlen(sys.argv[2]))))
    elif sys.argv[1:2] == ["vergleichen"]:
        vorher, nachher = ([int(x) for x in s.split()] for s in sys.argv[2:4])
        fehler = abweichungen(vorher, nachher)
        if fehler:
            print("Bestand verändert: " + "; ".join(fehler))
            sys.exit(4)
    else:
        sys.exit("Aufruf: bestand.py zaehlen <db> | vergleichen <vorher> <nachher>")

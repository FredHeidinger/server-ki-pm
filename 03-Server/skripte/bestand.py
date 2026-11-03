"""Bestandszahlen für die Probemigration in installieren.sh (nur Standardbibliothek).

``zaehlen <db>``: Integrität prüfen und Zeilenzahlen der Kerntabellen ausgeben.
``vergleichen "<vorher>" "<nachher>"``: Exit 0, wenn nichts verloren ging. Der Verlauf
(``ereignis``) ist nur anhängend und darf wachsen, etwa wenn eine Migration eine Umstellung
festhält; alle anderen Zahlen müssen gleich bleiben. Sonst Exit 4 mit der betroffenen Tabelle.
"""

import sqlite3
import sys

TABELLEN = ("projekt", "auftrag", "revision", "ergebnis", "pruefung", "ereignis", "chatbindung")
NUR_ANHAENGEND = {"ereignis"}


def zaehlen(pfad) -> list[int]:
    with sqlite3.connect(pfad) as z:
        if z.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("Datenbank nicht integer")
        return [z.execute(f"select count(*) from steuerung_{t}").fetchone()[0] for t in TABELLEN]


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

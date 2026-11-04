"""Stellt in tests/test_kopieren.py das Programm age dar: -r verschlüsselt (kennzeichnet nur), -d entschlüsselt.

age selbst läuft erst in der Abnahme auf dem Server; hier geht es um Aufruf und Ablauf.
"""

import sys
from pathlib import Path

KOPF = b"AGE-NACHBILDUNG\n"


def wert(name: str) -> str:
    return sys.argv[sys.argv.index(name) + 1]


if __name__ == "__main__":
    ausgabe, eingabe = Path(wert("-o")), Path(sys.argv[-1])
    if "-d" in sys.argv:
        if wert("-i") != "-" or "RICHTIG" not in sys.stdin.read():
            sys.exit(1)
        ausgabe.write_bytes(eingabe.read_bytes().removeprefix(KOPF))
    else:
        ausgabe.write_bytes(KOPF + eingabe.read_bytes())

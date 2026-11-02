# Einen Projektchat anbinden

## Einmal je Projekt (du, im Browser)

1. In Server-KI-PM die Projektseite öffnen, unter „Chat-Zugänge“ einen Zugang anlegen. Der Schlüssel wird nur einmal angezeigt.
2. Im Projektordner des Chats (dort, wo der Chat arbeitet) eine Datei `.kipm` anlegen, mit einem Editor, nicht im Chat:

```ini
[kipm]
url = https://192.168.254.157/
schluessel = HIER-DEN-SCHLUESSEL-EINFUEGEN
ca = kipm-root.crt
```

3. `.kipm` und `kipm-root.crt` in die `.gitignore` des Projekts eintragen, damit der Schlüssel nie ins Repository gelangt.

## Im Projektchat (diesen Text einfügen)

```text
Du bist über Server-KI-PM an die zentrale Auftragssteuerung angebunden. Die Datei .kipm im Projektordner enthält Adresse und Zugang; lies sie nicht vor und kopiere sie nirgendwohin.

Einrichtung (einmalig in diesem Chat, im Projektordner):
1. curl -fsSL -o kipm.py https://raw.githubusercontent.com/FredHeidinger/server-ki-pm/main/02-Anwendung/client/kipm.py
2. curl -k -fsSL -o kipm-root.crt https://192.168.254.157/static/kipm-root.crt
3. python kipm.py projekt   (zeigt Projekt, Module und Arbeitsorte; damit ist der Zugang geprüft)

Regeln:
- Vor jedem Anlegen zuerst: python kipm.py auftraege  (alle offenen Aufträge; nichts doppelt anlegen)
- Neue Aufgabe nur als Entwurf: python kipm.py auftrag-anlegen --titel "…" --text "…" --modul <modulkennung> --prio hoch|mittel|niedrig
- Arbeiten nur an freigegebenen Aufträgen nach Übernahme: python kipm.py uebernehmen A-<nr> --revision <rev>; Ziel, Schreibumfang und Abnahmekriterien aus python kipm.py lesen A-<nr> sind bindend.
- Unterwegs: python kipm.py fortschritt A-<nr> --revision <rev> "…" oder hindernis.
- Zum Schluss: python kipm.py ergebnis A-<nr> --revision <rev> --zusammenfassung "…" --datei "pfad@commit" --pruefung "befehl: ergebnis" --luecken "…"
- Änderungswünsche am Auftrag mit vorschlag melden; Umfang, Freigabe und Abnahme änderst du nie selbst.
```

Dann sagst du dem Chat in normalen Worten, was du willst, zum Beispiel: „Lege im Modul Core einen Auftrag an: Anmeldefehler verständlich melden.“ Der Chat legt den Entwurf an, du konkretisierst und gibst im Browser frei, der Chat übernimmt und arbeitet.

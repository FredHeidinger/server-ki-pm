# Anleitung für einen Projektchat

Diesen Text in den Projektchat einfügen. Vorher auf der Projektseite in Server-KI-PM einen Chat-Zugang anlegen und den angezeigten Schlüssel bereithalten (er wird nur einmal angezeigt). Den Schlüssel dem Chat als Umgebungsvariable geben, nicht in den Chattext schreiben.

```text
Du arbeitest an das Projekt <PROJEKTNAME> und bist über Server-KI-PM an die zentrale Auftragssteuerung angebunden.

Einrichtung (einmalig in diesem Chat):
1. Lade das Werkzeug: curl -fsSL -o kipm.py https://raw.githubusercontent.com/FredHeidinger/server-ki-pm/main/02-Anwendung/client/kipm.py
2. Die Umgebungsvariablen KIPM_URL (https://192.168.254.157/) und KIPM_SCHLUESSEL sind gesetzt; der Server nutzt ein Zertifikat einer internen CA, verwende bei Bedarf die Umgebungsvariable PYTHONHTTPSVERIFY=0 nur im Heimnetz.
3. Prüfe den Zugang mit: python kipm.py projekt

Regeln:
- Bevor du etwas anlegst: python kipm.py auftraege ansehen, damit nichts doppelt entsteht. Gleiche Titel werden vom Dienst abgewiesen.
- Neue Aufgaben legst du nur als Entwurf an: python kipm.py auftrag-anlegen --titel "…" --text "…" --modul <modulkennung> --prio hoch|mittel|niedrig. Modulkennungen zeigt python kipm.py projekt.
- Arbeiten darfst du nur an Aufträgen, die freigegeben sind und die du übernommen hast: python kipm.py uebernehmen A-<nr> --revision <rev>. Halte dich genau an Ziel, Schreibumfang und Abnahmekriterien aus python kipm.py lesen A-<nr>.
- Während der Arbeit: python kipm.py fortschritt A-<nr> --revision <rev> "…" oder bei Problemen hindernis.
- Am Ende: python kipm.py ergebnis A-<nr> --revision <rev> --zusammenfassung "…" --datei "pfad@commit" --pruefung "befehl: ergebnis" --luecken "…".
- Änderungswünsche am Auftrag selbst meldest du mit vorschlag; du änderst Umfang, Freigabe oder Abnahme nie selbst.
```

Platzhalter `<PROJEKTNAME>` ersetzen. Die Adresse anpassen, falls der Server unter einem anderen Namen erreichbar ist.

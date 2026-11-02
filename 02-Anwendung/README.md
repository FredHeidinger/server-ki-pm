# Anwendung

Django 5.2 (LTS) mit einer SQLite-Datei. Oberfläche und HTTP-Schnittstelle für Chats rufen denselben fachlichen Dienst.

| Ordner / Datei | Inhalt |
|---|---|
| `kipm/settings.py` | Einstellungen: Daten in `daten/` (nicht im Repository), Geheimnis aus `KIPM_SECRET_KEY` oder `daten/.secret_key`, SQLite im WAL-Modus mit `BEGIN IMMEDIATE` |
| `steuerung/models.py` | Projekt, Modul, Arbeitsort, ChatBindung, Auftrag, Revision, Abhängigkeit, Meldung, Ergebnis, Prüfung, Kriteriumsbewertung, Entscheidung, Übernahmebestätigung, AnfrageKennung, Ereignis |
| `steuerung/dienst.py` | Alle Regeln: Befugnis, Reifegrad, Freigabeprüfung mit Überschneidungssperre, exklusive Übernahme, Revisionen, Prüfung, Entscheidungen; jede Änderung schreibt ein Ereignis im selben Commit |
| `steuerung/api.py` | HTTP-Schnittstelle für Chat-Bindungen (`Authorization: Bearer kipm_…`), Wiederholungsschutz über `anfrage_kennung` (Kennung wird vor der Ausführung reserviert, gleichzeitige Wiederholungen erhalten 409) |
| `steuerung/views.py`, `templates/`, `static/` | Oberfläche: Übersicht, Projektseite, Aufträge, Prüfung, Entscheidungen, Verlauf; serverseitig gerendert, ohne JavaScript bedienbar |
| `client/kipm.py` | Kommandozeile für Projektchats, nur Standardbibliothek |
| `tests/` | 86 Tests: Dienstregeln, Schnittstelle, Oberfläche, Kommandozeile gegen Live-Server, gleichzeitige Übernahme und gleichzeitige Wiederholung, Regressionen aus der unabhängigen Prüfung |
| `requirements.txt` | Aus `uv.lock` exportiert für die Serverinstallation (`uv export --no-dev --extra server`) |

Befehle stehen in [PLAYBOOK.md](../PLAYBOOK.md). Die Serverinstallation ist in [03-Server/INSTALLATION.md](../03-Server/INSTALLATION.md) vorbereitet.

## Schnittstelle für Chats

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/auftraege/` | Übernehmbare und eigene Aufträge des gebundenen Projekts |
| `GET /api/auftraege/<id>/` | Gebundene Revision mit Umfang und Kriterien |
| `POST /api/auftraege/<id>/uebernehmen/` | `{revision, anfrage_kennung}`; exklusiv, genau einer gewinnt |
| `POST /api/auftraege/<id>/fortschritt/`, `…/hindernis/`, `…/vorschlag/`, `…/pruefbericht/` | `{revision, text, anfrage_kennung}` |
| `POST /api/auftraege/<id>/ergebnis/` | `{revision, zusammenfassung, dateien[], pruefungen[], luecken, anfrage_kennung}` |
| `GET /api/entscheidungen/`, `POST /api/entscheidungen/vorschlagen/`, `POST /api/entscheidungen/<id>/bestaetigen/` | Entscheidungen des eigenen Projekts; ein Vorschlag betrifft immer nur das eigene Projekt |

Es gibt keine Schnittstelle für Freigabe, Prüferwechsel, Abnahme, Rollen oder Umfangsänderung. Fremde Projekte antworten mit 404, Konflikte und veraltete Revisionen mit 409.

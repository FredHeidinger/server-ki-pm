# Anwendung

Django 5.2 (LTS) mit einer SQLite-Datei. Oberfläche und HTTP-Schnittstelle für Chats rufen denselben fachlichen Dienst.

| Ordner / Datei | Inhalt |
|---|---|
| `kipm/settings.py` | Einstellungen: Daten in `daten/` (nicht im Repository), Geheimnis aus `KIPM_SECRET_KEY` oder `daten/.secret_key`, SQLite im WAL-Modus mit `BEGIN IMMEDIATE` |
| `steuerung/models.py` | Projekt, Modul, Arbeitsort, ChatBindung, Auftrag, Revision, Abhängigkeit, Meldung, Ergebnis, Prüfung, Kriteriumsbewertung, Entscheidung, Übernahmebestätigung, AnfrageKennung, Ereignis |
| `steuerung/dienst.py` | Alle Regeln: Befugnis, Reifegrad, Freigabeprüfung mit Überschneidungssperre, exklusive Übernahme, Revisionen, Prüfung, Entscheidungen; jede Änderung schreibt ein Ereignis im selben Commit |
| `steuerung/api.py` | HTTP-Schnittstelle für Chat-Bindungen (`Authorization: Bearer kipm_…`), Wiederholungsschutz über `anfrage_kennung` (Kennung wird vor der Ausführung reserviert, gleichzeitige Wiederholungen erhalten 409) |
| `steuerung/views.py`, `templates/`, `static/` | Oberfläche: Posteingang, Meine Aufträge, Alle Aufträge, Entscheidungen, Verlauf, Projekte als Arbeitsbereiche mit Reitern, Verbindungen als Katalog mit Anleitung; Auftrag als Seitenpanel (Fragment `auftrag_panel`) oder Seite; serverseitig gerendert, ohne JavaScript bedienbar |
| `steuerung/static/client/kipm.py` | Kommandozeile für Projektchats, nur Standardbibliothek |
| `tests/` | 103 Tests: Dienstregeln, Schnittstelle, Oberfläche, Kommandozeile gegen Live-Server, gleichzeitige Übernahme und gleichzeitige Wiederholung, Regressionen aus der unabhängigen Prüfung |
| `requirements.txt` | Aus `uv.lock` exportiert für die Serverinstallation (`uv export --no-dev --extra server`) |

Befehle stehen in [PLAYBOOK.md](../PLAYBOOK.md). Die Serverinstallation ist in [03-Server/INSTALLATION.md](../03-Server/INSTALLATION.md) vorbereitet.

## Schnittstelle für Chats

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/auftraege/` | Alle offenen Aufträge des Projekts; `?nur_meine=1` übernehmbare und eigene, `?alle=1` auch abgeschlossene |
| `POST /api/auftraege/anlegen/` | `{titel, beschreibung, modul?, prioritaet?, erzwingen?, anfrage_kennung}`; Entwurf, 409 bei gleichem Titel eines offenen Auftrags |
| `GET /api/auftraege/<id>/` | Gebundene Revision mit Umfang und Kriterien |
| `POST /api/auftraege/<id>/uebernehmen/` | `{revision, anfrage_kennung}`; exklusiv, genau einer gewinnt |
| `POST /api/auftraege/<id>/fortschritt/`, `…/rueckfrage/`, `…/hindernis/`, `…/fehler/`, `…/vorschlag/`, `…/pruefbericht/` | `{revision, text, anfrage_kennung}`; Rückfrage, Hindernis und Fehler blockieren den Auftrag bis zur Antwort des Menschen |
| `POST /api/auftraege/<id>/ergebnis/` | `{revision, zusammenfassung, dateien[], pruefungen[], luecken, anfrage_kennung}` |
| `GET /api/entscheidungen/`, `POST /api/entscheidungen/vorschlagen/`, `POST /api/entscheidungen/<id>/bestaetigen/` | Entscheidungen des eigenen Projekts; ein Vorschlag betrifft immer nur das eigene Projekt |

Es gibt keine Schnittstelle für Freigabe, Prüferwechsel, Abnahme, Rollen oder Umfangsänderung. Fremde Projekte antworten mit 404, Konflikte und veraltete Revisionen mit 409.

# Anwendung

Django 5.2 (LTS) mit einer SQLite-Datei. Oberfläche und HTTP-Schnittstelle für Chats rufen denselben fachlichen Dienst.

| Ordner / Datei | Inhalt |
|---|---|
| `kipm/settings.py` | Einstellungen: Daten in `daten/` (nicht im Repository), Geheimnis aus `KIPM_SECRET_KEY` oder `daten/.secret_key`, SQLite im WAL-Modus mit `BEGIN IMMEDIATE` |
| `steuerung/models.py` | Projekt (mit Profil und Inhaltsrevision), Modul, Arbeitsort, ChatBindung, Auftrag, Revision, Abhängigkeit, Meldung, Ergebnis, Prüfung, Kriteriumsbewertung, Entscheidung, Übernahmebestätigung, AnfrageKennung, Ereignis; Projektakte: Objektversion, Funktion, Meilenstein, Nachweis, Umgebung, Release mit Komponenten, Installation, Unterlage, Unterlagenversion, Unterlagenzuordnung, RepoVerbindung, Abrufstand, ExternesObjekt, ExterneAktion, Besuchsmarke, Kontextbezug |
| `steuerung/dienst.py` | Alle Regeln: Befugnis, Reifegrad, Freigabeprüfung mit Überschneidungssperre, exklusive Übernahme, Revisionen, Prüfung, Entscheidungen; jede Änderung schreibt ein Ereignis im selben Commit |
| `steuerung/akte.py` | Dienst der Projektakte: Profil, Funktionen mit getrennten Zuständen (`funktionsstand`), Nachweise, Funktionsabnahme, Umgebungen, Releases, Installationen, Unterlagen mit Versionen und Rollen, Besuchsmarke, Suche, KI-Kontext mit Teilen und Manifest; jede Änderung mit Objektversion und Ereignis |
| `steuerung/ablage.py`, `steuerung/extrakt.py` | Private Dateiablage: Größen-, Namens-, Typ- und Strukturprüfung, Quarantäne, Scanner-Adapter, Textauszug in eigenem Prozess |
| `steuerung/github.py`, `steuerung/abgleich.py` | GitHub-App-Transport (austauschbar für Tests) und GitHub im Dienst: Verbindung an der stabilen ID, Lesen mit Abrufstand, bestätigte Schreibaktionen mit Reservierung, Schritten und Abgleich |
| `steuerung/api.py` | HTTP-Schnittstelle für Chat-Bindungen (`Authorization: Bearer kipm_…`), Wiederholungsschutz über `anfrage_kennung` (Kennung wird vor der Ausführung reserviert, gleichzeitige Wiederholungen erhalten 409) |
| `steuerung/views.py`, `steuerung/views_akte.py`, `templates/`, `static/` | Oberfläche: Projektauswahl, je Projekt sechs Bereiche (Überblick, Funktionen & Planung, Arbeitsaufträge, Versionen & Betrieb, Wissen & Entscheidungen, Verlauf), Projekteinstellungen, „Konto & KI verbinden“; Auftrag als Seitenpanel oder Seite; serverseitig gerendert, ohne JavaScript bedienbar |
| `steuerung/mcp.py` | MCP-Endpunkt `/mcp/<schlüssel>/` für jedes KI-Programm (Assistent: anlegen, planen, Issues, antworten, lesen); reicht Werkzeugaufrufe an `api.py` weiter |
| `tests/` | Tests: Dienstregeln, Schnittstelle, MCP-Endpunkt, Oberfläche, gleichzeitige Übernahme und gleichzeitige Wiederholung, Regressionen aus der unabhängigen Prüfung, Spiegelerzeugung |
| `requirements.txt` | Aus `uv.lock` exportiert für die Serverinstallation (`uv export --no-dev --extra server`) |

Befehle stehen in [PLAYBOOK.md](../PLAYBOOK.md). Die Serverinstallation ist in [03-Server/INSTALLATION.md](../03-Server/INSTALLATION.md) vorbereitet.

## Schnittstelle für Chats

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/auftraege/` | Alle offenen Aufträge des Projekts; `?nur_meine=1` übernehmbare und eigene, `?alle=1` auch abgeschlossene |
| `POST /api/auftraege/anlegen/` | `{titel, beschreibung, modul?, prioritaet?, ziel?, nicht_ziele?, arbeitsort?, schreibumfang[]?, quellen?, abnahmekriterien[]?, pruefer?, freigeben?, erzwingen?, anfrage_kennung}`; in einem Aufruf angelegt und geplant (Planen nur Freds Assistent, PC-Zugang); `freigeben` nur auf Freds Anweisung im Chat, mit Vermerk im Verlauf; scheitert die Freigabeprüfung, wird nichts angelegt; 409 bei gleichem Titel eines offenen Auftrags |
| `POST /api/auftraege/<id>/planen/` | Nur angegebene Felder ändern sich, eine Freigabe erlischt; `freigeben` wie beim Anlegen |
| `GET /api/auftraege/<id>/` | Gebundene Revision mit Umfang und Kriterien |
| `POST /api/auftraege/<id>/uebernehmen/` | `{revision, anfrage_kennung}`; exklusiv, genau einer gewinnt |
| `POST /api/auftraege/<id>/fortschritt/`, `…/rueckfrage/`, `…/hindernis/`, `…/fehler/`, `…/vorschlag/`, `…/pruefbericht/` | `{revision, text, anfrage_kennung}`; Rückfrage, Hindernis und Fehler blockieren den Auftrag bis zur Antwort des Menschen |
| `POST /api/auftraege/<id>/ergebnis/` | `{revision, zusammenfassung, dateien[], pruefungen[]?, fehlgeschlagen[]?, luecken, anfrage_kennung}`; keine Nutzerabnahme: alle Prüfungen bestanden → Erledigt, eine fehlgeschlagen → Blockiert mit Grund, keine Prüfungen → Erledigt mit Vermerk „ohne automatische Tests“ |
| `GET /api/entscheidungen/`, `POST /api/entscheidungen/vorschlagen/`, `POST /api/entscheidungen/<id>/bestaetigen/` | Entscheidungen des eigenen Projekts; ein Vorschlag betrifft immer nur das eigene Projekt |

| `GET /api/projekt/akte/`, `GET /api/projekt/kontext/?auftrag=&kontext_id=&teil=`, `GET /api/projekt/verlauf/?seit=` | Projektakte, KI-Kontext (Teile mit gleicher Kontext-ID), Verlauf |
| `GET /api/unterlagen/<nr>/` | Nur für KI freigegebene, geprüfte Unterlagen |
| `POST /api/funktionen/vorschlagen/`, `…/nachweise/melden/`, `…/installationen/melden/`, `…/unterlagen/entwurf/`, `…/github/vorschlag/` | Vorschläge und Meldungen im Rahmen der Befugnisse; Funktionen nur als Idee, Unterlagen nur als Entwurf, GitHub nur als unbestätigter Entwurf |
| `POST /api/stand/melden/`, `GET /api/stand/` | Meldevertrag `kipm-stand/1`: Stand nach Arbeitsabschluss (Umsetzung, Tests je Umgebung, Installation, Einschränkungen) mit stabiler `meldung_id`; Stand je Modul mit Lücken. Melder für lokale Abläufe und CI: `03-Server/skripte/kipm_melden.py` |
| `POST /api/unterlagen/<nr>/version/` | Neue Textversion unter derselben Kennung (Entwurf) mit `erwartete_version` und `grund` |
| `POST /api/profil/vorschlagen/`, `/api/umgebungen/vorschlagen/`, `/api/umgebungen/aendern/`, `/api/releases/vorschlagen/`, `/api/releases/aendern/`, `/api/arbeitsorte/vorschlagen/`, `/api/arbeitsorte/aendern/`, `/api/module/aendern/` | Pflege durch den Assistenten mit erwarteter Revision und Grund; Profil, Umgebung und Arbeitsort gelten erst nach Freds Übernahme |

Freigeben kann über die Schnittstelle nur Freds Assistent (PC-Zugang) auf seine Anweisung im Chat; eine Abnahme von Aufträgen gibt es nicht. Es gibt keine Schnittstelle für Funktionsabnahme, Grundlagen, KI-Freigabe, Datei-Upload, Rollen, Umfangsänderung oder Bestätigung von GitHub-Aktionen. Fremde Projekte antworten mit 404, Konflikte und veraltete Revisionen mit 409.

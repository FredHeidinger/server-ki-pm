# Anwendung

Django 5.2 (LTS) mit einer SQLite-Datei. Oberfläche und HTTP-Schnittstelle für Chats rufen denselben fachlichen Dienst. Umfang und Ablauf: [01-Planung/ARBEITSABLAUF.md](../01-Planung/ARBEITSABLAUF.md).

| Ordner / Datei | Inhalt |
|---|---|
| `kipm/settings.py` | Einstellungen: Daten in `daten/` (nicht im Repository), Geheimnis aus `KIPM_SECRET_KEY` oder `daten/.secret_key`, SQLite im WAL-Modus mit `BEGIN IMMEDIATE` |
| `steuerung/models.py` | Genutzt: Projekt, Modul, Arbeitsort, ChatBindung, OrdnerAnfrage, Auftrag, Revision, Abhängigkeit, Meldung, Ergebnis, Entscheidung, AnfrageKennung, Ereignis, Objektversion, Meilenstein, Unterlage, Unterlagenversion, RepoVerbindung, GitHubZugang, VersionStand, Kontextbezug. Tabellen früherer Stufen bleiben mit ihren Daten stehen, kein Code liest oder schreibt sie |
| `steuerung/dienst.py` | Regeln für Aufträge: Befugnis, Freigabeprüfung mit Überschneidungssperre, exklusive Übernahme, Revisionen, Ergebnis schließt ab; jede Änderung schreibt ein Ereignis im selben Commit |
| `steuerung/akte.py` | Beschreibung, Entscheidungen, Meilensteine mit automatischer Zählung, Texte und Links mit Fassungen, KI-Kontext mit Teilen und Manifest |
| `steuerung/github.py`, `steuerung/abgleich.py` | GitHub nur lesend (nur GET): Lese-Schlüssel, Repository je Projekt, Versionszeile aus Releases und Bereitstellungen |
| `steuerung/ablage.py` | Früher hochgeladene Dateien lesen und prüfen; neue kommen nicht hinzu |
| `steuerung/api.py` | HTTP-Schnittstelle für Zugänge (`Authorization: Bearer kipm_…`), Wiederholungsschutz über `anfrage_kennung` (Kennung wird vor der Ausführung reserviert, gleichzeitige Wiederholungen erhalten 409) |
| `steuerung/mcp.py` | MCP-Endpunkt `/mcp/<schlüssel>/` für jedes KI-Programm; reicht Werkzeugaufrufe an `api.py` weiter |
| `steuerung/views.py`, `steuerung/views_akte.py`, `templates/`, `static/` | Oberfläche: Projektauswahl, je Projekt vier Bereiche (Überblick, Aufträge, Wissen, Verlauf), Projekteinstellungen, „Konto & KI verbinden“; Auftrag als ganze Seite; serverseitig gerendert, ohne JavaScript bedienbar |
| `tests/` | Tests: Dienstregeln, Schnittstelle, MCP-Endpunkt, Oberfläche, Klickwege, GitHub-Lesen gegen Attrappe und lokale Nachbildung, gleichzeitige Übernahme und Wiederholung, Migrationen, Spiegelerzeugung |
| `requirements.txt` | Aus `uv.lock` exportiert für die Serverinstallation (`uv export --no-dev --extra server`) |

Befehle stehen in [PLAYBOOK.md](../PLAYBOOK.md). Die Serverinstallation ist in [03-Server/INSTALLATION.md](../03-Server/INSTALLATION.md) beschrieben.

## Schnittstelle für Chats

| Methode und Pfad | Zweck |
|---|---|
| `GET /api/projekte/`, `GET /api/projekt/` | Alle Projekte (Assistent) oder das Projekt des Schlüssels, mit Modulen und Arbeitsorten |
| `POST /api/projekte/anlegen/`, `POST /api/module/anlegen/`, `POST /api/module/aendern/` | Projekt und Module pflegen |
| `POST /api/arbeitsorte/vorschlagen/`, `POST /api/arbeitsorte/aendern/` | Arbeitsort vorschlagen oder ändern; gilt erst mit Freds Häkchen |
| `GET /api/auftraege/` | Alle offenen Aufträge; `?nur_meine=1` übernehmbare und eigene, `?alle=1` auch abgeschlossene, `?art=issue` nur Issues |
| `POST /api/auftraege/anlegen/` | `{titel, beschreibung, art?, modul?, meilenstein?, faellig?, prioritaet?, ziel?, nicht_ziele?, arbeitsort?, schreibumfang[]?, quellen?, abnahmekriterien[]?, freigeben?, erzwingen?, anfrage_kennung}`; in einem Aufruf angelegt und geplant (Planen nur Freds Assistent, PC-Zugang); `freigeben` nur auf Freds Anweisung im Chat, mit Vermerk im Verlauf; scheitert die Freigabeprüfung, wird nichts angelegt; 409 bei gleichem Titel eines offenen Auftrags |
| `POST /api/auftraege/<id>/planen/` | Nur angegebene Felder ändern sich, eine Freigabe erlischt; `freigeben` wie beim Anlegen; `meilenstein` allein (`M-n`, leer löst) und `faellig` allein (`JJJJ-MM-TT`, leer entfernt) lassen Revision und Freigabe unberührt |
| `GET /api/auftraege/<id>/` | Gebundene Revision mit Umfang und Kriterien |
| `POST /api/auftraege/<id>/uebernehmen/` | `{revision, anfrage_kennung}`; exklusiv, genau einer gewinnt |
| `POST /api/auftraege/<id>/fortschritt/`, `…/rueckfrage/`, `…/hindernis/`, `…/fehler/` | `{revision, text, anfrage_kennung}`; Rückfrage, Hindernis und Fehler blockieren den Auftrag bis zur Antwort |
| `POST /api/auftraege/<id>/antworten/` | Antwort in Freds Namen; hebt die Blockade auf |
| `POST /api/auftraege/<id>/ergebnis/` | `{revision, zusammenfassung, dateien[], pruefungen[]?, fehlgeschlagen[]?, luecken, anfrage_kennung}`; alle Prüfungen bestanden → Erledigt, eine fehlgeschlagen → Blockiert mit Grund, keine Prüfungen → Erledigt mit Vermerk „ohne automatische Tests“ |
| `GET /api/entscheidungen/`, `POST /api/entscheidungen/eintragen/`, `POST /api/entscheidungen/aendern/` | Entscheidungen des Projekts (Entscheidung, Warum, Seit); eintragen `{entscheidung, warum?}`, ändern oder zurückziehen `{entscheidung: "E-n", stand, entscheidung_text?, warum?, zurueckziehen?}` |
| `POST /api/entscheidungen/fragen/` | Offene Frage an Fred `{frage, warum?, anfrage_kennung}`; legt nichts fest, bis er in „Wartet auf dich“ antwortet |
| `GET /api/meilensteine/`, `POST /api/meilensteine/anlegen/`, `POST /api/meilensteine/aendern/` | Meilensteine mit Name, Datum und automatisch gezähltem Stand (Abgebrochene zählen nicht mit); anlegen `{name, datum?}`, ändern `{meilenstein: "M-n", revision, name?, datum?}`, leeres Datum entfernt es |
| `GET /api/projekt/akte/`, `GET /api/projekt/kontext/?auftrag=&modul=&kontext_id=&teil=`, `GET /api/projekt/verlauf/?seit=` | Profil mit Übersicht der Texte und Links, KI-Kontext (Teile mit gleicher Kontext-ID), Verlauf |
| `GET /api/sitzungsstart/` | Für den Hook beim Sitzungsstart: nächster Eintrag nach der Regel für „weiter“, offene Einträge, was auf Fred wartet. Schreibt nichts; unbekannter Ordner ergibt `projekt: null`. Vertrag in [ROADMAP-ERSATZ.md](../01-Planung/ROADMAP-ERSATZ.md) |
| `POST /api/profil/aendern/` | Beschreibung und Profil ändern (`{revision, beschreibung?, zweck?, …}`), mit Eintrag im Verlauf |
| `GET /api/unterlagen/<nr>/` | Neueste Fassung eines Texts oder Links des Projekts, ohne Freigabe; archivierte und früher hochgeladene Dateien ohne damalige Freigabe nicht |
| `POST /api/unterlagen/entwurf/`, `POST /api/unterlagen/<nr>/version/` | Text anlegen; neue Fassung unter derselben Kennung mit `erwartete_version` und `grund`, ältere Fassungen bleiben |

Jeder `POST` außer `uebernehmen`, `fortschritt`, `rueckfrage`, `hindernis`, `fehler`, `ergebnis` und `entscheidungen/fragen` verlangt `fred_sagte`: Freds Worte aus dem Chat, mit denen er die Änderung angewiesen oder bestätigt hat (höchstens 500 Zeichen). Ohne sie antwortet die Schnittstelle mit 400 und ändert nichts, mit ihnen stehen sie im Verlauf.

Freigeben kann über die Schnittstelle nur Freds Assistent (PC-Zugang) auf seine Anweisung im Chat. Fremde Projekte antworten mit 404, Konflikte und veraltete Revisionen mit 409.

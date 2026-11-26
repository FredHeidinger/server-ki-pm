# Lesezugang Projektdaten 1

Vertragskennung: `kipm-werkbank-projektdaten/1` (Antwortfeld `schema_version: 1`). Diese Fassung
veröffentlicht KI-PM; sie ersetzt den Entwurf der Werkbank (`docs/kipm/projektdaten-1/ENTWURF.md`,
26.11.2026). Sie ergänzt `kipm-werkbank/2`, ohne dessen Schemas zu ändern; `kipm-werkbank/1`
bleibt unberührt. Jede Änderung an Teilen, Feldern oder Regeln dieses Dokuments erhöht die Zahl.

## Zweck

Freds Entscheidung KW-14 (26.11.2026): Alles, was KI-PM zu einem Projekt speichert, liegt
zusätzlich lesbar im Projekt unter `ki-werkbank/kipm/`. KI-PM bleibt führend; die Kopie schreibt
nur die Werkbank. Der Werkbank-Zugang bekommt dafür keine Chat-Rechte: die Chat-Schnittstellen
weisen ihn weiter ab (`WERKBANK_ABWEISUNG`).

## Endpunkt

`GET /api/werkbank/v1/projektdaten/?projektbindung_id=<id>[&seite=<zeiger>]`

- Anmeldung wie die übrige Werkbank-Schnittstelle (Bearer der Werkbank-Instanz); Chat-Schlüssel:
  403 `FALSCHE_ZUGANGSART`.
- Kopf `X-KIPM-Vertrag: kipm-werkbank/2`. Ohne Kopf oder mit `kipm-werkbank/1`: 426
  `VERTRAG_INKOMPATIBEL`.
- Nur lesend. Der Abruf vermerkt keinen Kontakt (er ist kein Lebenszeichen) und ändert weder
  Zuweisung, Ausführung, Erlaubnis noch Verlauf.
- Nur für eine Projektbindung dieser Instanz, deren Projekt die Werkbank erreichen darf (aktiv
  und erlaubt). Sonst 404 `NICHT_GEFUNDEN`. Fehlende oder nicht ganzzahlige `projektbindung_id`
  und ein ungültiger Seitenzeiger: 400 `UNGUELTIGE_NACHRICHT`. Andere Methoden: 405.

## Antwort je Seite

```json
{"vertrag": "kipm-werkbank/2", "art": "projektdaten", "schema_version": 1, "projektbindung_id": 9,
 "stand": {"inhalt_revision": 12, "ereignis_bis": 4300, "erzeugt": "2026-11-26T10:00:00+00:00"},
 "projekt": {"kennung": "testalpha", "...": "..."},
 "teile": {"module": [], "arbeitsorte": [], "auftraege": [], "entscheidungen": [],
           "meilensteine": [], "texte": [], "links": [], "verlauf": [], "erlaubnisse": []},
 "seite": {"naechste": "<zeiger>"}}
```

- `teile` hat auf jeder Seite genau diese neun Namen; jede Liste höchstens 100 Einträge, nach `id`
  aufsteigend, jeder Eintrag ein Objekt mit `id`. Ein auf dieser Seite erschöpfter Teil ist leer.
- `projekt` steht nur auf der ersten Seite, auf Folgeseiten `null`.
- `stand`: `inhalt_revision` des Projekts, `ereignis_bis` (letztes Ereignis des Projekts) und
  `erzeugt` (Zeitpunkt dieses Ereignisses, nicht der Antwort). Er ist auf allen Seiten gleich.
  Der Zeiger bindet den Stand der ersten Seite: Hat er sich geändert, antwortet der Server mit
  409 `STAND_GEAENDERT`; die Werkbank beginnt dann später ohne Zeiger von vorn. Dasselbe gilt,
  wenn sich der Stand ändert, während eine Seite entsteht (auch auf der ersten oder einzigen).
- `seite.naechste` ist undurchsichtig; `null` auf der letzten Seite.

## Inhalt

Alle Zeichenketten sind bereinigt wie im KI-Kontext (`akte._bereinigt`): Zugangsschlüssel,
MCP-Links, GitHub-Token und private Schlüssel sind ersetzt (etwa „[Schlüssel entfernt]“),
Zugangsdaten in Adressen (`https://name:kennwort@host`) ebenso („[Zugangsdaten entfernt]“).
Zugangsdaten, Schlüsselhashes und Kennwörter stehen nie in der Antwort. Frühere Tabellen und
Spalten (PLAYBOOK) fehlen.

| Teil | Auswahl | Felder |
|---|---|---|
| `projekt` | Projekt der Bindung | `kennung`, `name`, `kuerzel`, `beschreibung`, `aktiv`, `zweck`, `zielgruppe`, `nicht_ziele`, `entwicklungsstufe`, `phase`, `profil_revision`, `angelegt` |
| `module` | alle, auch inaktive | `id`, `kennung`, `name`, `eltern`, `revision`, `aktiv` |
| `arbeitsorte` | alle, auch unbestätigte | `id`, `kennung`, `name`, `rechner`, `pfad`, `repository`, `modul`, `aktiv`, `bestaetigt`, `vorgeschlagen_von`, `revision` |
| `auftraege` | Aufträge, Issues und Integrationen in allen Zuständen | `id` und dieselben Felder wie `kipm_lesen` (Revision, Planung, Kriterien, Ergebnis, Lieferungen, `w1`), `meldungen` vollständig |
| `entscheidungen` | alle, die das Projekt betreffen, in jedem Zustand | `id`, `titel`, `nummer`, `entscheidung`, `warum`, `seit`, `zustand`, `stand`, `verweis`, `vorgeschlagen_von`, `vorgeschlagen` |
| `meilensteine` | alle | `id`, `nummer`, `name`, `datum`, `revision`, `erledigt`, `gesamt`, `stand` |
| `texte` | Texte und frühere Dateien | lesbar: `id`, `titel`, `version`, `ki_lesbar: true`, `kennung`, `art`, `beschreibung`, `modul`, `geaendert`, `inhalt`; sonst (archiviert, gesperrte Datei) nur `id`, `titel`, `version`, `ki_lesbar: false` |
| `links` | Links und Repository-Dateien | wie `texte`, statt `inhalt` aber `adresse` und `fixierter_stand` |
| `verlauf` | Ereignisse des Projekts bis `ereignis_bis` | `id`, `zeit`, `beobachtet`, `akteur`, `objekt`, `art` (das Ereignis), `text` (Anlass mit Freds Worten), `revision` |
| `erlaubnisse` | alle W1-Erlaubnisse des Projekts | `id`, `zugang` (Name, nie Schlüssel), `revision`, `aktionen`, `ziele`, `standard_ziel`, `budget`, `reserviert`, `gueltig_ab`, `gueltig_bis`, `widerrufen`, `widerrufen_am` |

Felder können in einer späteren Fassung dazukommen; die Werkbank liest Einträge allgemein und
verlangt nur `id`.

## Beispiele

`beispiele/seite-1.json` und `beispiele/seite-2.json` sind die Beispiele des Entwurfs. Der dort
eingeschleuste Testschlüssel steht so, wie der Server ihn ausgibt: „[Schlüssel entfernt]“. Die
Beispiele zeigen einen Ausschnitt der Felder; `tests/test_werkbank_projektdaten.py` baut die Lage
nach und prüft, dass jede Antwort mindestens diese Felder trägt.

## Grenzen

- Eine Entscheidung, die mehrere Projekte betrifft, erhöht den Stand nur im Projekt, in dem ihr
  Ereignis steht.
- Werte, die ohne eigenes Ereignis wechseln (laufende Ordnersperre, `uebernehmbar`, reserviertes
  Budget einer Erlaubnis), können sich zwischen zwei Seiten oder Abrufen bei gleichem Stand
  unterscheiden.
- Zugangsdaten in Adressen erkennt der Server bis zum letzten `@` vor Pfad, Abfrage oder Anker.
  Ein Kennwort, das roh (nicht kodiert) `/`, `?`, `#`, Leerzeichen, `"`, `<`, `>`, `[` oder `]`
  enthält, erkennt er nicht; solche Zeichen müssen in einer Adresse ohnehin kodiert sein.
- `meldungen` eines Auftrags sind vollständig; die Grenze von 100 gilt für Einträge, nicht für
  deren Größe.

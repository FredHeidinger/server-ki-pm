# Vertrag kipm-werkbank/1

Kanonische Beschreibung der Schnittstelle zwischen KI-PM (Server) und der Werkbank (Dienst auf Freds PC). Maßgeblich sind diese Datei, `schemas/` und `beispiele/`. Die Werkbank hält eine **fest versionierte Kopie** dieses Ordners; sie lädt nichts nach und führt keinen Code aus dem anderen Projekt aus. Rechenhilfen des Servers: `steuerung/werkbank_vertrag.py`.

Inhalt des Ordners:

- `schemas/*.schema.json`: JSON Schema (Draft 2020-12), gemeinsame Bausteine in `gemeinsam.schema.json`.
- `beispiele/*.gut.json` müssen gegen das Schema mit gleichem Namensanfang gültig sein, `*.schlecht.json` ungültig. `beispiele/hash-vektoren.json` legt die kanonische Darstellung fest.
- Die Schemas weisen unbekannte Felder ab. Jede Änderung am Vertrag, auch ein neues Feld, erhöht deshalb die Zahl im Namen (`kipm-werkbank/2`).

## Zusammenspiel in einem Satz

Die Werkbank fragt alle 10 Sekunden den Server (`abgleich`), übernimmt zugewiesene, freigegebene Aufträge (`uebernehmen`), holt sich vor jedem Schritt die Erlaubnis (`schritt`) und meldet technische Fakten (`ereignisse`). Fred bedient alles in KI-PM; die Werkbank liest seine Aktionen im Abgleich.

## Transport

- Nur ausgehende HTTPS-Verbindungen der Werkbank, Zertifikat prüfen, keine Weiterleitung auf andere Hosts, keine Rückverbindung zum PC.
- Schlüssel ausschließlich im Kopf `Authorization: Bearer <schluessel>`; nie in URL, Beispielen, Protokollen oder Antworten.
- Kopf `X-KIPM-Vertrag: kipm-werkbank/1` in jeder Anfrage. Weicht der Server ab, antwortet er mit `VERTRAG_INKOMPATIBEL`; die Werkbank sperrt dann die Ausführung vor dem ersten KI-Aufruf.
- Endpunkte (Pfade unter `/api/werkbank/v1/`):
  - `GET abgleich/?seite=<cursor>`: Antwort `abgleich_antwort`, höchstens 100 Einträge je Liste und Seite; die Werkbank folgt `seite.naechste`, bis sie `null` ist. Der Cursor ist undurchsichtig; `seite.groesse` ist die größte Listenlänge der Seite.
  - `GET operationen/<uuid>/`: Antwort `operation_antwort`; liest nur, ein gespeicherter Erfolg ist kein neuer Startschein.
  - `POST uebernehmen/`: `uebernehmen_anfrage` → `uebernehmen_antwort`.
  - `POST schritt/`: `schritt_anfrage` → `schritt_antwort`.
  - `POST ereignisse/`: `ereignisse_anfrage` (ein Ereignis je Aufruf) → `ereignisse_antwort`.
- Takt: Abgleich alle 10 s, Lebenszeichen (Ereignis `lebenszeichen`) spätestens alle 30 s, Warnung „Werkbank nicht erreichbar“ nach 90 s. Maßgeblich ist die Empfangszeit des Servers, nie die Uhr der Werkbank.

## Identitäten

| Feld | Bedeutung |
|------|-----------|
| `server_id` | UUID dieser KI-PM-Installation, dauerhaft gespeichert |
| `werkbank_id` | UUID einer registrierten Werkbank-Instanz |
| `projektbindung_id`, `projektbindung_version` | Projekt und bestätigter Arbeitsort der Werkbank; Version steigt bei jeder Änderung |
| `auftrag_id` | interner stabiler Schlüssel; die sichtbare Nummer (`KIPM-25`) ist nur Anzeige |
| `revision` | freigegebene Inhaltsrevision des Auftrags |
| `zuweisung_version` | steigt bei jedem Zielwechsel, Freigaberücknahme und jeder Kontextänderung; ersetzt einen eigenen Epoch-Zähler |
| `ausfuehrung_id` | UUID der technischen Ausführung |
| `snapshot_sha256` | Hash des unveränderlichen Auftragssnapshots |

**Vollständige Quellbindung** (`quelle`): alle Felder der Tabelle außer `ausfuehrung_id` und `snapshot_sha256` vor der Übernahme (`quelle_vor_uebernahme`), danach alle. Fehlt ein Feld oder ist es veraltet, ist die Nachricht ungültig (400) beziehungsweise veraltet (409); eine alte Meldung wird nie auf eine neuere Fassung angewendet.

## Kanonische JSON-Darstellung und Hashes

Beide Seiten bilden Hashes aus derselben Darstellung:

1. Erlaubt sind Objekte, Listen, Texte, `true`/`false`, `null` und Ganzzahlen von −(2^53−1) bis 2^53−1. **Keine** Gleitkommazahlen, kein NaN, keine anderen Typen, keine Nicht-Text-Schlüssel.
2. Objektschlüssel nach Unicode-Codepunkt aufsteigend sortiert; Listenreihenfolge bleibt.
3. Kein Leerraum außerhalb von Texten; Trenner `,` und `:`.
4. Texte als UTF-8 ohne `\u`-Maskierung von Nicht-ASCII-Zeichen; Steuerzeichen und `"` `\` wie in JSON üblich maskiert. Keine Unicode-Normalisierung.
5. `sha256` = SHA-256 der UTF-8-Bytes, kleingeschrieben hexadezimal.

`snapshot_sha256` ist der Hash des Objekts `auftragssnapshot` (Schema) genau so, wie es in `uebernehmen_antwort.snapshot` steht. **Nicht** in den Snapshot und damit nicht in den Kontexthash gehören Fortschrittsmeldungen anderer Aufträge, Kontakt- und Besuchszeiten, Termine, Meilensteine und lokale Git-Fortschreibung; `Projekt.inhalt_revision` ist kein Vergleichsschlüssel.

`inhaltshash` einer Nachricht = `sha256` der ganzen Nachricht ohne das Feld `inhaltshash`. Nachrichten tragen keine Sendezeit, damit eine Wiederholung denselben Hash hat.

## Ändernde Nachrichten

Pflichtfelder: `vertrag`, `operation_id` (UUID, für eine Wiederholung gleich), `art`, `quelle`, `nutzlast`, `inhaltshash`. Unbekannte Felder werden mit 400 abgewiesen.

- Gleiche `operation_id` und gleicher Hash: Wiederholung. Der Server liefert den gespeicherten Ausgang und ändert nichts; bei `schritt` prüft er den aktuellen Berechtigungsstand erneut.
- Gleiche `operation_id`, anderer Hash: 409 `WIDERSPRUCH_WIEDERHOLUNG`.
- Ging die Antwort verloren: `GET operationen/<uuid>/` liefert den Ausgang; die Werkbank wiederholt die Nachricht nur mit derselben `operation_id`.

### Logische Schritte

Schrittschlüssel: `ausfuehrung_id`, `planfassung` (`vorplanung` oder der Planhash), `paket` (`root` oder Paketkennung), `phase`, `position`. Jeder Versuch trägt `versuch_nr`, `eingabehash` und seine `operation_id`.

- Je Schrittschlüssel und `versuch_nr` genau eine Autorisierung; eine andere `operation_id` für denselben Versuch erzeugt keine zweite.
- Höchstens ein nicht abschließend geklärter Versuch je Schritt; bei `unklar` startet nichts Neues; ein neuer Versuch nur nach bekanntem Fehlschlag und zulässiger Wiederholungsregel.
- Eine Owner-Aktion (`owner_aktion_id`) wird einmalig an die zugehörige Wirkung gebunden.

### Ereignisarten (`ereignisse_anfrage`)

`lebenszeichen`, `plan_bestaetigt`, `paket_status`, `rueckfrage`, `freigabe_noetig`, `antwort_angewendet`, `aktion_angewendet`, `fehler`, `offline_phase`, `kontext_veraltet`, `halt_bestaetigt`, `abbruch_bestaetigt`, `abschluss_bericht`. Jedes trägt `ereignis_id`, eine je Ausführung lückenlose `sequenz` und bei Statusprojektionen eine monotone `statusversion`. Der Server prüft die Quellbindung; eine verspätete Meldung zu einer überholten Fassung wird mit `historisch_quittiert` beantwortet und ändert nichts Aktuelles.

### Menschliche Aktionen (Liste `aktionen` in `abgleich_antwort`)

`antworten`, `start_freigeben`, `uebernahme_freigeben`, `weiterbauen`, `bearbeitung_beenden`, `abbrechen`. Nur Fred in der angemeldeten Oberfläche erzeugt sie; Werkbank-Schlüssel und `fred_sagte` ersetzen das nicht. Zustände: `ausstehend` (gesendet), `angenommen`, `ausgefuehrt` (von der Werkbank bestätigt), `veraltet`, `fehlgeschlagen`, `unklar`. Eine Aktion wirkt höchstens einmal.

### Technischer Zustand

`plant`, `laeuft`, `wartet_antwort`, `wartet_freigabe`, `fehler`, `unklar`, `offline`, `halt_angefordert`, `angehalten`, `abbruch_angefordert`, `abgebrochen`, `abschluss_gemeldet`. Abbruch und Bearbeitung ändern gelten erst nach `halt_bestaetigt` beziehungsweise `abbruch_bestaetigt`.

## Fehler

HTTP-Status mit Körper `fehler` (Schema): `code`, `text` (verständlich), `naechste_handlung`, optional `details`. Nie Geheimnisse oder Stacktraces.

| Status | Bedeutung | Codes (stabil) |
|--------|-----------|----------------|
| 400 | Nachricht ungültig | `UNGUELTIGE_NACHRICHT` |
| 401 | Zugang fehlt, ungültig, abgelaufen, widerrufen | `NICHT_ANGEMELDET` |
| 404 | unbekannt oder für diese Werkbank unzugänglich | `NICHT_GEFUNDEN` |
| 409 | veraltet, Zustandskonflikt, widersprüchliche Wiederholung | `VERALTET`, `ZUSTANDSKONFLIKT`, `WIDERSPRUCH_WIEDERHOLUNG`, `HALT_ANGEFORDERT`, `KONTEXT_VERALTET`, `ABSCHLUSS_GESPEICHERT` |
| 413 | Größe überschritten | `ZU_GROSS` |
| 426 | Vertrag nicht kompatibel | `VERTRAG_INKOMPATIBEL` |

Weitere Codes dürfen ergänzt werden, solange sie dem Muster `^[A-Z][A-Z0-9_]{2,63}$` folgen; die Werkbank behandelt unbekannte Codes nach dem Status.

## Abschlussbericht

`abschluss_bericht` trägt Behandlung **aller** ursprünglichen Kriterien (`AK-n`, auch „bereits erfüllt“ mit Nachweis am vorhandenen Commit), den endgültigen Paketplan samt Zerlegungen und Ersatzpaketen, die tatsächlichen Integrationen, Prüf- und Reviewnachweise, die Gesamtprüfung, offene Arbeiten und ungeklärte Wirkungen. Der Server prüft Vollständigkeit und Herkunft, nicht die Wahrheit der Tests (Regeln in WP7).

# Vertrag kipm-werkbank/2

Fassung 2 der Schnittstelle zwischen KI-PM (Server) und der Werkbank. Sie ist **Fassung 1 plus zwei Erweiterungen** und ersetzt Fassung 1 nicht: Alles, was hier nicht anders steht, gilt wörtlich wie in [`../kipm-werkbank-1/VERTRAG.md`](../kipm-werkbank-1/VERTRAG.md). Maßgeblich sind diese Datei, `schemas/` und `beispiele/`; die Werkbank hält davon eine feste Kopie. Führend: Server-KI-PM, Auftrag KIPM-36 (Paket KP02 des Drei-Wege-Plans). Der frühere Entwurf [`ENTWURF.md`](ENTWURF.md) bleibt als Hintergrund; wo er mehr verlangt, gilt diese Datei.

Die zwei Erweiterungen:

1. **W1-Delegation**: Aufträge, die ein berechtigter Chat über `kipm-chat-delegation/1` angelegt und freigegeben hat, laufen nur über Fassung 2. Der Snapshot nennt die Zuständigkeit; Start- und Integrationsfreigaben gibt die gespeicherte Erlaubnis statt Freds Klick.
2. **Zielverbund**: Ein Auftrag kann neben seinem Ziel weitere Repositories (Projektbindungen derselben Werkbank) umfassen. Jedes Paket hat genau ein Zielrepository, integriert wird je Repository, eine globale Transaktion über mehrere Repositories gibt es nicht.

## Aushandlung und Kopf

- `GET /api/werkbank/faehigkeiten/?vertrag=kipm-werkbank/1&vertrag=kipm-werkbank/2` liefert `gemeinsam` mit beiden Fassungen, wenn beide angeboten werden ([werkbank-aushandlung-1](../werkbank-aushandlung-1/VERTRAG.md)).
- Endpunkte und Pfade bleiben gleich (`/api/werkbank/v1/...`; das `v1` im Pfad ist der Pfadname, nicht die Vertragsfassung). Der Kopf `X-KIPM-Vertrag: kipm-werkbank/2` wählt Fassung 2 für diese Anfrage; das Feld `vertrag` jeder Nachricht muss zum Kopf passen, sonst 400.
- **Eine Ausführung bleibt bei der Fassung, mit der sie übernommen wurde.** Nachrichten in der anderen Fassung zu dieser Ausführung: 409 `VERTRAG_INKOMPATIBEL`. Kein stiller Wechsel, kein Rückfall.
- Ein Auftrag, der Fassung 2 braucht (delegiert oder mit Zielverbund), erscheint im Abgleich nach Fassung 1 nicht und wird dort nicht übernommen (409 `VERTRAG_INKOMPATIBEL` mit Grund). Fassung 1 wird nie mit fehlenden Feldern bedient.

## Snapshot (`auftragssnapshot`, `snapshot_version: 2`)

Alle Felder von Fassung 1 und zusätzlich:

| Feld | Inhalt |
|---|---|
| `zustaendigkeit` | `{"weg": "W1", "fuehrendes_projekt": <Kennung>, "erlaubnis": null}` oder bei Delegation `"erlaubnis": {"id", "revision", "aktionen"}`; `aktionen` ist die gespeicherte Liste der Erlaubnisfassung (etwa `starten`, `integrieren`). Ein Text erteilt nichts; maßgeblich ist der Stand bei jedem Schritt. |
| `zielverbund` | Liste mit 1 bis 20 Mitgliedern, das führende zuerst: `repo_id` (`repo-<projektbindung_id>`), `fuehrend`, `projekt` (Kennung), `projektbindung_id`, `projektbindung_version`, `arbeitsort` (`id`, `name`, `beschreibung`) und `basiscommit`: im Snapshot der Übernahmeantwort der Stand, den die Werkbank für dieses Repository gemeldet hat (`repos`). |
| `integrationseinheiten` | je Mitglied eine Einheit `{"einheit": "<repo_id>", "repos": ["<repo_id>"]}`: Atomar ist höchstens ein einzelnes Repository. |

Ohne weitere Mitglieder hat `zielverbund` genau einen Eintrag (das bisherige Ziel).

## Übernahme

`uebernehmen_anfrage.nutzlast` trägt zusätzlich `repos`: je Mitglied des Zielverbunds genau ein Eintrag `{"repo_id", "basiscommit", "repo_fingerabdruck"}` (Commit als 40 oder 64 Hexzeichen). Fehlt ein Mitglied, steht eines zu viel darin oder doppelt: 409 `ZUSTANDSKONFLIKT`, nichts wird übernommen. Bei Delegation prüft der Server die aktuelle Erlaubnisfassung (nicht widerrufen, nicht abgelaufen, gleiche Fassung wie der Auftrag); sonst 409 `ERLAUBNIS_UNGUELTIG`.

## Schritte

Wie Fassung 1. Zusätzlich vor jeder Erlaubnis:

- **Mitglieder unverändert**: Hat sich `projektbindung_version` eines Mitglieds seit dem Snapshot geändert (anderer Arbeitsort, Zugang, Zielwechsel), lehnt der Server ab: `MITGLIED_GEAENDERT`. Die Werkbank baut andere Repositories nicht blind neu; Fred gibt in KI-PM neu frei.
- **Erlaubnis gültig** (nur delegierte Aufträge): widerrufen oder abgelaufen → abgelehnt `ERLAUBNIS_UNGUELTIG`, nichts Neues startet; laufende Arbeit hält wie bisher über `halte`.
- **Freigabe aus der Erlaubnis**: Wartet ein Schritt auf eine Freigabe der Art `start`, gilt sie als erteilt, wenn die aktuelle Erlaubnis `starten` enthält; Art `uebernahme` (Integration) entsprechend mit `integrieren`. Die Antwort nennt dann `text` „Schritt freigegeben (W1-Erlaubnis …)“; der Verlauf in KI-PM zeigt die Erlaubnis. Fehlt die Aktion, wartet der Schritt wie in Fassung 1 auf Freds Aktion (`FREIGABE_AUSSTEHEND`).

## Ereignisse

- `plan_bestaetigt.pakete[]` trägt zusätzlich `repo_id` (Pflicht); unbekanntes Repository → 400 `UNGUELTIGE_NACHRICHT` mit Grund, nichts gespeichert.
- `paket_status` trägt optional `integration: {"repo_id", "commit"}`, sobald ein Paket in sein Repository integriert ist. So bleibt ein integrierter Commit sichtbar, auch wenn ein anderes Repository danach scheitert.
- `abschluss_bericht.integrationen[]` trägt zusätzlich `repo_id` (Pflicht).

## Abschluss

Zusätzlich zu Fassung 1: Jedes erledigte Paket muss mit seinem Repository in einem bestätigten Plan (`plan_bestaetigt`) stehen; ein Teil- oder Ersatzpaket meldet die Werkbank vor dem Abschluss mit einem neuen Plan. Jede Integration nennt ein Mitglied des Zielverbunds und das Repository ihres Pakets; jedes Mitglied mit einem erledigten Paket hat mindestens eine Integration. Fehlt eine, ist ein Paket gescheitert oder die Gesamtprüfung rot, antwortet der Server 409 `ABSCHLUSS_UNVOLLSTAENDIG`; der Auftrag bleibt offen beziehungsweise blockiert, die schon gemeldeten Integrationen anderer Repositories bleiben sichtbar. Ein Teilstand ist nie „Erledigt“.

## Fehlercodes (zusätzlich)

| Status | Code | Bedeutung |
|---|---|---|
| 409 | `ERLAUBNIS_UNGUELTIG` | W1-Erlaubnis widerrufen, abgelaufen oder in anderer Fassung |
| 409 | `MITGLIED_GEAENDERT` | ein Mitglied des Zielverbunds wurde nach dem Snapshot geändert |
| 409 | `VERTRAG_INKOMPATIBEL` | Ausführung oder Auftrag gehört zur anderen Fassung |

## Bewusst nicht in Fassung 2

Seitenweises Nachladen von Quellen, eigene Schritt-Tickets, Kosten- und Budgetverbrauch je Schritt: Der Entwurf nannte sie; der Drei-Wege-Plan braucht sie für W1-Delegation und Zielverbund nicht. Sie kämen als Fassung 3.

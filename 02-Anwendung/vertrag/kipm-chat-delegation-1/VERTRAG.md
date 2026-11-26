# W1-Chatdelegation 1

Vertragskennung: `kipm-chat-delegation/1`. Dieser additive Chatzugang ersetzt weder
`kipm-werkbank/1` noch dessen noch nicht ausgelieferte v2-Gegenstelle. Kein Laufstart.

## Anmeldung und Zuständigkeit

Nur ein aktiver Projekt- oder PC-Chat-Bearer ist zulässig. Sitzungscookies gelten hier nicht;
Werkbank-Schlüssel werden mit 403 abgewiesen. Der Server ermittelt Chat und Projekt aus dem
Schlüssel sowie beim PC-Zugang aus `X-KIPM-Projekt` oder dem registrierten Arbeitsordner.
Ein im Text behaupteter Ownerstatus oder `fred_sagte` erteilt keine W1-Rechte.

Der angemeldete tatsächliche Owner verwaltet Erlaubnisse mit Sitzung und CSRF-Schutz unter
„Konto & KI verbinden“ → „W1: Chat an die Werkbank delegieren“. Es gibt keinen Chat-Endpunkt
zum Erteilen oder Erweitern einer Erlaubnis. Alte Zugänge und Fachaufträge werden nicht
umgedeutet. Der bestehende Chatablauf behält seine bisherigen Regeln.

KI-PM führt bei W1 die Fachakte; technische Pakete und ihre Ausführung führt die Werkbank.
W2 und W3 benötigen keinen KI-PM-Auftrag. Die ausgelieferte Arbeitsregel steht gemeinsam in
MCP-Anweisungen und Projektkontext, erzeugt selbst aber keine Befugnis.

## Erlaubnis und Budget

Eine Erlaubnis bindet Chat, Projekt, tatsächlichen Owneraussteller, eindeutige Fassung,
Zeitfenster, einzeln erlaubte Aktionen und konkrete Werkbankziele. Die Zielsicherung enthält
Projektbindungs-ID und -Version, Werkbank-Dienst- und Zugangsidentität, Arbeitsort-ID und einen Hash aus
Pfad, Repository, Rechner und Modul. Anzeigenamen allein entwerten die Zielbindung nicht.
Eine neue oder veränderte Erlaubnis wird nicht aus einer alten Rollenbezeichnung abgeleitet.
Fassungen bleiben erhalten; Widerruf erzeugt eine neue widerrufene Fassung.

Derzeit nutzbare Einzelaktionen: `auftrag_anlegen`, `auftrag_aendern`, `zuweisen`, `freigeben`.
Start, Fortsetzen und Integration bleiben ohne v2-Gegenstelle nicht verfügbar. Der Dienst
prüft Rechte und Zielstände vor jeder Wirkung frisch in derselben SQLite-Schreibtransaktion.

Für eine ausstehende delegierte Freigabe bindet KI-PM die Profilfelder des Projekts, seine
geltenden Entscheidungen sowie die vollständigen neuesten Fassungen aktiver Projekttexte und
Linkadressen. Ändert sich diese Basis, kehrt der Auftrag beim Abgleich in den Entwurf zurück.
Die Projektzuweisung erhält eine neue Fassung. Nach Prüfung kann der berechtigte Chat mit
einer neuen Anfragekennung erneut freigeben. Die bestehende Budgetreservierung gilt weiter.
Auch bei einer Freigabe im früheren Hashformat prüft der Abgleich die aktuelle Quellenbindung.

`budget` hat genau `waehrung`, `geld_mikro`, `zeit_ms`, `aufrufe`. Unterstützte Währungen:
USD und EUR, ohne Umrechnung. Alle Zahlen sind Ganzzahlen bis `2^53-1`, kein Boolean oder
Gleitkommawert; Geld darf null sein, Zeit und Aufrufe müssen positiv sein. Geld ist in
Millionsteln der gewählten Währung angegeben. Der Erlaubnisrahmen gilt gemeinsam für alle
Aufträge. Die Freigabe reserviert atomar das unveränderliche Auftragsbudget; eine erneute
Freigabe reserviert es nicht doppelt. Ein Budget ist keine Kostenfreigabe außerhalb dieses
konkreten Rahmens und kein Beleg für tatsächlich verbrauchte Kosten.

Mehrere Ziele ohne Standard erzeugen einen sichtbaren Konflikt. Eine eindeutige Auswahl
oder das gespeicherte erlaubte Standardziel ist nötig. Freigabe erfordert eine belastbare
Bereitschaft und denselben Arbeitsort in Fachrevision und Zielzuweisung. Belastbar bereit ist ein
Ziel, für das die Werkbank schon übernommen hat, oder dessen Werkbank gerade abfragt (Kontakt
innerhalb von 90 s). Der Repository-Fingerabdruck ist der aktuelle Commit der Werkbank; ein neuer
Commit ändert die Zielbindung nicht.

## HTTP- und MCP-Wege

| HTTP | MCP | Wirkung |
|---|---|---|
| `GET /api/w1/erlaubnisse/` | `kipm_w1_erlaubnisse` | Nur die Erlaubnisse des angemeldeten Chats im tatsächlichen Projekt lesen |
| `POST /api/w1/auftraege/anlegen/` | `kipm_w1_auftrag_anlegen` | Fachauftrag atomar anlegen, optional zuweisen und freigeben |
| `POST /api/w1/auftraege/<id>/aktion/` | `kipm_w1_auftrag_aktion` | Neu planen, zuweisen oder freigeben; Fachrevision und Erlaubnisfassung prüfen |

Jede Änderung verlangt `vertrag`, eine eigene `anfrage_kennung` (1–80 Zeichen),
`delegation_id`, `delegation_revision`. Der Aktionsweg verlangt zusätzlich
`auftrag_revision` und `aktion`. Die MCP-Werkzeuge erfinden keine Wiederholungskennung.
`text` im MCP-Werkzeug wird zu `beschreibung` in HTTP. Der HTTP-Vertrag weist unbekannte
Felder ab. Die Erlaubnisreferenz in gelesenen neuen Aufträgen steht unter `auftrag.w1`;
Altaufträge haben dieses zusätzliche Objekt nicht.

Der Wiederholungsnachweis bindet Schlüsselidentität, Kennung, Pfad, Inhalt und tatsächliches
Projekt. Reservierung, Fachwirkung und Antwortspeicherung teilen eine Transaktion. Gleicher
Inhalt liefert die gespeicherte Antwort mit `wiederholt: true`, abweichender Inhalt oder
Projektkontext ergibt 409. Nach Erlaubniswiderruf darf eine weiterhin angemeldete Chatbindung
eine identische gespeicherte Antwort lesen; das führt keine Aktion erneut aus. Nach
Zugangswiderruf ist auch dieser Zugang gesperrt. Ein neuer Aufruf prüft die Erlaubnis neu.

Beispiel einer Anlage in einem synthetischen Testsystem; IDs müssen zuvor aus den eigenen
Erlaubnissen gelesen werden, sie dürfen nicht erraten werden:

```json
{
  "vertrag": "kipm-chat-delegation/1",
  "anfrage_kennung": "eigene-stabile-kennung-1",
  "delegation_id": 1,
  "delegation_revision": 1,
  "budget": {"waehrung": "USD", "geld_mikro": 1000000, "zeit_ms": 60000, "aufrufe": 1},
  "titel": "Anmeldehinweis verbessern",
  "beschreibung": "Der Hinweis soll den nächsten Schritt nennen.",
  "ziel": "Verständlicher Hinweis",
  "nicht_ziele": "Keine Änderung der Anmeldung",
  "schreibumfang": ["src/auth/**"],
  "abnahmekriterien": ["Hinweis sichtbar und verständlich"],
  "zuweisen": true,
  "freigeben": true
}
```

Bei fehlendem Teilrecht wird die gesamte Anlage zurückgerollt. Kein Fachauftrag oder
Teilbudget bleibt zurück. Ein fremdes Projekt, ein falscher Chat, abgelaufene oder widerrufene
Rechte, eine veraltete Fassung und ein verändertes Ziel sind keine zulässigen Ersatzbelege.
Eine Neuplanung verändert das ursprüngliche Auftragsbudget und die Erlaubniszuordnung nicht.

## Entwertung und Betriebsgrenze

Erlaubnis-/Zugangswiderruf sowie relevante Änderungen an Arbeitsort oder Zielzulassung
entwerten passende ausstehende Freigaben; die Zielzuweisungsfassung steigt und der gebundene
Kontext wird gelöscht. Laufende Ausführungen erhalten ausschließlich eine Haltanforderung.
Eine Haltanforderung ist weder Prozessende noch lokale Endquittung. Zeitablauf und
Ausstellerrechte werden vor der nächsten berechtigungspflichtigen Wirkung erneut geprüft;
ein zusätzlicher Hintergrundwächter wird nicht behauptet.

Reserviertes Budget wird nach einer Ausführung nicht allein wegen eines Datenbankstatus
freigegeben. Vor jemals begonnener Ausführung kann der tatsächliche Owner die Freigabe
zurücknehmen. Widerruf erfindet keine Rückerstattung. Verbrauchs- und Endnachweisabgleich
für echte Ausführungen gehört zur v2-Gegenstelle.

Neue delegierte Aufträge werden im alten Werkbank-v1-Abgleich nicht angeboten. Auch ein
direkter v1-Übernahmeversuch wird vor der Ausführung abgewiesen. Unveränderte Altaufträge
bleiben im bisherigen v1-Vertrag. Migration 0022 ist additiv; sie erteilt keinem Altauftrag
oder alten Schlüssel neue Rechte. Produktive Migration, Aktivierung und Anbieterläufe
bedürfen ihrer konkreten Betriebs- beziehungsweise Kostenfreigabe.

Die ausführbaren positiven und negativen Fälle stehen in `tests/test_werkbank_delegation.py`,
der Schutz laufender Ausführungen zusätzlich in `tests/test_werkbank_abschluss.py`.

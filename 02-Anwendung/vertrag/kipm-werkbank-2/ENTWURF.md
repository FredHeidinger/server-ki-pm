# C02 – W1-Transport kipm-werkbank/2 / Entwurf 1

Führend: KP02 in KI-PM. Verbraucher: WB05/WB06; Zielversion ist `kipm-werkbank/2`, `snapshot_version: 2`. Dieser Entwurf ersetzt weder v1-Schemas noch den aktiven Versionskonstantenwert. Die finalen JSON-Schemas und identischen Produzent-/Verbrauchertests sind ein Liefergate von KP02.

## Hülle und Snapshot

Hülle: Pflichtfelder `vertrag`, `operation_id`, `art`, `quelle`, `nutzlast`, `inhaltshash`. Quellbindung übernimmt v1-Felder server_id, werkbank_id, projektbindung_id, projektbindung_version, auftrag_id, revision, zuweisung_version; nach Übernahme zusätzlich ausfuehrung_id und snapshot_sha256. Keine Zahl als String. Identische Nachricht unter gleicher Operation nur einmal anwenden; anderer Inhalt unter gleicher Operation ist Konflikt. Wiederholungsschutz wird vor Wirkung reserviert und zusammen mit Ereignis/Antwort dauerhaft abgeschlossen.

Snapshot v2 enthält alle v1-Pflichtinhalte (auftrag_id, nummer, titel, revision, beschreibung, ziel, nicht_ziele, schreibumfang, arbeitsort, abnahmekriterien, quellenhinweise, profil, entscheidungen, texte, links) und zusätzlich `zustaendigkeit`, `delegation_ref`, `quellenmanifest`, `zielverbund`, `snapshot_version: 2`. Quellenreferenzen sind keine Lesebestätigung. Alle Pflichtabschnitte werden über C03 geladen und geprüft, bevor ein davon abhängiger Schritt freigegeben wird.

`zielverbund` enthält 1..100 Einträge mit repo_id, projektbindung_id/version, Arbeitsortbindung und Basiscommit sowie `integrationseinheiten` mit geordneten Repo-IDs und expliziter Atomizitätsgrenze. Jeder Teil braucht eigene aktuelle Rechte; ein Recht auf Repo A gilt nicht für B. „Gemeinsam“ darf keine globale Git-Atomizität behaupten. Arbeitsorte wählt der Server ausschließlich aus bestätigten Bindungen; mehrere mögliche Ziele ohne Standard erzeugen eine Frage.

## Nachrichten und Reihenfolge

Die Arten uebernehmen, schritt, ereignisse bleiben fachlich erhalten. Hinzu kommen explizite lesende Abfragen `operation`, `quellen_seite`, `faehigkeiten`; schreibende Aufrufe bleiben im bestehenden operation_ausfuehren-Pfad. Fähigkeiten werden nach authentifizierter Verbindung als Schnittmenge ausgehandelt; fehlt v2, wird kein v2-Auftrag als v1 gesendet. Laufende v1-Jobs bleiben auf v1 oder werden bestätigt beendet und neu freigegeben.

Schrittanfrage enthält aktuelle Quelle, Plan-/Versuchs-/Schrittkennung, C01-Referenzen und gebundene Hashes. Antwort erlaubt/abgelehnt/unklar plus einmaliges C01-Ticket. Bei Netzverlust keine neue autorisierungspflichtige Aktion; lokaler Notstopp bleibt möglich. Ereignis = ereignis_id, streng steigende sequenz je ausfuehrung_id, statusversion, art und daten. Gleiche Sequenz+Hash ist Wiederholung; andere Daten Konflikt; Lücke verlangt Nachladen ab letzter lückenloser Sequenz. Lebenszeichen erhöht keine Fachrevision und gilt nicht als Wirkungsquittung.

Quellenseite enthält manifest_sha256, quelle_id, version, segment_id, offset, laenge, gesamt_bytes, inhalt_sha256, naechster_cursor (null nur am belegten Ende), Leserechtsreferenz und Daten. Maximal 100 Einträge/1 MiB; Inhalt vollständig zusammenbauen und Original-/Segmenthash prüfen. Cursor ist an Snapshot, Akteur und Leserecht gebunden. Abgelaufenes/widerrufenes Recht stoppt Nachladen; keine Credentials an Modelle. Altdateien nur bei bestätigter Freigabe und bestehender Sauberkeitsprüfung, Pfade nie aus Chat beliebig auflösen.

Antwort auf Frage/Aktion nennt id, erwartete Revision und Operation. Erst nach tatsächlicher Anwendung kommt antwort_angewendet/aktion_angewendet mit neuer Revision; Eingangsquittung ist nur angefordert. Ein konkurrierender veralteter Versuch wird abgewiesen. Fachfragen in KI-PM, technische Einrichtungsfragen als Verweis auf Werkbank; gemischte Fragen verknüpft trennen.

Abschluss trägt C04-Nachweise je Kriterium/Paket/Repo und Gesamtprüf-/Nutzungsbelege. Teilintegriert und Abschluss blockiert sind ausdrücklich mögliche Ergebnisse, kein Erledigt. Chat-Lieferautomatik bleibt ein anderer Dienst: Lieferung/Bedarf startet keine Werkbankarbeit und erteilt keine Rechte.

## Fehler, Kompatibilität und Rückweg

VERTRAG_INKOMPATIBEL, SNAPSHOT_UNVOLLSTAENDIG, QUELLE_NICHT_FREIGEGEBEN, SEQUENZ_LUECKE, OPERATION_KONFLIKT, REVISION_VERALTET, WIRKUNG_UNKLAR, ABSCHLUSS_UNVOLLSTAENDIG. Nutzertext nennt fehlenden Inhalt, nächste Aktion und bekannte Wirkung, nie „nicht ausgeführt“ bei unklarem Ausgang. Bestehende 401/403 bleiben unverändert.

Matrix: v1↔v1 unverändert; v2↔v2 erst nach gemeinsamem Schema-/Vektorbeleg; v2→v1 und v1 mit Zusatzfeldern abweisen; unbekannte Version kein Fallback. Einführung Produzent zuerst kompatibel, Verbraucher danach, Aktivierung einzeln. Additive Datenmigration auf Kopie mit Bestandstest; kein Rollen-/Rechtezuwachs für Altzugänge. Rücknahme mit passendem Daten-/Konfigurationsstand und Operations-/Integrationsabgleich, keine Neuausführung durch Restore.


Status: **AP01-Vertragsentwurf 1, zur Implementierung bestimmt; nicht aktiviert und noch nicht als Gegenstellenvertrag abgenommen.**
Grundlage: [Umsetzungsplan](../../../../07-UMSETZUNGSPLAN-DREI-WEGE.md), C01–C06. Die aktuelle Laufzeit bleibt unverändert.

Gemeinsame Kodierung: UTF-8-JSON, Objekte ohne doppelte Schlüssel, keine Gleitkommazahlen/NaN/Infinity, Ganzzahlen im Bereich ±(2^53−1), keine implizite Typumwandlung. Unbekannte Pflichtfassungen, zusätzliche Felder, fehlende Pflichtfelder und null statt Pflichtwert werden abgewiesen. Identifikatoren sind nichtleere ASCII-Zeichenfolgen `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`; SHA-256 sind 64 kleine Hexzeichen. Geld in ganzzahligen Millionsteln mit expliziter ISO-Währung; Zeitdauern in ganzen Millisekunden, Zeitpunkte UTC mit `Z`. Kein Token/Passwort/Schlüssel im Vertrag, Prompt oder Nachweis. Generierte Kennungen sind keine Zugangsdaten.

Objekt- und Listenlimits sind Ablehnungsgrenzen, keine Erlaubnis zum Abschneiden. Standardgrenzen: eine Nachricht höchstens 1 MiB UTF-8, ID 128 Zeichen, sichtbarer Freitext 16 000 Zeichen, Listen höchstens 100 Einträge je Seite, feste Gesamtlänge und Hash für paginierte Inhalte. Ein erforderlicher größerer Inhalt wird autorisiert segmentiert oder vor Wirkung blockiert. Vollständigkeit ist nach Rückladen zu prüfen. Engere produktspezifische Altgrenzen bleiben erhalten.

Kanonischer Hash: bestehende v1-JSON-Regel beibehalten (UTF-8, Schlüssel nach Codepunkt, kompakt, Unicode unmaskiert, kein Float); ein Inhaltshash umfasst alle Felder außer sich selbst. Hash beweist Gleichheit, keine Berechtigung. Zeiger auf vertrauliche Quellen sind ausschließlich mit dem aktuellen Leserecht verwendbar.

Fehlerantwort: `code`, `text`, `operation_id`, `wirksamkeit` (keine/angewendet/unklar), `naechste_aktion`; kein Stacktrace, Secret oder Quellinhalt. Zeitüberschreitung ist unklarer Ausgang, niemals automatisch keine Wirkung. Bei verlorener Antwort zuerst Operationsabgleich; keine neue Kennung, um eine unklare Aktion erneut auszuführen. Erwartete Ablehnungen verbrauchen keine Ausführungsrechte.

# Gemeinsame Werkbank-Vertragsfassungen abfragen

Ein Client sendet `GET /api/werkbank/faehigkeiten/` mit seinem Werkbank-Bearer und einem Queryparameter `vertrag` je angebotener Fassung. Er sendet den Kopf `X-KIPM-Vertrag` erst beim anschließenden Aufruf eines versionierten Endpunkts. Die Abfrage berührt weder den letzten Kontakt noch eine Zuweisung oder Ausführung.

Das Angebot umfasst eine bis 100 unterschiedliche Kennungen in der Form `kipm-werkbank/<ganze positive Versionsnummer>`. Die Versionsnummer verwendet ASCII-Ziffern ohne führende Null. Jede Kennung darf bis zu 64 Zeichen enthalten. Weitere Queryparameter und ein Vertragskopf bei der Abfrage führen zu HTTP 400.

Bei HTTP 200 enthält die Antwort `schema_version` mit dem ganzzahligen Wert 1 und `gemeinsam` mit den angebotenen, serverseitig implementierten Fassungen. Der Server unterstützt zurzeit `kipm-werkbank/1`. Eine leere Schnittmenge ergibt HTTP 426. Die Fehlerhülle entspricht dem bestehenden Werkbank-Fehlerformat. Ein fehlender oder widerrufener Zugang ergibt 401, ein Chat-Zugang 403. Die Antwort trägt `Cache-Control: no-store`.

Ein Client, dessen Auftrag v2 erfordert, darf nur dann fortfahren, wenn v2 in `gemeinsam` enthalten ist. Bei einem älteren Server ohne diesen Endpunkt bleibt der erforderliche Vertrag unbestätigt. Ein bereits an v1 gebundener Auftrag nutzt weiterhin die bestehenden v1-Endpunkte und den v1-Vertragskopf.

Die [gemeinsamen Testvektoren](vektoren.json) beschreiben die Anfragen über die HTTP-Grenze. Jeder Vektor setzt einen aktiven Werkbank-Zugang voraus.

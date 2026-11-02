# Chats anbinden

Die vollständigen Anleitungen stehen in Server-KI-PM unter **Verbindungen**: je Verbindungsart (Claude Code per MCP, Kommandozeile für andere Chats, HTTP-Schnittstelle) mit Voraussetzungen, Schritten, kopierbaren Befehlen, eingesetztem Schlüssel und „Verbindung prüfen“. Diese Datei ist nur ein Hinweis darauf.

Kurzfassung:

- **Claude Code:** Verbindungen → Claude Code (MCP) → Zugang anlegen → `kipm_mcp.py`, `kipm.py`, `kipm-root.crt` nach `%USERPROFILE%\.kipm`, `konfig.ini` mit dem angezeigten Inhalt, dann `claude mcp add --scope user kipm -- python "%USERPROFILE%\.kipm\kipm_mcp.py"`. Das Projekt ergibt sich aus dem Ordner (Arbeitsort des Projekts).
- **Andere Chats:** Verbindungen → Kommandozeile → Zugang für das Projekt anlegen → `.kipm` im Projektordner, Regeltext aus der Anleitung in den Chat einfügen.
- **Eigene Integrationen:** Verbindungen → HTTP-Schnittstelle: Kopfzeile `Authorization: Bearer …`, Pfade und Beispiel dort.

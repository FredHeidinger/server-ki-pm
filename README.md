# Server-KI-PM

Zentrale Auftragssteuerung für Softwareprojekte mit KI-Chats: Browseroberfläche für den Menschen, begrenzte HTTP-Schnittstelle und Kommandozeile für Projektchats, SQLite hinter einem Django-Dienst.

## Installation auf Proxmox

Auf dem Proxmox-Wirt in der Shell (als root oder mit sudo):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/FredHeidinger/server-ki-pm/main/03-Server/skripte/proxmox-installieren.sh)"
```

Legt einen eigenen Debian-13-Container an, installiert Dienst, TLS-Proxy und tägliche Sicherung, fragt das Passwort für das Owner-Konto ab und zeigt die Adresse. Aktualisieren: dieselbe Zeile mit `KIPM_CT=<Container-ID>` davor.

## Inhalt

| Ordner | Inhalt |
|---|---|
| `02-Anwendung` | Django-Anwendung, HTTP-Schnittstelle, Kommandozeile `client/kipm.py`, Tests |
| `03-Server/skripte` | Installations-, Sicherungs- und Wiederherstellungsskripte, systemd-Unit, Caddy-Vorlage |

Lokal entwickeln: in `02-Anwendung` `uv sync`, `uv run pytest`, `uv run manage.py migrate`, `uv run manage.py konto_anlegen <name>`, `uv run manage.py runserver`.

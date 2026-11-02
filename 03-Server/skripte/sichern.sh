#!/usr/bin/env bash
# Konsistente Sicherung: SQLite über die Backup-API (WAL-sicher) plus Konfiguration.
# Ergebnis: /var/backups/kipm/kipm-JJJJMMTT-HHMM.tar.gz; die letzten 14 bleiben erhalten.
# Die Sicherung gehört zusätzlich auf ein getrenntes Ziel (Storage Box, anderer Rechner); das ist
# ein eigener Schritt mit eigener Freigabe und steht nicht in diesem Skript.
set -euo pipefail

DATEN=${KIPM_DATEN_DIR:-/var/lib/kipm}
ZIEL=/var/backups/kipm
STEMPEL=$(date +%Y%m%d-%H%M)
ARBEIT=$(mktemp -d)
trap 'rm -rf "$ARBEIT"' EXIT

/opt/kipm/venv/bin/python - "$DATEN/kipm.sqlite3" "$ARBEIT/kipm.sqlite3" <<'EOF'
import sqlite3, sys
quelle, ziel = sys.argv[1], sys.argv[2]
with sqlite3.connect(quelle) as q, sqlite3.connect(ziel) as z:
    q.backup(z)
with sqlite3.connect(ziel) as z:
    assert z.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Sicherung nicht integer"
EOF

cp /etc/kipm/kipm.env "$ARBEIT/kipm.env"
cp /etc/caddy/Caddyfile "$ARBEIT/Caddyfile"
[ -f "$DATEN/.secret_key" ] && cp "$DATEN/.secret_key" "$ARBEIT/.secret_key"
tar -C "$ARBEIT" -czf "$ZIEL/kipm-$STEMPEL.tar.gz" .
chmod 0640 "$ZIEL/kipm-$STEMPEL.tar.gz"
ls -1t "$ZIEL"/kipm-*.tar.gz | tail -n +15 | xargs -r rm -f
sha256sum "$ZIEL/kipm-$STEMPEL.tar.gz"

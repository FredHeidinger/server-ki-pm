#!/usr/bin/env bash
# Konsistente Sicherung: SQLite über die Backup-API (WAL-sicher), alle darin referenzierten
# Unterlagendateien mit Manifest (SHA-256) und die Konfiguration.
# Ergebnis: /var/backups/kipm/kipm-JJJJMMTT-HHMM.tar.gz; die letzten 14 bleiben erhalten.
# Während der Sicherung sperrt eine Sperrdatei die Bereinigung der Ablage. Fehlt eine
# referenzierte Datei oder weicht ihr Hash ab, bricht die Sicherung ab.
# Die Sicherung gehört zusätzlich auf ein getrenntes Ziel (Storage Box, anderer Rechner); das ist
# ein eigener Schritt mit eigener Freigabe und steht nicht in diesem Skript.
set -euo pipefail

DATEN=${KIPM_DATEN_DIR:-/var/lib/kipm}
ABLAGE=${KIPM_ABLAGE_DIR:-$DATEN/ablage}
ZIEL=/var/backups/kipm
STEMPEL=$(date +%Y%m%d-%H%M)
ARBEIT=$(mktemp -d)

# Die Ablage legt der Installer als kipm an; hier nur die Sperre, wenn es sie gibt.
[ -d "$ABLAGE" ] && touch "$ABLAGE/.sicherung-laeuft"
trap 'rm -rf "$ARBEIT"; rm -f "$ABLAGE/.sicherung-laeuft"' EXIT

/opt/kipm/venv/bin/python - "$DATEN/kipm.sqlite3" "$ARBEIT/kipm.sqlite3" "$ABLAGE" "$ARBEIT" <<'EOF'
import hashlib, os, shutil, sqlite3, sys
quelle, ziel, ablage, arbeit = sys.argv[1:5]
with sqlite3.connect(quelle) as q, sqlite3.connect(ziel) as z:
    q.backup(z)
with sqlite3.connect(ziel) as z:
    assert z.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Sicherung nicht integer"
    tabellen = {r[0] for r in z.execute("select name from sqlite_master where type='table'")}
    blobs = []
    if "steuerung_unterlagenversion" in tabellen:
        blobs = z.execute(
            "select blob, sha256 from steuerung_unterlagenversion where blob <> ''"
        ).fetchall()
os.makedirs(os.path.join(arbeit, "ablage", "blobs"), exist_ok=True)
zeilen = []
for blob, sha in blobs:
    quelle_blob = os.path.join(ablage, "blobs", blob)
    ziel_blob = os.path.join(arbeit, "ablage", "blobs", blob)
    shutil.copyfile(quelle_blob, ziel_blob)
    with open(ziel_blob, "rb") as f:
        ist = hashlib.sha256(f.read()).hexdigest()
    assert ist == sha, f"Hash weicht ab: {blob}"
    zeilen.append(f"{sha}  ablage/blobs/{blob}")
with open(os.path.join(arbeit, "MANIFEST.sha256"), "w", encoding="utf-8") as f:
    f.write("\n".join(zeilen) + ("\n" if zeilen else ""))
print(f"Datenbank integer, {len(zeilen)} Datei(en) gesichert")
EOF

cp /etc/kipm/kipm.env "$ARBEIT/kipm.env"
cp /etc/caddy/Caddyfile "$ARBEIT/Caddyfile"
[ -f "$DATEN/.secret_key" ] && cp "$DATEN/.secret_key" "$ARBEIT/.secret_key"
tar -C "$ARBEIT" -czf "$ZIEL/kipm-$STEMPEL.tar.gz" .
chmod 0640 "$ZIEL/kipm-$STEMPEL.tar.gz"
ls -1t "$ZIEL"/kipm-*.tar.gz | tail -n +15 | xargs -r rm -f
sha256sum "$ZIEL/kipm-$STEMPEL.tar.gz"

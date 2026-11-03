#!/usr/bin/env bash
# Wiederherstellung aus einer Sicherung. Zwei Betriebsarten:
#   kipm-wiederherstellen --pruefen  DATEI.tar.gz   Restore in einen Testordner: Integrität,
#                                                   Tabellenzahl und jede referenzierte Datei mit
#                                                   Hash prüfen; nichts Produktives anfassen.
#   kipm-wiederherstellen --scharf   DATEI.tar.gz   Dienst stoppen, aktuelle DB und Ablage
#                                                   beiseitelegen, gesicherten Stand einsetzen,
#                                                   Dienst starten.
# Fehlt eine referenzierte Datei oder weicht ein Hash ab, ist die Wiederherstellung nicht
# erfolgreich und es wird nichts eingesetzt.
# Rücknahme der scharfen Wiederherstellung: die beiseitegelegten Stände kipm.sqlite3.vor-restore-*
# und ablage.vor-restore-* zurückverschieben und den Dienst neu starten.
set -euo pipefail

MODUS=${1:-}; DATEI=${2:-}
DATEN=${KIPM_DATEN_DIR:-/var/lib/kipm}
ABLAGE=${KIPM_ABLAGE_DIR:-$DATEN/ablage}
[ -f "$DATEI" ] || { echo "Sicherungsdatei fehlt: $DATEI"; exit 2; }
ARBEIT=$(mktemp -d); trap 'rm -rf "$ARBEIT"' EXIT
tar -C "$ARBEIT" -xzf "$DATEI"

pruefen() {
  /opt/kipm/venv/bin/python - "$1" "$2" <<'EOF'
import hashlib, os, sqlite3, sys
datenbank, ablage = sys.argv[1], sys.argv[2]
with sqlite3.connect(datenbank) as z:
    assert z.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Datenbank nicht integer"
    tabellen = {r[0] for r in z.execute("select name from sqlite_master where type='table'")}
    auftraege = z.execute("select count(*) from steuerung_auftrag").fetchone()[0]
    ereignisse = z.execute("select count(*) from steuerung_ereignis").fetchone()[0]
    blobs = []
    if "steuerung_unterlagenversion" in tabellen:
        blobs = z.execute(
            "select blob, sha256 from steuerung_unterlagenversion where blob <> ''"
        ).fetchall()
fehler = []
for blob, sha in blobs:
    pfad = os.path.join(ablage, "blobs", blob)
    if not os.path.exists(pfad):
        fehler.append(f"fehlt {blob}")
        continue
    with open(pfad, "rb") as f:
        if hashlib.sha256(f.read()).hexdigest() != sha:
            fehler.append(f"Hash weicht ab {blob}")
assert not fehler, "Wiederherstellung unvollständig: " + "; ".join(fehler[:10])
print(f"integer: ja, Tabellen: {len(tabellen)}, Aufträge: {auftraege}, Ereignisse: {ereignisse}, "
      f"Dateien: {len(blobs)} mit passendem Hash")
EOF
}

case "$MODUS" in
  --pruefen)
    pruefen "$ARBEIT/kipm.sqlite3" "$ARBEIT/ablage"
    echo "Probewiederherstellung erfolgreich; Produktivdaten unverändert."
    ;;
  --scharf)
    pruefen "$ARBEIT/kipm.sqlite3" "$ARBEIT/ablage"
    systemctl stop kipm
    STEMPEL=$(date +%Y%m%d-%H%M%S)
    [ -f "$DATEN/kipm.sqlite3" ] && mv "$DATEN/kipm.sqlite3" "$DATEN/kipm.sqlite3.vor-restore-$STEMPEL"
    rm -f "$DATEN/kipm.sqlite3-wal" "$DATEN/kipm.sqlite3-shm"
    install -o kipm -g kipm -m 0640 "$ARBEIT/kipm.sqlite3" "$DATEN/kipm.sqlite3"
    if [ -d "$ARBEIT/ablage" ]; then
      [ -d "$ABLAGE" ] && mv "$ABLAGE" "$ABLAGE.vor-restore-$STEMPEL"
      mkdir -p "$ABLAGE"
      cp -a "$ARBEIT/ablage/." "$ABLAGE/"
      chown -R kipm:kipm "$ABLAGE"
      chmod -R u=rwX,go= "$ABLAGE"
    fi
    systemctl start kipm
    echo "Wiederhergestellt. Vorherige Stände: $DATEN/kipm.sqlite3.vor-restore-$STEMPEL, $ABLAGE.vor-restore-$STEMPEL"
    ;;
  *)
    echo "Aufruf: $0 --pruefen|--scharf DATEI.tar.gz"; exit 2 ;;
esac

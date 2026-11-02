#!/usr/bin/env bash
# Wiederherstellung aus einer Sicherung. Zwei Betriebsarten:
#   kipm-wiederherstellen --pruefen  DATEI.tar.gz   Restore in einen Testordner, Integrität und
#                                                   Tabellenzahl prüfen, nichts Produktives anfassen.
#   kipm-wiederherstellen --scharf   DATEI.tar.gz   Dienst stoppen, aktuelle DB beiseitelegen,
#                                                   gesicherte DB einsetzen, Dienst starten.
# Rücknahme der scharfen Wiederherstellung: die beiseitegelegte Datei kipm.sqlite3.vor-restore-*
# zurückkopieren und den Dienst neu starten.
set -euo pipefail

MODUS=${1:-}; DATEI=${2:-}
DATEN=${KIPM_DATEN_DIR:-/var/lib/kipm}
[ -f "$DATEI" ] || { echo "Sicherungsdatei fehlt: $DATEI"; exit 2; }
ARBEIT=$(mktemp -d); trap 'rm -rf "$ARBEIT"' EXIT
tar -C "$ARBEIT" -xzf "$DATEI"

pruefen() {
  /opt/kipm/venv/bin/python - "$1" <<'EOF'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as z:
    assert z.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Datenbank nicht integer"
    tabellen = z.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
    auftraege = z.execute("select count(*) from steuerung_auftrag").fetchone()[0]
    ereignisse = z.execute("select count(*) from steuerung_ereignis").fetchone()[0]
print(f"integer: ja, Tabellen: {tabellen}, Aufträge: {auftraege}, Ereignisse: {ereignisse}")
EOF
}

case "$MODUS" in
  --pruefen)
    pruefen "$ARBEIT/kipm.sqlite3"
    echo "Probewiederherstellung erfolgreich; Produktivdaten unverändert."
    ;;
  --scharf)
    pruefen "$ARBEIT/kipm.sqlite3"
    systemctl stop kipm
    STEMPEL=$(date +%Y%m%d-%H%M%S)
    [ -f "$DATEN/kipm.sqlite3" ] && mv "$DATEN/kipm.sqlite3" "$DATEN/kipm.sqlite3.vor-restore-$STEMPEL"
    rm -f "$DATEN/kipm.sqlite3-wal" "$DATEN/kipm.sqlite3-shm"
    install -o kipm -g kipm -m 0640 "$ARBEIT/kipm.sqlite3" "$DATEN/kipm.sqlite3"
    systemctl start kipm
    echo "Wiederhergestellt. Vorherige Datenbank: $DATEN/kipm.sqlite3.vor-restore-$STEMPEL"
    ;;
  *)
    echo "Aufruf: $0 --pruefen|--scharf DATEI.tar.gz"; exit 2 ;;
esac

#!/usr/bin/env bash
# Installation von Server-KI-PM IN einem Debian-13-Container (nicht auf dem Proxmox-Wirt).
# Voraussetzungen: Quellcode unter /opt/kipm/quelle, Konfiguration in /etc/kipm/kipm.env
# (Vorlage: 03-Server/skripte/kipm.env.beispiel). Idempotent: erneutes Ausführen aktualisiert
# Code und Abhängigkeiten und lässt Daten unberührt. Wird von proxmox-installieren.sh aufgerufen.
set -euo pipefail

QUELLE=/opt/kipm/quelle/02-Anwendung
SKRIPTE=/opt/kipm/quelle/03-Server/skripte
APP=/opt/kipm/app
VENV=/opt/kipm/venv
DATEN=/var/lib/kipm
SICHERUNG=/var/backups/kipm

[ -f /etc/kipm/kipm.env ] || { echo "/etc/kipm/kipm.env fehlt (Vorlage: $SKRIPTE/kipm.env.beispiel)."; exit 2; }
set -a; . /etc/kipm/kipm.env; set +a

echo "== Pakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip rsync caddy >/dev/null

echo "== Dienstkonto und Ordner"
id kipm >/dev/null 2>&1 || useradd --system --home-dir /opt/kipm --shell /usr/sbin/nologin kipm
# Datenordner nur durchquerbar (Caddy liest static), Datenbank bleibt 0640 kipm:kipm
install -d -o kipm -g kipm -m 0751 "$DATEN"
install -d -o kipm -g kipm -m 0755 "$DATEN/static"
install -d -o root -g kipm -m 0750 /etc/kipm "$SICHERUNG"
install -d -o root -g root -m 0755 /opt/kipm "$APP"
chown root:kipm /etc/kipm/kipm.env; chmod 0640 /etc/kipm/kipm.env

echo "== Code aktualisieren (nur Anwendung, keine Daten)"
rsync -a --delete --exclude daten --exclude .venv --exclude __pycache__ --exclude .pytest_cache "$QUELLE/" "$APP/"
chown -R root:kipm "$APP"
chmod -R g+rX,o-rwx "$APP"

echo "== Python-Umgebung"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"

echo "== Datenbank migrieren und statische Dateien sammeln"
runuser -u kipm --preserve-environment -- "$VENV/bin/python" "$APP/manage.py" migrate --noinput
runuser -u kipm --preserve-environment -- "$VENV/bin/python" "$APP/manage.py" collectstatic --noinput --ignore __pycache__ >/dev/null
chmod -R a+rX "$DATEN/static"

echo "== TLS-Proxy (Caddy) für: ${KIPM_ALLOWED_HOSTS:-localhost}"
{
  echo "# erzeugt von installieren.sh aus KIPM_ALLOWED_HOSTS"
  echo "$(echo "${KIPM_ALLOWED_HOSTS:-localhost}" | tr ',' ' ') {"
  cat "$SKRIPTE/Caddyfile.rumpf"
  echo "}"
} > /etc/caddy/Caddyfile

echo "== Dienste"
install -m 0644 "$SKRIPTE/kipm.service" /etc/systemd/system/kipm.service
install -m 0750 "$SKRIPTE/sichern.sh" /usr/local/sbin/kipm-sichern
install -m 0750 "$SKRIPTE/wiederherstellen.sh" /usr/local/sbin/kipm-wiederherstellen
systemctl daemon-reload
systemctl enable kipm >/dev/null 2>&1
systemctl restart kipm
systemctl enable caddy >/dev/null 2>&1
systemctl restart caddy

echo "== Tägliche Sicherung 03:30"
printf '30 3 * * * root /usr/local/sbin/kipm-sichern >>/var/log/kipm-sicherung.log 2>&1\n' > /etc/cron.d/kipm-sicherung

echo "== Zertifikat der internen CA für Clients bereitstellen (/static/kipm-root.crt)"
for _ in $(seq 1 15); do
  WURZEL=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
  [ -f "$WURZEL" ] && { install -m 0644 "$WURZEL" "$DATEN/static/kipm-root.crt"; break; }
  sleep 1
done

sleep 2
systemctl is-active --quiet kipm && echo "   Dienst kipm läuft." || { echo "!! Dienst kipm läuft nicht: journalctl -u kipm"; exit 3; }
systemctl is-active --quiet caddy && echo "   Proxy caddy läuft." || { echo "!! caddy läuft nicht: journalctl -u caddy"; exit 3; }

#!/usr/bin/env bash
# Einzeiler-Installation von Server-KI-PM auf einem Proxmox-Wirt.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/FredHeidinger/server-ki-pm/main/03-Server/skripte/proxmox-installieren.sh)"
#
# Legt einen eigenen unprivilegierten Debian-13-Container an (nächste freie ID, DHCP auf vmbr0),
# holt den Code aus dem Repository, installiert Dienst, TLS-Proxy und Sicherung, fragt das
# Passwort für das Owner-Konto ab und zeigt am Ende die Adresse. Läuft als root oder mit sudo.
# Erneut ausführen mit KIPM_CT=<ID> aktualisiert einen bestehenden Container.
set -euo pipefail

REPO=${KIPM_REPO:-https://github.com/FredHeidinger/server-ki-pm.git}
ZWEIG=${KIPM_ZWEIG:-main}
HOSTNAME_CT=${KIPM_HOSTNAME:-kipm}
BRIDGE=${KIPM_BRIDGE:-vmbr0}
KONTO=${KIPM_KONTO:-fred}

[ "$(id -u)" = 0 ] || exec sudo -E bash "$0" "$@"
command -v pct >/dev/null || { echo "Dieses Skript läuft nur auf einem Proxmox-Wirt."; exit 1; }

echo "== Vorlage Debian 13"
pveam update >/dev/null
VORLAGE=$(pveam available --section system | awk '/debian-13-standard/ {print $2}' | sort | tail -1)
[ -n "$VORLAGE" ] || { echo "Keine Debian-13-Vorlage gefunden."; exit 1; }
pveam list local | grep -q "$VORLAGE" || pveam download local "$VORLAGE"

if [ -n "${KIPM_CT:-}" ]; then
  ID=$KIPM_CT
  echo "== Bestehender Container $ID wird aktualisiert"
else
  ID=$(pvesh get /cluster/nextid)
  SPEICHER=$(pvesm status --content rootdir | awk 'NR>1 && $3=="active" {print $1}' | grep -m1 -E '^local-lvm$|^local-zfs$' || pvesm status --content rootdir | awk 'NR>1 && $3=="active" {print $1; exit}')
  [ -n "$SPEICHER" ] || { echo "Kein Speicher für Container gefunden."; exit 1; }
  echo "== Container $ID anlegen (Speicher $SPEICHER, Netz $BRIDGE per DHCP)"
  pct create "$ID" "local:vztmpl/$VORLAGE" \
    --hostname "$HOSTNAME_CT" --unprivileged 1 --cores 1 --memory 1024 --swap 512 \
    --rootfs "$SPEICHER:8" --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
    --features nesting=1 --onboot 1 --description "Server-KI-PM (automatisch angelegt)"
  pct start "$ID"
fi

echo "== Warten auf Netz im Container"
for _ in $(seq 1 30); do
  IP=$(pct exec "$ID" -- hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$IP" ] && break
  sleep 2
done
[ -n "${IP:-}" ] || { echo "Container hat keine Adresse bekommen; DHCP auf $BRIDGE prüfen."; exit 1; }
echo "   Adresse: $IP"

echo "== Code holen"
pct exec "$ID" -- bash -c "apt-get update -q && apt-get install -y -q git ca-certificates curl >/dev/null"
pct exec "$ID" -- bash -c "if [ -d /opt/kipm/quelle/.git ]; then git -C /opt/kipm/quelle pull -q; else mkdir -p /opt/kipm && git clone -q --branch '$ZWEIG' '$REPO' /opt/kipm/quelle; fi"

echo "== Konfiguration"
pct exec "$ID" -- bash -c "install -d -m 0750 /etc/kipm; if [ ! -f /etc/kipm/kipm.env ]; then
  GEHEIM=\$(python3 -c 'import secrets; print(secrets.token_urlsafe(50))')
  printf 'KIPM_DEBUG=0\nKIPM_DATEN_DIR=/var/lib/kipm\nKIPM_ALLOWED_HOSTS=$IP,$HOSTNAME_CT,$HOSTNAME_CT.home.arpa\nKIPM_SECRET_KEY=%s\nKIPM_HINTER_PROXY=1\n' \"\$GEHEIM\" > /etc/kipm/kipm.env
  chmod 0640 /etc/kipm/kipm.env
fi"

echo "== Installation im Container"
pct exec "$ID" -- bash /opt/kipm/quelle/03-Server/skripte/installieren.sh

if ! pct exec "$ID" -- bash -c "set -a; . /etc/kipm/kipm.env; set +a; runuser -u kipm -- /opt/kipm/venv/bin/python /opt/kipm/app/manage.py konto_vorhanden" >/dev/null 2>&1; then
  echo "== Owner-Konto „$KONTO“ anlegen"
  while true; do
    read -r -s -p "Passwort für $KONTO (mind. 12 Zeichen): " PW; echo
    read -r -s -p "Passwort wiederholen: " PW2; echo
    [ "$PW" = "$PW2" ] && [ ${#PW} -ge 12 ] && break
    echo "Passwörter ungleich oder zu kurz, bitte erneut."
  done
  printf '%s' "$PW" | pct exec "$ID" -- bash -c "set -a; . /etc/kipm/kipm.env; set +a; KIPM_PASSWORT=\$(cat) KIPM_KONTO='$KONTO' runuser -u kipm --preserve-environment -- /opt/kipm/venv/bin/python /opt/kipm/app/manage.py erstkonto"
  unset PW PW2
fi

echo
echo "================================================================"
echo " Server-KI-PM läuft im Container $ID."
echo " Adresse im Browser:  https://$IP/"
echo " Die Zertifikatswarnung einmal bestätigen (eigener Server, interne CA)."
echo " Anmeldename: $KONTO"
echo " Aktualisieren: dieselbe Zeile erneut mit  KIPM_CT=$ID  davor."
echo "================================================================"

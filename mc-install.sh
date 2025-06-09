#!/usr/bin/env bash
set -euo pipefail
#set -euox pipefail #debug

# --- Logging ---
log() { echo -e "[\033[1;32mINFO\033[0m] $1"; }
warn() { echo -e "[\033[1;33mWARN\033[0m] $1"; }
error() { echo -e "[\033[1;31mERROR\033[0m] $1" >&2; exit 1; }

#https://github.com/settings/tokens
#“Generate new token (classic)”
#nur public_repo
#GITHUB_TOKEN="ghp_pG"

# --- Konfiguration ---
STATE_FILE="/var/log/install-webapp/state.json"
INSTALL_DIR="/var/www/html/webapp"
latest_tag=$(curl -s https://api.github.com/repos/DK5EN/McAdvChat/releases/latest | jq -r .tag_name)
#latest_tag=$(curl -H "Authorization: token $GITHUB_TOKEN" -s https://api.github.com/repos/DK5EN/McAdvChat/releases/latest | jq -r .tag_name)

RELEASE_URL="https://github.com/DK5EN/McAdvChat/releases/download/${latest_tag}/dist.tar.gz"
PY_SCRIPT_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/C2-mc-ws.py"
PY_FILE="/usr/local/bin/C2-mc-ws.py"

#SH_SCRIPT_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/mc-screen.sh"
#SH_FILE="/usr/local/bin/mc-screen.sh"

SV_SCRIPT_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/supervisor.py"
SV_FILE="/usr/local/bin/supervisor.py"

SCRIPT_VERSION="v0.4.0"

MS_LIB="/usr/local/bin/message_storage.py"
MS_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/message_storage.py"
UDP_LIB="/usr/local/bin/udp_handler.py"
UDP_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/udp_handler.py"
WS_LIB="/usr/local/bin/websocket_handler.py"
WS_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/websocket_handler.py"

BLE_LIB="/usr/local/bin/ble_handler.py"
BLE_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/ble_handler.py"

COMMAND_LIB="/usr/local/bin/command_handler.py"
COMMAND_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/command_handler.py"

METEO_LIB="/usr/local/bin/meteo.py"
METEO_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/meteo.py"

MAGIC_LIB="/usr/local/bin/magicword.py"
MAGIC_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/magicword.py"


# --- Sudo-Handling ---
if [[ $EUID -ne 0 ]]; then
  if sudo -n true 2>/dev/null; then
    exec sudo "$0" "$@"
  else
    echo "🔐 Root-Rechte erforderlich. Bitte Passwort eingeben:"
    exec sudo -k bash "$0" "$@"
  fi
fi

# --- User-Erkennung ---
REAL_USER="${SUDO_USER:-$USER}"
log "Skript läuft unter Benutzer: $REAL_USER"

# Prüfen, ob echter Benutzer root ist
if [ "$REAL_USER" = "root" ]; then
  error "❌Fehler: Dieses Skript darf nicht als root ausgeführt werden!"
  exit 1
fi

# --- State-Datei vorbereiten ---
if [[ ! -f "$STATE_FILE" ]]; then
  log "State-Datei nicht gefunden. Erzeuge neue Datei."
  mkdir -p "$(dirname "$STATE_FILE")"
  echo '{}' > "$STATE_FILE"
fi

# --- Hilfsfunktionen ---
get_local_version_file() {
  [[ -f "$1" ]] && grep -oE 'VERSION="v[0-9]+\.[0-9]+\.[0-9]+"' "$1" | cut -d'"' -f2 || echo "v0.0.0"
}

#get_local_webapp_version() {
#  [[ -f "$INSTALL_DIR/version.txt" ]] && cat "$INSTALL_DIR/version.txt" || echo "v0.0.0"
#}

get_local_webapp_version() {
  [[ -f "$INSTALL_DIR/version.html" ]] && cat "$INSTALL_DIR/version.html" || echo "v0.0.0"
}

get_remote_script_version() {
  curl -fsSL "$1" | grep -oE 'VERSION="v[0-9]+\.[0-9]+\.[0-9]+"' | cut -d'"' -f2 || echo "v0.0.0"
}

get_latest_webapp_version() {
  local latest_tag
  latest_tag=$(curl -s https://api.github.com/repos/DK5EN/McAdvChat/releases/latest | jq -r .tag_name)
  #latest_tag=$(curl -H "Authorization: token $GITHUB_TOKEN" -s https://api.github.com/repos/DK5EN/McAdvChat/releases/latest | jq -r .tag_name)

  if [[ -z "$latest_tag" || "$latest_tag" == "null" ]]; then
    echo "Fehler: konnte keine gültige Release-Version von DK5EN/McAdvChat ermitteln." >&2
    return 1
  fi

  echo "$latest_tag"
}

version_gt() {
  [[ "$1" != "$2" ]] && printf '%s\n%s' "$1" "$2" | sort -V | tail -n1 | grep -qx "$1"
}

# --- Lokale Versionen ---
WEBAPP_LOCAL_VERSION=$(get_local_webapp_version)
PY_LOCAL_VERSION=$(get_local_version_file "$PY_FILE")
#SH_LOCAL_VERSION=$(get_local_version_file "$SH_FILE")
SV_LOCAL_VERSION=$(get_local_version_file "$SV_FILE")
SCRIPT_LOCAL_VERSION=$(get_local_version_file "$0")

log "Install-Skript-Version: $SCRIPT_VERSION"


MS_LOCAL_VERSION=$(get_local_version_file "$MS_LIB")
WS_LOCAL_VERSION=$(get_local_version_file "$WS_LIB")
BLE_LOCAL_VERSION=$(get_local_version_file "$BLE_LIB")
UDP_LOCAL_VERSION=$(get_local_version_file "$UDP_LIB")
COMMAND_LOCAL_VERSION=$(get_local_version_file "$COMMAND_LIB")
METEO_LOCAL_VERSION=$(get_local_version_file "$METEO_LIB")
MAGIC_LOCAL_VERSION=$(get_local_version_file "$MAGIC_LIB")


# --- Remote Versionen ---
log "Lokale WebApp-Version: $WEBAPP_LOCAL_VERSION"
WEBAPP_REMOTE_VERSION=$(get_latest_webapp_version)
log "Remote WebApp-Version: $WEBAPP_REMOTE_VERSION"

log "Lokale Python-Skript-Version: $PY_LOCAL_VERSION"
PY_REMOTE_VERSION=$(get_remote_script_version "$PY_SCRIPT_URL")
log "Remote Python-Skript-Version: $PY_REMOTE_VERSION"

#log "Lokale Shell-Skript-Version: $SH_LOCAL_VERSION"
#SH_REMOTE_VERSION=$(get_remote_script_version "$SH_SCRIPT_URL")
#log "Remote Shell-Skript-Version: $SH_REMOTE_VERSION"

log "Lokale Super Visor Skript Version: $SV_LOCAL_VERSION"
SV_REMOTE_VERSION=$(get_remote_script_version "$SV_SCRIPT_URL")
log "Remote Super Visor Skript Version: $SV_REMOTE_VERSION"

log "Lokale Python-MessageStore-Version: $MS_LOCAL_VERSION"
MS_REMOTE_VERSION=$(get_remote_script_version "$MS_LIB_URL")
log "Remote Python-MessageStore-Version: $MS_REMOTE_VERSION"

log "Lokale Python-UDP-Version: $UDP_LOCAL_VERSION"
UDP_REMOTE_VERSION=$(get_remote_script_version "$UDP_LIB_URL")
log "Remote Python-UDP-Version: $UDP_REMOTE_VERSION"

log "Lokale Python-WebSocket-Version: $WS_LOCAL_VERSION"
WS_REMOTE_VERSION=$(get_remote_script_version "$WS_LIB_URL")
log "Remote Python-WebSocket-Version: $WS_REMOTE_VERSION"

log "Lokale Python-Bluetooth-Version: $BLE_LOCAL_VERSION"
BLE_REMOTE_VERSION=$(get_remote_script_version "$BLE_LIB_URL")
log "Remote Python-Bluetooth-Version: $BLE_REMOTE_VERSION"

log "Lokale Command-Handler Version: $COMMAND_LOCAL_VERSION"
COMMAND_REMOTE_VERSION=$(get_remote_script_version "$COMMAND_LIB_URL")
log "Remote Command-Handler Version: $COMMAND_REMOTE_VERSION"

log "Lokale Meteo-Handler Version: $METEO_LOCAL_VERSION"
METEO_REMOTE_VERSION=$(get_remote_script_version "$METEO_LIB_URL")
log "Remote Meteo-Handler Version: $METEO_REMOTE_VERSION"

log "Lokale Python-MagicWord-Version: $MAGIC_LOCAL_VERSION"
MAGIC_REMOTE_VERSION=$(get_remote_script_version "$MAGIC_LIB_URL")
log "Remote Python-MagicWord-Version: $MAGIC_REMOTE_VERSION"

# --- WebApp Update ---
if version_gt "$WEBAPP_REMOTE_VERSION" "$WEBAPP_LOCAL_VERSION"; then
  log "Aktualisiere WebApp von $WEBAPP_LOCAL_VERSION auf $WEBAPP_REMOTE_VERSION"
  if [[ -d "$INSTALL_DIR" ]]; then
    mv "$INSTALL_DIR" "$INSTALL_DIR-$(date +%Y%m%d%H%M%S)"
  fi
  mkdir -p "$INSTALL_DIR"
  curl -fsSL "$RELEASE_URL" | tar -xz --strip-components=1 -C "$INSTALL_DIR"
  chown -R "$REAL_USER":www-data "$INSTALL_DIR"
  chmod -R 775 "$INSTALL_DIR"

  # --- Webserver neu starten ---
  log "Reloade Webserver ..."
  systemctl restart lighttpd || warn "Neustart fehlgeschlagen, versuche Reload"
  #sleep 2 #give some time to start

  TIMEOUT=15
  COUNTER=0
  PID_FILE="/var/run/lighttpd.pid"

  while [ $COUNTER -lt $TIMEOUT ]; do
    if [ -f "$PID_FILE" ]; then
      log "Webserver gestartet (PID: $(cat $PID_FILE))"
      break
    fi
    sleep 1
    COUNTER=$((COUNTER + 1))
  done

if [ $COUNTER -eq $TIMEOUT ]; then
  warn "Timeout: PID-Datei nicht gefunden nach ${TIMEOUT}s"
fi

fi

count=$(ls -1d $INSTALL_DIR-* 2>/dev/null | wc -l)
if [ "$count" -gt 2 ]; then
    ls -1dt $INSTALL_DIR-* | tail -n +3 | xargs rm -rf
    echo "Cleaned up $((count - 2)) old webapp installations"
fi


# --- Python-Skript Update ---
if version_gt "$PY_REMOTE_VERSION" "$PY_LOCAL_VERSION"; then
  log "Aktualisiere Python-Skript von $PY_LOCAL_VERSION auf $PY_REMOTE_VERSION"
  curl -fsSL "$PY_SCRIPT_URL" -o "$PY_FILE"
  chmod +x "$PY_FILE"
fi

# --- Python-MessageStore-Lib Update ---
if version_gt "$MS_REMOTE_VERSION" "$MS_LOCAL_VERSION"; then
  log "Aktualisiere Python-MessageStore-Lib von $MS_LOCAL_VERSION auf $MS_REMOTE_VERSION"
  curl -fsSL "$MS_LIB_URL" -o "$MS_LIB"
  chmod +x "$MS_LIB"
fi

# --- Python-UDP-Lib Update ---
if version_gt "$UDP_REMOTE_VERSION" "$UDP_LOCAL_VERSION"; then
  log "Aktualisiere Python-UDP-Lib von $UDP_LOCAL_VERSION auf $UDP_REMOTE_VERSION"
  curl -fsSL "$UDP_LIB_URL" -o "$UDP_LIB"
  chmod +x "$UDP_LIB"
fi

# --- Python-WebSocket-Lib Update ---
if version_gt "$WS_REMOTE_VERSION" "$WS_LOCAL_VERSION"; then
  log "Aktualisiere Python-WebSocket-Lib von $WS_LOCAL_VERSION auf $WS_REMOTE_VERSION"
  curl -fsSL "$WS_LIB_URL" -o "$WS_LIB"
  chmod +x "$WS_LIB"
fi

# --- Python-BLE-Lib Update ---
if version_gt "$BLE_REMOTE_VERSION" "$BLE_LOCAL_VERSION"; then
  log "Aktualisiere Python-Bluetooth-Lib von $BLE_LOCAL_VERSION auf $BLE_REMOTE_VERSION"
  curl -fsSL "$BLE_LIB_URL" -o "$BLE_LIB"
  chmod +x "$BLE_LIB"
fi

# --- Python-Command-Handler-Lib Update ---
if version_gt "$COMMAND_REMOTE_VERSION" "$COMMAND_LOCAL_VERSION"; then
  log "Aktualisiere Python-Command-Lib von $COMMAND_LOCAL_VERSION auf $COMMAND_REMOTE_VERSION"
  curl -fsSL "$COMMAND_LIB_URL" -o "$COMMAND_LIB"
  chmod +x "$COMMAND_LIB"
fi

# --- Python-Meteo-Handler-Lib Update ---
if version_gt "$METEO_REMOTE_VERSION" "$METEO_LOCAL_VERSION"; then
  log "Aktualisiere Python-Meteo-Lib von $METEO_LOCAL_VERSION auf $METEO_REMOTE_VERSION"
  curl -fsSL "$METEO_LIB_URL" -o "$METEO_LIB"
  chmod +x "$METEO_LIB"
fi

# --- Python-Magic-Word-Lib Update ---
if version_gt "$MAGIC_REMOTE_VERSION" "$MAGIC_LOCAL_VERSION"; then
  log "Aktualisiere Python-MagicWord-Lib von $MAGIC_LOCAL_VERSION auf $MAGIC_REMOTE_VERSION"
  curl -fsSL "$MAGIC_LIB_URL" -o "$MAGIC_LIB"
  chmod +x "$MAGIC_LIB"
fi

# --- Shell-Skript Update ---
#if version_gt "$SH_REMOTE_VERSION" "$SH_LOCAL_VERSION"; then
#  log "Aktualisiere Shell-Skript von $SH_LOCAL_VERSION auf $SH_REMOTE_VERSION"
#  curl -fsSL "$SH_SCRIPT_URL" -o "$SH_FILE"
#  chmod +x "$SH_FILE"
#fi

# --- Super-Visor-Skript Update ---
if version_gt "$SV_REMOTE_VERSION" "$SV_LOCAL_VERSION"; then
  log "Aktualisiere Super Visor Skript von $SV_LOCAL_VERSION auf $SV_REMOTE_VERSION"
  curl -fsSL "$SV_SCRIPT_URL" -o "$SV_FILE"
  chmod +x "$SV_FILE"
fi


# --- Funktionstest ---
HOSTNAME=$(hostname -s)
DOMAIN=$(dnsdomainname 2>/dev/null)
if [[ -z "$DOMAIN" ]]; then

  # Prüfe, ob HOSTNAME.fritz.box auflösbar ist
  if getent hosts "${HOSTNAME}.fritz.box" > /dev/null; then
    DOMAIN="fritz.box"
  else
    DOMAIN="local"
  fi

fi

CHECK_URL="https://$HOSTNAME.$DOMAIN/webapp/version.html"

log "Prüfe WebApp unter $CHECK_URL"
VERSION_CHECK=$(curl -fsSL "$CHECK_URL" || echo "unreachable")
if [[ "$VERSION_CHECK" == "$WEBAPP_REMOTE_VERSION" ]]; then
  log "WebApp erfolgreich aktualisiert auf Version $VERSION_CHECK"
else
  warn "Version nicht verifiziert oder Seite nicht erreichbar ($VERSION_CHECK)"
fi

      #--arg sh "$SH_REMOTE_VERSION" \
# --- State speichern ---
jq -n --arg wa "$WEBAPP_REMOTE_VERSION" \
      --arg py "$PY_REMOTE_VERSION" \
      --arg inst "$SCRIPT_VERSION" \
      '{webapp: $wa, python: $py, installer: $inst}' > "$STATE_FILE"
      #'{webapp: $wa, python: $py, shell: $sh, installer: $inst}' > "$STATE_FILE"

log "Installations-Skript erfolgreich abgeschlossen."


#Now run the post installer
if version_gt "$PY_REMOTE_VERSION" "$PY_LOCAL_VERSION"; then
  log "🚀 Please launch server component Script post installer .."
  echo "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash"
fi

exit 0


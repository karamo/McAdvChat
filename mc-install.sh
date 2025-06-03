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
SH_SCRIPT_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/mc-screen.sh"
PY_FILE="/usr/local/bin/C2-mc-ws.py"
SH_FILE="/usr/local/bin/mc-screen.sh"
SCRIPT_VERSION="v0.2.0"

MS_LIB="/usr/local/bin/message_storage.py"
MS_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/message_storage.py"
UDP_LIB="/usr/local/bin/udp_handler.py"
UDP_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/udp_handler.py"
WS_LIB="/usr/local/bin/websocket_handler.py"
WS_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/websocket_handler.py"

BLE_LIB="/usr/local/bin/ble_handler.py"
BLE_LIB_URL="https://raw.githubusercontent.com/DK5EN/McAdvChat/main/ble_handler.py"


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

get_local_webapp_version() {
  [[ -f "$INSTALL_DIR/version.txt" ]] && cat "$INSTALL_DIR/version.txt" || echo "v0.0.0"
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
SH_LOCAL_VERSION=$(get_local_version_file "$SH_FILE")
SCRIPT_LOCAL_VERSION=$(get_local_version_file "$0")

log "Lokale WebApp-Version: $WEBAPP_LOCAL_VERSION"
log "Lokale Python-Skript-Version: $PY_LOCAL_VERSION"
log "Lokale Shell-Skript-Version: $SH_LOCAL_VERSION"
log "Install-Skript-Version: $SCRIPT_VERSION"

MS_LOCAL_VERSION=$(get_local_version_file "$MS_LIB")
UDP_LOCAL_VERSION=$(get_local_version_file "$UDP_LIB")
WS_LOCAL_VERSION=$(get_local_version_file "$WS_LIB")
BLE_LOCAL_VERSION=$(get_local_version_file "$BLE_LIB")

log "Lokale Python-MessageStore-Version: $MS_LOCAL_VERSION"
log "Lokale Python-UDP-Version: $UDP_LOCAL_VERSION"
log "Lokale Python-WebSocket-Version: $WS_LOCAL_VERSION"
log "Lokale Python-Bluetooth-Version: $BLE_LOCAL_VERSION"

# --- Remote Versionen ---
WEBAPP_REMOTE_VERSION=$(get_latest_webapp_version)
PY_REMOTE_VERSION=$(get_remote_script_version "$PY_SCRIPT_URL")
SH_REMOTE_VERSION=$(get_remote_script_version "$SH_SCRIPT_URL")

MS_REMOTE_VERSION=$(get_remote_script_version "$MS_LIB_URL")
UDP_REMOTE_VERSION=$(get_remote_script_version "$UDP_LIB_URL")
WS_REMOTE_VERSION=$(get_remote_script_version "$WS_LIB_URL")
BLE_REMOTE_VERSION=$(get_remote_script_version "$BLE_LIB_URL")

log "Remote WebApp-Version: $WEBAPP_REMOTE_VERSION"
log "Remote Python-Skript-Version: $PY_REMOTE_VERSION"
log "Remote Shell-Skript-Version: $SH_REMOTE_VERSION"

log "Remote Python-MessageStore-Version: $MS_REMOTE_VERSION"
log "Remote Python-UDP-Version: $UDP_REMOTE_VERSION"
log "Remote Python-WebSocket-Version: $WS_REMOTE_VERSION"
log "Remote Python-Bluetooth-Version: $BLE_REMOTE_VERSION"

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

# --- Python-WebSocket-Lib Update ---
if version_gt "$BLE_REMOTE_VERSION" "$BLE_LOCAL_VERSION"; then
  log "Aktualisiere Python-Bluetooth-Lib von $BLE_LOCAL_VERSION auf $BLE_REMOTE_VERSION"
  curl -fsSL "$BLE_LIB_URL" -o "$BLE_LIB"
  chmod +x "$BLE_LIB"
fi

# --- Shell-Skript Update ---
if version_gt "$SH_REMOTE_VERSION" "$SH_LOCAL_VERSION"; then
  log "Aktualisiere Shell-Skript von $SH_LOCAL_VERSION auf $SH_REMOTE_VERSION"
  curl -fsSL "$SH_SCRIPT_URL" -o "$SH_FILE"
  chmod +x "$SH_FILE"
fi

# --- Webserver neu starten ---
log "Reloade Webserver ..."
systemctl restart lighttpd || warn "Neustart fehlgeschlagen, versuche Reload"
sleep 2 #give some time to start

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

CHECK_URL="https://$HOSTNAME.$DOMAIN/webapp/version.txt"

log "Prüfe WebApp unter $CHECK_URL"
VERSION_CHECK=$(curl -fsSL "$CHECK_URL" || echo "unreachable")
if [[ "$VERSION_CHECK" == "$WEBAPP_REMOTE_VERSION" ]]; then
  log "WebApp erfolgreich aktualisiert auf Version $VERSION_CHECK"
else
  warn "Version nicht verifiziert oder Seite nicht erreichbar ($VERSION_CHECK)"
fi

# --- State speichern ---
jq -n --arg wa "$WEBAPP_REMOTE_VERSION" \
      --arg py "$PY_REMOTE_VERSION" \
      --arg sh "$SH_REMOTE_VERSION" \
      --arg inst "$SCRIPT_VERSION" \
      '{webapp: $wa, python: $py, shell: $sh, installer: $inst}' > "$STATE_FILE"

log "Installations-Skript erfolgreich abgeschlossen."


#Now run the post installer
if version_gt "$PY_REMOTE_VERSION" "$PY_LOCAL_VERSION"; then
  log "🚀 Please launch server component Script post installer .."
  echo "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash"
fi

exit 0


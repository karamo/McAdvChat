#!/bin/bash
set -euo pipefail

# --- Sudo-Handling ---
#if [[ $EUID -ne 0 ]]; then
#  if sudo -n true 2>/dev/null; then
#    exec sudo "$0" "$@"
#  else
#    echo "🔐 Root-Rechte erforderlich. Bitte Passwort eingeben:"
#    exec sudo -k bash "$0" "$@"
#  fi
#fi

# --- User-Erkennung ---
REAL_USER="${SUDO_USER:-$USER}"
echo "Skript läuft unter Benutzer: $REAL_USER"

# Prüfen, ob echter Benutzer root ist
if [ "$REAL_USER" = "root" ]; then
  echo "❌Fehler: Dieses Skript darf nicht als root ausgeführt werden!"
  exit 1
fi

# Determine the user and home
#USER_NAME=$(whoami)
USER_NAME=$REAL_USER
HOME_DIR=$(eval echo "~$USER_NAME")

VENV_DIR="$HOME_DIR/venv"
PY_SCRIPT="/usr/local/bin/C2-mc-ws.py"
SERVICE_FILE="/etc/systemd/system/mcproxy.service"
CONFIG_DIR="/etc/mcadvchat"
CONFIG_FILE="config.json"

echo "Using user: $USER_NAME"
echo "Home directory: $HOME_DIR"

# 1. Check and create virtualenv
if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "Creating virtual environment in $VENV_DIR takes a minute..."
  python3 -m venv "$VENV_DIR"

  # 2. Activate and install websockets
  echo "🚀Installing 'websockets, dbus and timezone' into virtualenv..."
  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip
  pip install websockets
  pip install dbus_next
  pip install timezonefinder
  pip install zstandard
  pip install requests
else
  echo "Virtual environment already exists."
  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip
  pip install --upgrade websockets
  pip install --upgrade dbus_next
  pip install --upgrade timezonefinder
  pip install --upgrade zstandard
  pip install --upgrade requests
fi

# 3. Check if the Python script exists
if [ ! -f "$PY_SCRIPT" ]; then
  echo "❌ ERROR: Proxy script not found at $PY_SCRIPT"
  exit 1
fi

echo "check, if config directory is there .."
if [ ! -d "$CONFIG_DIR" ]; then
  echo "Creating configuration directory $CONFIG_DIR"
  sudo mkdir $CONFIG_DIR
fi 

echo "check, if config file is there .."
if [ ! -f "$CONFIG_DIR/$CONFIG_FILE" ]; then
  echo "Creating dummy configutation $CONFIG_DIR/$CONFIG_FILE"
  sudo tee "$CONFIG_DIR/$CONFIG_FILE" > /dev/null <<EOF
{
  "UDP_PORT_list": 1799,
  "UDP_PORT_send": 1799,
  "UDP_TARGET": "DK0XXX-99.local",
  "WS_HOST": "127.0.0.1",
  "WS_PORT": 2980,
  "PRUNE_HOURS": 168,
  "MAX_STORAGE_SIZE_MB": 20,
  "STORE_FILE_NAME": "$HOME_DIR/mcdump.json",
  "VERSION": "v0.1.0",
  "CALL_SIGN": "DK0XXX",
  "LAT":48.4031,
  "LONG": 11.7497,
  "STAT_NAME": "Freising",
  "USER_INFO_TEXT": "DK0XX-99 Node | Location: City, Country | Operator: Name | QRV: 70cm/2m/HF | QRZ.COM DK0XX"
}
EOF
  echo "now:    sudo vi $CONFIG_DIR/$CONFIG_FILE"
  echo "and then execute again to finish installation: "
  echo "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash"
  exit 0
fi

REQUIRED_TARGET="DK0XXX-99.local"

if jq -e --arg tgt "$REQUIRED_TARGET" '.UDP_TARGET == $tgt' "$CONFIG_DIR/$CONFIG_FILE" > /dev/null; then
  echo "❌Error: valid parameters missing in $CONFIG_FILE"
  echo "📝 edit your config parameter and come back .."
  echo "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/install_mcproxy.sh | bash"
  exit 1
fi

#update store size and add callsign
# Stage 1: Storage size check
if [[ $(jq -r '.MAX_STORAGE_SIZE_MB' /etc/mcadvchat/config.json) -lt 20 ]]; then
  echo "Checking and updating message store size to min 25MB"
  sudo jq '.MAX_STORAGE_SIZE_MB = 20' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo  mv /tmp/config.tmp /etc/mcadvchat/config.json
fi

# Stage 2: Call sign check
if ! jq -e '.CALL_SIGN' /etc/mcadvchat/config.json > /dev/null 2>&1; then
  echo "Adding a config parameter for your Callsign, to enable remote commands"
  sudo jq '.CALL_SIGN = "DK0XXX"' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo mv /tmp/config.tmp /etc/mcadvchat/config.json
fi

# Stage 3: Location Check
if ! jq -e '.LAT' /etc/mcadvchat/config.json > /dev/null 2>&1; then
  echo "Adding a config parameter for your latitude, to enable weather reports"
  sudo jq '.LAT = 48.4031' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo mv /tmp/config.tmp /etc/mcadvchat/config.json
fi
if ! jq -e '.LONG' /etc/mcadvchat/config.json > /dev/null 2>&1; then
  echo "Adding a config parameter for your longitude, to enable weather reports"
  sudo jq '.LONG = 11.7497' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo mv /tmp/config.tmp /etc/mcadvchat/config.json
fi
if ! jq -e '.STAT_NAME' /etc/mcadvchat/config.json > /dev/null 2>&1; then
  echo "Adding a config parameter for your Station, to enable weather reports"
  sudo jq '.STAT_NAME = "Freising" ' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo mv /tmp/config.tmp /etc/mcadvchat/config.json
fi

if ! jq -e '.USER_INFO_TEXT' /etc/mcadvchat/config.json > /dev/null 2>&1; then
  echo "Adding a config parameter for your Station, to enable weather reports"
  sudo jq '.USER_INFO_TEXT = "DK0XX-99 Node | Location: City, Country | Operator: Name | QRV: 70cm/2m/HF | QRZ.COM DK0XX" ' /etc/mcadvchat/config.json > /tmp/config.tmp && \
  sudo mv /tmp/config.tmp /etc/mcadvchat/config.json
fi

# 4. Check and create systemd service
if [ ! -f "$SERVICE_FILE" ]; then
  echo "Creating systemd service file at $SERVICE_FILE..."
  sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=McAdvances MeshCom Proxy Service
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$HOME_DIR
ExecStart=/bin/bash -c 'source $VENV_DIR/bin/activate && exec python3 $PY_SCRIPT'
Restart=on-failure
RestartSec=3
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
else
  echo "Service file already exists."
fi

# 5. Reload systemd and start the service
echo "Reloading systemd daemon and starting service..."
sudo systemctl daemon-reexec
sudo systemctl daemon-reload
sudo systemctl enable mcproxy.service
sudo systemctl restart mcproxy.service

echo "✅Service 'mcproxy' successfully installed and started."
echo "if not already done, go to your webbrowser, import the root certificate"
echo "then check the settings page"

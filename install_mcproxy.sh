#!/bin/bash
set -euox pipefail

# Determine the user and home
USER_NAME=$(whoami)
HOME_DIR=$(eval echo "~$USER_NAME")
VENV_DIR="$HOME_DIR/venv"
PY_SCRIPT="/usr/local/bin/C2-mc-ws.py"
SERVICE_FILE="/etc/systemd/system/mcproxy.service"

echo "Using user: $USER_NAME"
echo "Home directory: $HOME_DIR"

# 1. Check and create virtualenv
if [ ! -f "$VENV_DIR/bin/activate" ]; then
  echo "Creating virtual environment in $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
else
  echo "Virtual environment already exists."
fi

# 2. Activate and install websockets
echo "🚀Installing 'websockets' into virtualenv..."
source "$VENV_DIR/bin/activate"
pip install -q --upgrade pip
pip install -q websockets

# 3. Check if the Python script exists
if [ ! -f "$PY_SCRIPT" ]; then
  echo "❌ ERROR: Proxy script not found at $PY_SCRIPT"
  exit 1
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

#!/bin/bash

# Konfiguration
DIR=""
SCREEN_NAME="mcproxy"
VENV_ACTIVATE="venv/bin/activate"
PYTHON_SCRIPT="C2-mc-ws.py"
VERSION="v0.2.0"

# Ins Verzeichnis wechseln
cd "$DIR" || { echo "Fehler: Verzeichnis $DIR nicht gefunden."; exit 1; }

# Alte Screen-Session ggf. killen
if screen -list | grep -q "$SCREEN_NAME"; then
    screen -S "$SCREEN_NAME" -X quit
fi

# Neue detached Screen-Session starten
screen -dmS "$SCREEN_NAME" bash -c "source $VENV_ACTIVATE && python3 /usr/local/bin/$PYTHON_SCRIPT"

# Kurze Pause, damit Prozess startet
sleep 1

# Überprüfung, ob Python-Prozess innerhalb Screen läuft
if screen -list | grep -q "$SCREEN_NAME"; then
    PID=$(screen -S "$SCREEN_NAME" -Q select . >/dev/null 2>&1; pgrep -f "$PYTHON_SCRIPT")
    if [ -n "$PID" ]; then
        echo "✅ Proxy erfolgreich gestartet ($PID Screen: $SCREEN_NAME)"
        echo "Um den Inhalt der Screen Session aufzurufen: screen -r"
        echo "In der Screen Session: CRTL + a und d - um Screen Session zu verlassen"
        echo "Um zu scrollen: CTRL + a und ["
        echo "dann mit Tasten j und k scrollen. Mit q den scroll modus verlassen"
        exit 0
    else
        echo "⚠️  Fehler: Screen läuft, aber kein Python-Prozess gefunden."
        exit 2
    fi
else
    echo "❌ Fehler: Screen $SCREEN_NAME konnte nicht gestartet werden."
    exit 3
fi

#!/bin/bash
set -euo pipefail

#supress warnings "setlocale: LC_ALL: cannot change locale (de_DE.UTF-8)"
export LC_ALL=C
export LANG=C

echo "🔧 Starting Caddy + Lighttpd installer..."

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
echo "Skript läuft unter Benutzer: $REAL_USER"

# Prüfen, ob echter Benutzer root ist
if [ "$REAL_USER" = "root" ]; then
  echo "❌Fehler: Dieses Skript darf nicht als root ausgeführt werden!"
  exit 1
fi


# 1. Check architecture
ARCH=$(uname -m)
echo "📦 Detected architecture: $ARCH"
if [[ "$ARCH" != "aarch64" ]]; then
  echo "❌ Unsupported architecture: $ARCH (expected aarch64)"
  exit 1
fi


if locale -a | grep -q '^de_DE\.utf8$'; then
  echo "Locale de_DE.UTF-8 is already generated."
else

  echo ">>> Installing required locales..."
  LOCALE_TO_USE="de_DE.UTF-8 UTF-8"
  LANG_KEY="de_DE.UTF-8"

  # Add desired locale if not already in /etc/locale.gen
  if ! grep -q "^$LOCALE_TO_USE" /etc/locale.gen; then
    echo "$LOCALE_TO_USE" | sudo tee -a /etc/locale.gen
  fi

  # remove en_GB.UTF-8 to avoid fallback conflicts
  sudo sed -i 's/^en_GB.UTF-8 UTF-8/# en_GB.UTF-8 UTF-8/' /etc/locale.gen

  # Generate only the selected locales
  sudo locale-gen

  # Write environment config
  echo ">>> Setting system-wide locale to $LANG_KEY"
  sudo bash -c "cat > /etc/default/locale" <<EOF
LANG=$LANG_KEY
LC_ALL=$LANG_KEY
EOF

  # Optional: also update current session
  export LANG=$LANG_KEY
  export LC_ALL=$LANG_KEY

  echo ">>> Locale setup complete "
  #echo ">>> Locale setup complete. Current locale:"
  #locale
fi

# 2. Update APT and install required packages
echo "📦 Updating package lists..."
sudo apt update

echo "📥 Installing base dependencies..."
sudo apt install -y debian-keyring debian-archive-keyring curl apt-transport-https jq

# 3. Add Caddy GPG key if missing
CADDY_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
if [ ! -f "$CADDY_KEYRING" ]; then
  echo "🔐 Adding Caddy GPG key..."
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | \
    sudo gpg --dearmor -o "$CADDY_KEYRING"
else
  echo "🔐 Caddy GPG key already exists."
fi


# 4. Add Caddy repo if missing
CADDY_LIST="/etc/apt/sources.list.d/caddy-stable.list"
if [ ! -f "$CADDY_LIST" ]; then
  echo "🧾 Adding Caddy APT repo..."
  echo "deb [signed-by=$CADDY_KEYRING] https://dl.cloudsmith.io/public/caddy/stable/deb/debian bookworm main" | \
    sudo tee "$CADDY_LIST" > /dev/null
else
  echo "🧾 Caddy APT repo already defined."
fi

# 5. Install caddy, lighttpd, screen
echo "📦 Updating APT again..."
sudo apt update

echo "📥 Installing caddy, lighttpd, screen..."
sudo apt install -y caddy lighttpd screen

# 6. Verify Caddy installation
echo "🧪 Verifying caddy installation..."
if ! CADDY_VERSION=$(caddy version); then
  echo "❌ Caddy failed to install or is not available on PATH."
  exit 1
else
  echo "✅ Caddy version: $CADDY_VERSION"
fi


# 7. Determine hostname and domain
HOSTNAME=$(hostname -s)
DOMAIN=$(dnsdomainname 2>/dev/null)

if [[ -z "$DOMAIN" ]]; then
  # Prüfe, ob HOSTNAME.fritz.box auflösbar ist
  if getent hosts "${HOSTNAME}.fritz.box" > /dev/null; then
    DOMAIN="fritz.box"
  else
    DOMAIN="local" 
  fi

  FQDN="$HOSTNAME.$DOMAIN"
  DOMAIN_STYLE="(fallback: $DOMAIN)"
else
  FQDN="$HOSTNAME.$DOMAIN"
  DOMAIN_STYLE="(detected)"
fi

echo "🌐 Hostname: $HOSTNAME"
echo "🌐 Domain: $DOMAIN $DOMAIN_STYLE"
echo "🌐 Full address: $FQDN"

# 8. Write Caddyfile
CADDYFILE="/etc/caddy/Caddyfile"
echo "📝 Writing Caddyfile to $CADDYFILE..."

sudo tee "$CADDYFILE" > /dev/null <<EOF
{
        auto_https disable_redirects
        log {
                #level DEBUG
                level INFO
                format console
        }
}

:443 {
        tls {
                protocols tls1.3
        }
        respond "Hello with ChaCha?"
}

$FQDN {
        tls internal
        reverse_proxy 127.0.0.1:80
        encode gzip
}

$FQDN:2981 {
        tls internal
        reverse_proxy 127.0.0.1:2980
}
EOF

echo "✅ Caddyfile written successfully."

# 9. Restart Caddy
echo "🔁 Restarting Caddy..."
sudo systemctl restart caddy

echo "🚀 enabling Caddy for startup..."
sudo systemctl enable --now caddy

CADDY_ROOT_CRT="root.crt"
CADDY_ATHORITY="/var/lib/caddy/.local/share/caddy/pki/authorities/local"
DST_DIR="/var/www/html"

echo "🔐 provisioning root.crt in html root folder ..."
if [ ! -f "$DST_DIR/$CADDY_ROOT_CRT" ]; then
  sudo cp $CADDY_ATHORITY/$CADDY_ROOT_CRT  $DST_DIR
  sudo chmod a+r $DST_DIR/$CADDY_ROOT_CRT
fi

echo "🔐 checking if root.crt in local cert store folder ..."
if [ ! -f "/etc/ssl/certs/Caddy_10y_Root.crt" ]; then
  echo "🚀installing caddy root certificate in local certificate store"
  sudo ln -s /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt /etc/ssl/certs/Caddy_10y_Root.crt
  echo "🔁rehashing root certificates (30 seconds) .."
  sudo c_rehash /etc/ssl/certs
  sudo update-ca-certificates
fi 

lighttpd_conf="/etc/lighttpd/lighttpd.conf"
rewrite_marker='/webapp/index.html'

echo "🔍 Checking if $rewrite_marker rewrite rule is present in $lighttpd_conf..."

if grep -q "$rewrite_marker" "$lighttpd_conf"; then
  echo "✅ Rewrite rule for /webapp/ already present. Skipping patch."
else
  echo "enabling rewrite rule for lighttpd"
  sudo lighty-enable-mod rewrite
  echo "🛠️ Patching $lighttpd_conf with /webapp/ rewrite rule..."

  sudo tee -a "$lighttpd_conf" > /dev/null <<EOF

# McAdvChat Patch: SPA rewrite for /webapp/
\$HTTP["url"] =~ "^/webapp/" {
  url.rewrite-if-not-file = (
    "^/webapp/(.*)" => "/webapp/index.html"
  )
}
  echo "✅ Patch applied to $lighttpd_conf"
EOF
fi

marker='prevent caching of version.txt'

echo "🔍 Checking if $marker rewrite rule is present in $lighttpd_conf..."

if grep -q "$marker" "$lighttpd_conf"; then
  echo "✅ Rule for already present. Skipping patch."
else
  echo "enabling no cache rule for version.txt for lighttpd"
  echo "🛠️ Patching $lighttpd_conf with rule..."

  sudo tee -a "$lighttpd_conf" > /dev/null <<EOF

#prevent caching of version.txt
\$HTTP["url"] =~ "^/version\.txt$" {
    set-response-header = (
        "Cache-Control" => "no-store, no-cache, must-revalidate, proxy-revalidate",
        "Pragma" => "no-cache",
        "Expires" => "0"
    )
}
EOF

  echo "✅ Patch applied to $lighttpd_conf"
fi

echo "🔁 Restarting lighttpd..."
sudo systemctl restart lighttpd

echo "🎉 Base Install of System Components complete."
echo "now execute:"
echo "curl -fsSL https://raw.githubusercontent.com/DK5EN/McAdvChat/main/mc-install.sh | sudo bash"

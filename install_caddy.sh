
#!/bin/bash
set -euo pipefail

echo "🔧 Starting Caddy + Lighttpd installer..."

# 1. Check architecture
ARCH=$(uname -m)
echo "📦 Detected architecture: $ARCH"
if [[ "$ARCH" != "aarch64" ]]; then
  echo "❌ Unsupported architecture: $ARCH (expected aarch64)"
  exit 1
fi

# 2. Update APT and install required packages
echo "📦 Updating package lists..."
sudo apt update

echo "📥 Installing base dependencies..."
sudo apt install -y debian-keyring debian-archive-keyring curl apt-transport-https

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
  echo "deb [signed-by=$CADDY_KEYRING] https://dl.cloudsmith.io/public/caddy/stable/deb/debian all main" | \
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


lighttps="/etc/ligttpd/lighttpd.conf"
rewrite_marker='^/webapp/'

echo "🔍 Checking if $rewrite_marker rewrite rule is present in $lighttpd_conf..."

if grep -q "$rewrite_marker" "$lighttpd_conf"; then
  echo "✅ Rewrite rule for /webapp/ already present. Skipping patch."
else
  echo "🛠️ Patching $lighttpd_conf with /webapp/ rewrite rule..."

  sudo tee -a "$lighttpd_conf" > /dev/null <<EOF

# McAdvChat Patch: SPA rewrite for /webapp/
\$HTTP["url"] =~ "^/webapp/" {
  url.rewrite-if-not-file = (
    "^/webapp/(.*)" => "/webapp/index.html"
  )
}
EOF

  echo "✅ Patch applied to $lighttpd_conf"
fi


echo "🎉 Installation complete."

#!/usr/bin/env bash
# Setup script for Kamatera magnet-to-b2 server (Ubuntu/Debian - apt)
set -euo pipefail

APP_DIR=/opt/magnet-b2

echo "=== [1/5] Installing system packages ==="
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y git python3.12 python3.12-venv python3-pip curl
PY=python3.12
command -v "$PY" >/dev/null 2>&1 || PY=python3
echo "Using python: $($PY --version 2>&1) ($PY)"

echo "=== [2/5] Creating app dir and downloading code ==="
mkdir -p "$APP_DIR"
curl -fsSL https://raw.githubusercontent.com/a05456/magnet-to-b2/main/app.py -o "$APP_DIR/app.py"
curl -fsSL https://raw.githubusercontent.com/a05456/magnet-to-b2/main/requirements.txt -o "$APP_DIR/requirements.txt"

echo "=== [3/5] Python venv + dependencies ==="
"$PY" -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -U pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== [4/5] Environment file ==="
if [ ! -f /etc/magnet-b2.env ]; then
  cat > /etc/magnet-b2.env <<'EOF'
B2_APPLICATION_KEY_ID=PUT_KEY_ID_HERE
B2_APPLICATION_KEY=PUT_APP_KEY_HERE
B2_BUCKET=PUT_BUCKET_NAME_HERE
PORT=80
EOF
  chmod 600 /etc/magnet-b2.env
  echo "Created /etc/magnet-b2.env - EDIT IT with real B2 credentials before starting!"
fi

echo "=== [5/5] systemd service ==="
cat > /etc/systemd/system/magnet-b2.service <<EOF
[Unit]
Description=Magnet to B2 (Gradio)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/magnet-b2.env
ExecStart=$APP_DIR/venv/bin/python app.py
Restart=always
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable magnet-b2.service

echo ""
echo "=============================================="
echo "Setup complete. Next steps:"
echo "  1. Edit /etc/magnet-b2.env with real B2 credentials"
echo "  2. systemctl start magnet-b2"
echo "  3. Check: curl http://localhost/health"
echo "=============================================="

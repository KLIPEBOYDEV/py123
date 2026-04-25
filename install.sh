#!/usr/bin/env bash
set -euo pipefail

# Ubuntu auto-install script for XLab Dance studio
# Usage (run on server):
#   sudo bash install.sh
# Optional env vars:
#   APP_DIR=/opt/py1 SERVICE_NAME=xlab-dance PORT=8050 APP_USER=ubuntu sudo bash install.sh

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-xlab-dance}"
PORT="${PORT:-8050}"
APP_USER="${APP_USER:-${SUDO_USER:-$USER}}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Installing system packages..."
apt-get update -y
apt-get install -y python3 python3-venv python3-pip

echo "==> Preparing app directory: $APP_DIR"
mkdir -p "$APP_DIR"
chown -R "$APP_USER":"$APP_USER" "$APP_DIR"

echo "==> Creating virtual environment..."
sudo -u "$APP_USER" "$PYTHON_BIN" -m venv "$VENV_DIR"

echo "==> Installing Python dependencies..."
sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install --upgrade pip
if [[ -f "$APP_DIR/requirements.txt" ]]; then
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
else
  sudo -u "$APP_USER" "$VENV_DIR/bin/pip" install flask gunicorn
fi

echo "==> Initializing database..."
sudo -u "$APP_USER" bash -lc "cd \"$APP_DIR\" && \"$VENV_DIR/bin/python\" -c 'from database import init_db; init_db()'"

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
echo "==> Creating systemd service: $SERVICE_FILE"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=XLab Dance studio service
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/gunicorn -w 2 -b 0.0.0.0:$PORT app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

echo "==> Enabling and starting service..."
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "Done. Service status:"
systemctl --no-pager --full status "$SERVICE_NAME" || true
echo
echo "Useful commands:"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "  sudo systemctl restart $SERVICE_NAME"

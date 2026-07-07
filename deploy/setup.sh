#!/bin/bash
# deploy/setup.sh — First-time server setup. Run ONCE on a fresh Ubuntu 24.04.
# Run as: bash /home/admin/deploy/setup.sh
#
# What this does:
#   1. Installs system packages (Python 3.12, nginx, etc.)
#   2. Creates /opt/optionlab with correct ownership
#   3. Installs the systemd service for optionlab
#   4. Configures nginx for both honerfit (2108) and optionlab (5001)
#   5. Enables and starts nginx

set -euo pipefail

# Re-exec with sudo if not already root
if [ "$EUID" -ne 0 ]; then
    echo "==> Re-running with sudo ..."
    exec sudo bash "$0" "$@"
fi

APP_DIR="/opt/optionlab"
APP_PORT=5001
HONERFIT_PORT=2108
NGINX_OPTIONLAB_PORT=8001
NGINX_HONERFIT_PORT=80
SERVICE_USER="admin"
SERVICE_FILE="/etc/systemd/system/optionlab.service"

echo "======================================================="
echo " OptionLab — First-time Ubuntu 24.04 Setup"
echo "======================================================="

# ── 1. System packages ─────────────────────────────────────────────────────────
echo ""
echo "==> Installing system packages ..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3.12 python3.12-venv python3.12-dev \
    python3-pip \
    nginx \
    curl \
    tar \
    build-essential \
    libssl-dev

# ── 2. App directory ───────────────────────────────────────────────────────────
echo ""
echo "==> Creating app directory: $APP_DIR ..."
sudo mkdir -p "$APP_DIR/data/models"
sudo mkdir -p "$APP_DIR/data/paper_trades"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ── 3. Systemd service ─────────────────────────────────────────────────────────
echo ""
echo "==> Installing systemd service ..."
sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=OptionLab Options Trading Platform
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn \\
    --bind 127.0.0.1:$APP_PORT \\
    --workers 2 \\
    --threads 4 \\
    --timeout 120 \\
    --access-logfile $APP_DIR/data/access.log \\
    --error-logfile $APP_DIR/data/error.log \\
    web.app:app
Restart=on-failure
RestartSec=5
StandardOutput=append:$APP_DIR/data/optionlab.log
StandardError=append:$APP_DIR/data/optionlab.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable optionlab
echo "  Service installed: optionlab.service"

# ── 4. Nginx config ────────────────────────────────────────────────────────────
echo ""
echo "==> Configuring nginx ..."

# OptionLab virtual host
sudo tee /etc/nginx/sites-available/optionlab > /dev/null <<EOF
server {
    listen $NGINX_OPTIONLAB_PORT;
    server_name _;

    client_max_body_size 10M;

    location / {
        proxy_pass         http://127.0.0.1:$APP_PORT;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }

    # SSE (Live Suggestions stream) — disable buffering
    location /stream {
        proxy_pass         http://127.0.0.1:$APP_PORT;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 600s;
        proxy_set_header   Connection '';
        chunked_transfer_encoding on;
    }
}
EOF

# Honerfit virtual host
sudo tee /etc/nginx/sites-available/honerfit > /dev/null <<EOF
server {
    listen $NGINX_HONERFIT_PORT;
    server_name _;

    location / {
        proxy_pass         http://127.0.0.1:$HONERFIT_PORT;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
EOF

# Enable sites
sudo ln -sf /etc/nginx/sites-available/optionlab  /etc/nginx/sites-enabled/optionlab
sudo ln -sf /etc/nginx/sites-available/honerfit   /etc/nginx/sites-enabled/honerfit

# Disable default site if it conflicts on port 80
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
sudo systemctl enable nginx
sudo systemctl restart nginx

echo "  nginx configured:"
echo "    http://192.168.1.199:$NGINX_HONERFIT_PORT  → honerfit  (port $HONERFIT_PORT)"
echo "    http://192.168.1.199:$NGINX_OPTIONLAB_PORT → optionlab (port $APP_PORT)"

# ── 5. Done ────────────────────────────────────────────────────────────────────
echo ""
echo "======================================================="
echo " Setup complete."
echo "======================================================="
echo ""
echo "Next steps:"
echo "  1. Transfer code:    run .\deploy\bundle.ps1 on Windows"
echo "  2. Transfer data:    run .\deploy\transfer_data.ps1 on Windows"
echo "  3. Deploy:           bash /home/admin/deploy/deploy.sh"
echo ""
echo "After deploy, create .env if not transferred:"
echo "  nano $APP_DIR/.env"

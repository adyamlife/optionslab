#!/bin/bash
# deploy/deploy.sh — Deploy or update OptionLab from a bundle.
# Run on the Ubuntu server after bundle.ps1 has copied the tarball.
# Safe to run for both first deploy and future updates.
#
# Usage:
#   bash /home/admin/deploy/deploy.sh
#   bash /home/admin/deploy/deploy.sh -f /path/to/custom_bundle.tar.gz

set -euo pipefail

# Re-exec with sudo if not already root
if [ "$EUID" -ne 0 ]; then
    echo "==> Re-running with sudo ..."
    exec sudo bash "$0" "$@"
fi

BUNDLE_DEFAULT="/home/admin/optionlab_bundle.tar.gz"
APP_DIR="/opt/optionlab"
SERVICE_USER="admin"
PYTHON="python3.12"

# Parse flags
BUNDLE="$BUNDLE_DEFAULT"
while getopts "f:" opt; do
    case $opt in
        f) BUNDLE="$OPTARG" ;;
        *) echo "Usage: $0 [-f <bundle.tar.gz>]"; exit 1 ;;
    esac
done

if [ ! -f "$BUNDLE" ]; then
    echo "ERROR: Bundle not found at $BUNDLE"
    echo "  Run .\deploy\bundle.ps1 on Windows first."
    exit 1
fi

echo "======================================================="
echo " OptionLab Deploy"
echo " Bundle : $BUNDLE"
echo " Target : $APP_DIR"
echo "======================================================="

# ── Stop service ───────────────────────────────────────────────────────────────
echo ""
echo "==> Stopping optionlab service ..."
sudo systemctl stop optionlab || true

# Ensure service file has correct worker count and reload if needed
SERVICE_FILE="/etc/systemd/system/optionlab.service"
if [ -f "$SERVICE_FILE" ] && grep -q "\-\-workers 2" "$SERVICE_FILE"; then
    echo "==> Fixing worker count in service file (2 -> 1) ..."
    sed -i 's/--workers 2/--workers 1/' "$SERVICE_FILE"
    systemctl daemon-reload
fi

# ── Extract bundle ─────────────────────────────────────────────────────────────
echo "==> Extracting bundle ..."
sudo tar -xzf "$BUNDLE" -C "$APP_DIR" \
    --exclude='data' \
    --exclude='.env' \
    --exclude='venv'

sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# ── Python virtual environment ─────────────────────────────────────────────────
if [ ! -d "$APP_DIR/venv" ]; then
    echo "==> Creating Python virtual environment ..."
    $PYTHON -m venv "$APP_DIR/venv"
fi

echo "==> Installing/updating Python dependencies ..."
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# ── Ensure data directories exist ─────────────────────────────────────────────
mkdir -p "$APP_DIR/data/models"
mkdir -p "$APP_DIR/data/paper_trades"

# ── Validate .env exists ───────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo "WARNING: $APP_DIR/.env not found."
    echo "  Run .\deploy\transfer_data.ps1 on Windows, or create it manually:"
    echo "  nano $APP_DIR/.env"
    echo ""
fi

# ── Start service ──────────────────────────────────────────────────────────────
echo "==> Starting optionlab service ..."
sudo systemctl daemon-reload
sudo systemctl start optionlab
sleep 3
sudo systemctl status optionlab --no-pager -l

# ── Cleanup bundle ─────────────────────────────────────────────────────────────
rm -f "$BUNDLE"
echo ""
echo "======================================================="
echo " Deploy complete."
echo " App : http://192.168.1.199:8001"
echo " Log : tail -f $APP_DIR/data/optionlab.log"
echo " Svc : sudo systemctl status optionlab"
echo "======================================================="

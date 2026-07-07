#!/bin/bash
# deploy_optionlab.sh — Run on Ubuntu server to extract bundle and restart optionlab
# Usage: bash deploy_optionlab.sh [-f <bundle_file>]
#   -f  Path to zip bundle (default: ~/deploy_bundle.zip)

set -e

BUNDLE_NAME="deploy_bundle.zip"
DEPLOY_DIR="/opt/optionlab"
BUNDLE="$HOME/$BUNDLE_NAME"
DEPLOY_USER="${SUDO_USER:-$USER}"

# Parse flags
while getopts "f:" opt; do
  case $opt in
    f) BUNDLE="$OPTARG" ;;
    *) echo "Usage: $0 [-f <bundle_file>]"; exit 1 ;;
  esac
done

if [ ! -f "$BUNDLE" ]; then
  echo "Error: bundle not found at $BUNDLE"
  exit 1
fi

echo "==> Using bundle: $BUNDLE"
echo "==> Stopping service..."
sudo systemctl stop optionlab || true

echo "==> Extracting bundle to $DEPLOY_DIR..."
sudo unzip -o "$BUNDLE" -d "$DEPLOY_DIR"

echo "==> Fixing ownership..."
sudo chown -R "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_DIR"

echo "==> Restarting service..."
sudo systemctl start optionlab
sleep 2
sudo systemctl status optionlab --no-pager

echo ""
echo "Deploy complete. App running at http://$(curl -s ifconfig.me):8002"

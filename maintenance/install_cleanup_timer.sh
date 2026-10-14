#!/bin/bash
# maintenance/install_cleanup_timer.sh
# Installs the monthly storage cleanup script and systemd timer on Gaia.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing cleanup script to /usr/local/bin/gaia-cleanup.sh..."
install -m 0755 "${SCRIPT_DIR}/cleanup_storage.sh" /usr/local/bin/gaia-cleanup.sh

echo "Installing systemd service and timer units..."
install -m 0644 "${SCRIPT_DIR}/systemd/gaia-cleanup.service" /etc/systemd/system/gaia-cleanup.service
install -m 0644 "${SCRIPT_DIR}/systemd/gaia-cleanup.timer" /etc/systemd/system/gaia-cleanup.timer

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting gaia-cleanup.timer..."
systemctl enable --now gaia-cleanup.timer

echo "Verifying timer status:"
systemctl list-timers --all | grep -E "gaia-cleanup|NEXT" || true

echo "Success! Gaia monthly storage cleanup timer is installed and active."

#!/bin/bash
# maintenance/install_cleanup_timer.sh
# Installs the monthly storage cleanup script and systemd timer on Gaia.
# Supports running both locally from a cloned repo or directly piped via curl.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
RAW_BASE="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main"

echo "Installing cleanup script to /usr/local/bin/gaia-cleanup.sh..."
if [ -f "${SCRIPT_DIR}/cleanup_storage.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/cleanup_storage.sh" /usr/local/bin/gaia-cleanup.sh
else
    curl -fsSL "${RAW_BASE}/maintenance/cleanup_storage.sh" -o /usr/local/bin/gaia-cleanup.sh
    chmod 0755 /usr/local/bin/gaia-cleanup.sh
fi

echo "Installing systemd service and timer units..."
if [ -f "${SCRIPT_DIR}/systemd/gaia-cleanup.service" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/gaia-cleanup.service" /etc/systemd/system/gaia-cleanup.service
    install -m 0644 "${SCRIPT_DIR}/systemd/gaia-cleanup.timer" /etc/systemd/system/gaia-cleanup.timer
else
    curl -fsSL "${RAW_BASE}/maintenance/systemd/gaia-cleanup.service" -o /etc/systemd/system/gaia-cleanup.service
    curl -fsSL "${RAW_BASE}/maintenance/systemd/gaia-cleanup.timer" -o /etc/systemd/system/gaia-cleanup.timer
    chmod 0644 /etc/systemd/system/gaia-cleanup.service /etc/systemd/system/gaia-cleanup.timer
fi

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting gaia-cleanup.timer..."
systemctl enable --now gaia-cleanup.timer

echo "Verifying timer status:"
systemctl list-timers --all | grep -E "gaia-cleanup|NEXT" || true

echo "Success! Gaia monthly storage cleanup timer is installed and active."

#!/bin/bash
# maintenance/install_cleanup_timer.sh
# Installs the Gaia cleanup script plus monthly and daily systemd timers.
# Supports running both locally from a cloned repo or directly piped via curl.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
fi
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main}"
BIN_PATH="${GAIA_CLEANUP_BIN_PATH:-/usr/local/bin/gaia-cleanup.sh}"
SYSTEMD_DIR="${GAIA_SYSTEMD_DIR:-/etc/systemd/system}"

install_asset() {
    local rel_path="$1"
    local dest_path="$2"
    local mode="$3"

    mkdir -p "$(dirname "$dest_path")"
    if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/${rel_path}" ]; then
        install -m "$mode" "${SCRIPT_DIR}/${rel_path}" "$dest_path"
    else
        curl -fsSL "${RAW_BASE}/maintenance/${rel_path}" -o "$dest_path"
        chmod "$mode" "$dest_path"
    fi
}

echo "Installing cleanup script to ${BIN_PATH}..."
install_asset "cleanup_storage.sh" "$BIN_PATH" "0755"

echo "Installing systemd service and timer units..."
UNITS=(
    "gaia-cleanup.service"
    "gaia-cleanup.timer"
    "gaia-container-prune.service"
    "gaia-container-prune.timer"
)
for unit in "${UNITS[@]}"; do
    install_asset "systemd/${unit}" "${SYSTEMD_DIR}/${unit}" "0644"
done

echo "Reloading systemd daemon..."
systemctl daemon-reload

TIMERS=(
    "gaia-cleanup.timer"
    "gaia-container-prune.timer"
)
for timer in "${TIMERS[@]}"; do
    echo "Enabling and starting ${timer}..."
    systemctl enable --now "$timer"
done

echo "Verifying timer status:"
systemctl list-timers --all | grep -E "gaia-cleanup|gaia-container-prune|NEXT" || true

echo "Success! Gaia monthly storage cleanup and daily container prune timers are installed and active."

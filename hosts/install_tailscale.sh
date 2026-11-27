#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Yggdrasil OS - Host-Native Tailscale & Ethtool Installer
# ==============================================================================
# Installs the official Tailscale APT package and ethtool on Debian/Ubuntu
# bare-metal hosts. Fails loudly on non-root execution or missing OS release
# metadata. Leaves node authentication for operator bootstrap.
# ==============================================================================

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: install_tailscale.sh must be run as root (sudo)." >&2
    exit 1
fi

if command -v tailscale >/dev/null 2>&1 && command -v ethtool >/dev/null 2>&1; then
    echo "Tailscale and ethtool are already installed."
    exit 0
fi

OS_RELEASE_FILE="${OS_RELEASE_PATH:-/etc/os-release}"
if [ ! -r "$OS_RELEASE_FILE" ]; then
    echo "Error: Cannot read OS release file: ${OS_RELEASE_FILE}" >&2
    exit 1
fi

DISTRO_ID="$(awk -F= '$1=="ID"{gsub(/^["'\'']|["'\'']$/, "", $2); print $2; exit}' "$OS_RELEASE_FILE")"
CODENAME="$(awk -F= '$1=="VERSION_CODENAME"{gsub(/^["'\'']|["'\'']$/, "", $2); print $2; exit}' "$OS_RELEASE_FILE")"

if [ -z "$DISTRO_ID" ] || [ -z "$CODENAME" ]; then
    echo "Error: Missing ID or VERSION_CODENAME in ${OS_RELEASE_FILE}" >&2
    exit 1
fi

if ! command -v tailscale >/dev/null 2>&1; then
    KEYRING_PATH="/usr/share/keyrings/tailscale-archive-keyring.gpg"
    REPO_LIST_PATH="/etc/apt/sources.list.d/tailscale.list"
    BASE_URL="https://pkgs.tailscale.com/stable/${DISTRO_ID}/${CODENAME}"

    install -d -m 0755 /usr/share/keyrings /etc/apt/sources.list.d
    curl -fsSL "${BASE_URL}.noarmor.gpg" -o "${KEYRING_PATH}"
    chmod 0644 "${KEYRING_PATH}"
    curl -fsSL "${BASE_URL}.tailscale-keyring.list" -o "${REPO_LIST_PATH}"
    chmod 0644 "${REPO_LIST_PATH}"
fi

apt-get update -y
apt-get install -y --no-install-recommends tailscale ethtool

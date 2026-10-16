#!/usr/bin/env bash
set -e

# ==============================================================================
# Yggdrasil OS — Host Prerequisites & Tooling Installer
# ==============================================================================
# Installs essential CLI tools (GitHub CLI, jq, curl, wget, ca-certificates)
# on Debian/Ubuntu bare-metal hosts.
# Idempotent: safe to run multiple times.
#
# Usage (Zero-Clone):
#   curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/install_prerequisites.sh | sudo bash
# ==============================================================================

TARGET_USER="${SUDO_USER:-$(id -un)}"
TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
TARGET_HOME="${TARGET_HOME:-/home/$TARGET_USER}"

echo "=== [1/3] Installing Base CLI Utilities ==="
if [ "$(id -u)" -eq 0 ]; then
    apt-get update -y
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        gnupg \
        jq \
        git \
        tar \
        gzip
    echo "  ✓ Base packages installed via apt"
else
    echo "  ! Running without root; skipping apt-get (ensure curl, tar, jq are installed)"
fi

echo "=== [2/3] Installing Official GitHub CLI (gh) ==="
if command -v gh >/dev/null 2>&1; then
    CURRENT_GH=$(gh --version | head -n 1)
    echo "  ✓ GitHub CLI already installed: ${CURRENT_GH}"
else
    if [ "$(id -u)" -eq 0 ]; then
        # Install official GitHub apt keyring
        mkdir -p -m 755 /etc/apt/keyrings
        wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
        chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
        apt-get update -y
        apt-get install -y gh
        echo "  ✓ gh installed system-wide via official apt repo"
    else
        # Standalone binary fallback to user's ~/.local/bin
        mkdir -p "${TARGET_HOME}/.local/bin"
        TMP_DIR=$(mktemp -d)
        TARBALL_URL=$(curl -s https://api.github.com/repos/cli/cli/releases/latest | grep "browser_download_url.*linux_amd64.tar.gz\"" | head -n 1 | cut -d '"' -f 4)
        TARBALL_URL="${TARBALL_URL:-https://github.com/cli/cli/releases/download/v2.60.1/gh_2.60.1_linux_amd64.tar.gz}"
        curl -fsSL "$TARBALL_URL" -o "${TMP_DIR}/gh.tar.gz"
        tar -xzf "${TMP_DIR}/gh.tar.gz" -C "${TMP_DIR}"
        GH_BIN=$(find "${TMP_DIR}" -type f -name "gh" -perm -111 | head -n 1)
        cp -f "$GH_BIN" "${TARGET_HOME}/.local/bin/gh"
        chmod +x "${TARGET_HOME}/.local/bin/gh"
        rm -rf "${TMP_DIR}"
        echo "  ✓ gh installed to ${TARGET_HOME}/.local/bin/gh"
    fi
fi

echo "=== [3/3] Setting Up GitHub CLI Authentication ==="
USER_GH_CONFIG="${TARGET_HOME}/.config/gh/hosts.yml"
if [ -f "${USER_GH_CONFIG}" ]; then
    echo "  ✓ Existing gh authentication found at ${USER_GH_CONFIG}"
elif [ -f "/opt/odin/hermes/home/.config/gh/hosts.yml" ]; then
    mkdir -p "${TARGET_HOME}/.config/gh"
    cp /opt/odin/hermes/home/.config/gh/hosts.yml "${USER_GH_CONFIG}"
    chmod 600 "${USER_GH_CONFIG}"
    if [ "$(id -u)" -eq 0 ]; then
        chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}/.config/gh"
    fi
    echo "  ✓ Copied existing cluster GitHub credentials to ${USER_GH_CONFIG}"
elif [ -n "${GITHUB_TOKEN:-}" ]; then
    mkdir -p "${TARGET_HOME}/.config/gh"
    echo "${GITHUB_TOKEN}" | gh auth login --with-token
    echo "  ✓ Authenticated gh using GITHUB_TOKEN"
else
    echo "  ! No GitHub token detected. Run 'gh auth login' to authenticate host CLI."
fi

echo ""
echo "=============================================================================="
echo "✓ Host Prerequisites Installation Complete!"
echo "=============================================================================="

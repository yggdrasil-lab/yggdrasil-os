#!/bin/bash
set -e

# ==============================================================================
# Yggdrasil OS — Gaia Host Setup & Directory Provisioning
# ==============================================================================
# Prepares host paths, permissions, and directories on the Gaia Manager node.
# Idempotent: safe to run multiple times.
#
# Usage (Zero-Clone):
#   curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/setup_gaia.sh | sudo bash
# ==============================================================================

echo "=== [1/9] Setting up Olympus (Ingress & DNS) ==="
mkdir -p /opt/olympus/adguard/conf /opt/olympus/adguard/work
chown -R 1000:1000 /opt/olympus/adguard
echo "  ✓ Olympus directories ready"

echo "=== [2/9] Setting up Odin (Hermes & AI Lab) ==="
mkdir -p /opt/odin/hermes/home /opt/odin/mnemosyne /mnt/storage/backups/odin/hermes
chown -R 1000:1000 /opt/odin /mnt/storage/backups/odin/hermes
chmod -R 775 /opt/odin/hermes /opt/odin/mnemosyne

# Ensure agy CLI symlink exists if installed in user directory
TARGET_USER=${SUDO_USER:-$(id -un)}
if [ -f "/home/${TARGET_USER}/.local/bin/agy" ]; then
    ln -sf "/home/${TARGET_USER}/.local/bin/agy" /usr/local/bin/agy
fi
echo "  ✓ Odin directories ready"

echo "=== [3/9] Setting up Apollo Core (Manager Services) ==="
APOLLO_GAIA_SERVICES=("jellystat" "jellystat-db" "prowlarr" "seerr" "recyclarr")
for svc in "${APOLLO_GAIA_SERVICES[@]}"; do
    mkdir -p "/opt/apollo-core/${svc}"
    chown -R 1000:1000 "/opt/apollo-core/${svc}"
done
mkdir -p /mnt/storage/backups/apollo/prowlarr
chown -R 1000:1000 /mnt/storage/backups/apollo/prowlarr
echo "  ✓ Apollo Core (Gaia) directories ready"

echo "=== [4/9] Setting up Cerberus (Auth & Identity) ==="
CERBERUS_SERVICES=("authentik" "authelia" "lldap" "vaultwarden" "postgres")
for svc in "${CERBERUS_SERVICES[@]}"; do
    mkdir -p "/opt/cerberus/${svc}"
    chown -R 1000:1000 "/opt/cerberus/${svc}"
done
echo "  ✓ Cerberus directories ready"

echo "=== [5/9] Setting up Charon (Sync & Backup) ==="
mkdir -p /opt/charon/rclone/sync /opt/charon/rclone/archive
chown -R 1000:1000 /opt/charon
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}/second-brain"
if [ -d "${VAULT_PATH}" ]; then
    chown -R 1000:1000 "${VAULT_PATH}"
    chmod -R o+w "${VAULT_PATH}"
fi
echo "  ✓ Charon directories ready"

echo "=== [6/9] Setting up Kratos (Workout Tracker) ==="
mkdir -p /opt/kratos
chown -R 1000:1000 /opt/kratos
echo "  ✓ Kratos directories ready"

echo "=== [7/9] Setting up Palworld Game Server ==="
mkdir -p /opt/palworld/data /opt/palworld/backups
chown -R 1000:1000 /opt/palworld
echo "  ✓ Palworld directories ready"

echo "=== [8/9] Git Global Optimizations ==="
git config --global status.submoduleSummary false
git config --global diff.ignoreSubmodules all
git config --global submodule.recurse false
echo "  ✓ Git submodule optimizations applied"

echo "=== [9/9] Installing Centralized Deployment Utilities (/usr/local/bin) ==="
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/deploy.sh -o /usr/local/bin/deploy.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/ensure_secret.sh -o /usr/local/bin/ensure_secret.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/prune_secrets.sh -o /usr/local/bin/prune_secrets.sh
chmod +x /usr/local/bin/deploy.sh /usr/local/bin/ensure_secret.sh /usr/local/bin/prune_secrets.sh
echo "  ✓ Deployment utilities installed to /usr/local/bin"

echo ""
echo "=============================================================================="
echo "✓ Gaia Host Provisioning Complete! Ready for Swarm deployments."
echo "=============================================================================="

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

echo "=== [1/8] Setting up Olympus (Ingress & DNS) ==="
mkdir -p /opt/olympus/adguard/conf /opt/olympus/adguard/work

# Migrate existing AdGuard Home configuration from Poseidon if present
if [ -d "/opt/poseidon/conf" ] && [ -n "$(ls -A /opt/poseidon/conf 2>/dev/null)" ]; then
    echo "  → Migrating existing AdGuard config from /opt/poseidon/conf..."
    cp -an /opt/poseidon/conf/* /opt/olympus/adguard/conf/ 2>/dev/null || true
fi
if [ -d "/opt/poseidon/work" ] && [ -n "$(ls -A /opt/poseidon/work 2>/dev/null)" ]; then
    echo "  → Migrating existing AdGuard work data from /opt/poseidon/work..."
    cp -an /opt/poseidon/work/* /opt/olympus/adguard/work/ 2>/dev/null || true
fi
chown -R 1000:1000 /opt/olympus/adguard
echo "  ✓ Olympus directories ready"

echo "=== [2/8] Setting up Odin (Hermes & AI Lab) ==="
mkdir -p /opt/odin/hermes/home /opt/odin/mnemosyne /mnt/storage/backups/odin/hermes
chown -R 1000:1000 /opt/odin /mnt/storage/backups/odin/hermes
chmod -R 775 /opt/odin/hermes /opt/odin/mnemosyne

# Ensure agy CLI symlink exists if installed in user directory
TARGET_USER=${SUDO_USER:-$(id -un)}
if [ -f "/home/${TARGET_USER}/.local/bin/agy" ]; then
    ln -sf "/home/${TARGET_USER}/.local/bin/agy" /usr/local/bin/agy
fi
echo "  ✓ Odin directories ready"

echo "=== [3/8] Setting up Apollo Core (Manager Services) ==="
APOLLO_GAIA_SERVICES=("jellystat" "jellystat-db" "prowlarr" "seerr" "recyclarr")
for svc in "${APOLLO_GAIA_SERVICES[@]}"; do
    mkdir -p "/opt/apollo-core/${svc}"
    chown -R 1000:1000 "/opt/apollo-core/${svc}"
done
mkdir -p /mnt/storage/backups/apollo/prowlarr
chown -R 1000:1000 /mnt/storage/backups/apollo/prowlarr
echo "  ✓ Apollo Core (Gaia) directories ready"

echo "=== [4/8] Setting up Cerberus (Auth & Identity) ==="
CERBERUS_SERVICES=("authentik" "authelia" "lldap" "vaultwarden" "postgres")
for svc in "${CERBERUS_SERVICES[@]}"; do
    mkdir -p "/opt/cerberus/${svc}"
    chown -R 1000:1000 "/opt/cerberus/${svc}"
done
echo "  ✓ Cerberus directories ready"

echo "=== [5/8] Setting up Charon (Sync & Backup) ==="
mkdir -p /opt/charon/rclone/sync /opt/charon/rclone/archive
chown -R 1000:1000 /opt/charon
VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}/second-brain"
if [ -d "${VAULT_PATH}" ]; then
    chown -R 1000:1000 "${VAULT_PATH}"
    chmod -R o+w "${VAULT_PATH}"
fi
echo "  ✓ Charon directories ready"

echo "=== [6/8] Setting up Kratos (Workout Tracker) ==="
mkdir -p /opt/kratos
chown -R 1000:1000 /opt/kratos
echo "  ✓ Kratos directories ready"

echo "=== [7/8] Setting up Palworld Game Server ==="
mkdir -p /opt/palworld/data /opt/palworld/backups
chown -R 1000:1000 /opt/palworld
echo "  ✓ Palworld directories ready"

echo "=== [8/8] Git Global Optimizations ==="
git config --global status.submoduleSummary false
git config --global diff.ignoreSubmodules all
git config --global submodule.recurse false
echo "  ✓ Git submodule optimizations applied"

echo ""
echo "=============================================================================="
echo "✓ Gaia Host Provisioning Complete! Ready for Swarm deployments."
echo "=============================================================================="

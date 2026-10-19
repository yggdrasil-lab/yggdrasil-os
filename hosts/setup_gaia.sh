#!/bin/bash
set -e

# ==============================================================================
# Yggdrasil OS - Gaia Host Setup & Directory Provisioning
# ==============================================================================
# Prepares host paths, permissions, and directories on the Gaia Manager node.
# Idempotent: safe to run multiple times.
#
# Usage (Zero-Clone):
#   curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/setup_gaia.sh | sudo bash
# ==============================================================================

echo "=== [1/10] Installing Host Prerequisites (gh, jq, base tooling) ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
if [ -f "${SCRIPT_DIR}/install_prerequisites.sh" ]; then
    bash "${SCRIPT_DIR}/install_prerequisites.sh"
else
    curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/install_prerequisites.sh | bash
fi

# Clean up deprecated standalone notify-discord.sh (migrated to central Iris gateway)
rm -f /usr/local/bin/notify-discord.sh

echo "=== [2/10] Setting up Olympus (Ingress & DNS) ==="
mkdir -p /opt/olympus/adguard/conf /opt/olympus/adguard/work
chown -R 1000:1000 /opt/olympus/adguard
echo "  ✓ Olympus directories ready"

echo "=== [3/10] Setting up Odin (Hermes & AI Lab) ==="
mkdir -p /opt/odin/hermes/home /opt/odin/mnemosyne /mnt/storage/backups/odin/hermes
chown -R 1000:1000 /opt/odin /mnt/storage/backups/odin/hermes
chmod -R 775 /opt/odin/hermes /opt/odin/mnemosyne

# Ensure agy CLI symlink exists if installed in user directory
TARGET_USER=${SUDO_USER:-$(id -un)}
TARGET_GROUP="$(id -gn "${TARGET_USER}" 2>/dev/null || echo "${TARGET_USER}")"
if [ -f "/home/${TARGET_USER}/.local/bin/agy" ]; then
    ln -sf "/home/${TARGET_USER}/.local/bin/agy" /usr/local/bin/agy
fi
echo "  ✓ Odin directories ready"

echo "=== [4/10] Setting up Apollo Core (Manager Services) ==="
APOLLO_GAIA_SERVICES=("jellystat" "prowlarr" "seerr" "recyclarr")
for svc in "${APOLLO_GAIA_SERVICES[@]}"; do
    mkdir -p "/opt/apollo-core/${svc}"
    chown -R 1000:1000 "/opt/apollo-core/${svc}"
done
mkdir -p /opt/apollo-core/jellystat-db
# Note: /opt/apollo-core/jellystat-db is managed by PostgreSQL's entrypoint directly (do not recursive chown)
mkdir -p /mnt/storage/backups/apollo/prowlarr
chown -R 1000:1000 /mnt/storage/backups/apollo/prowlarr
echo "  ✓ Apollo Core (Gaia) directories ready"

echo "=== [5/10] Setting up Cerberus (Auth & Identity) ==="
CERBERUS_SERVICES=("authentik" "authelia" "lldap")
for svc in "${CERBERUS_SERVICES[@]}"; do
    mkdir -p "/opt/cerberus/${svc}"
    chown -R 1000:1000 "/opt/cerberus/${svc}"
done
mkdir -p /opt/cerberus/postgres
# Note: /opt/cerberus/postgres is managed by PostgreSQL's entrypoint directly (do not recursive chown)
mkdir -p /mnt/storage/backups/lldap /mnt/storage/backups/authentik
chown -R 1000:1000 /mnt/storage/backups/lldap /mnt/storage/backups/authentik
echo "  ✓ Cerberus directories ready"

echo "=== [6/10] Setting up Charon (Sync & Backup) ==="
mkdir -p /opt/charon/rclone/sync /opt/charon/rclone/archive
chown -R 1000:1000 /opt/charon
VAULT_BASE="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}"
VAULT_PATH="${VAULT_BASE}/second-brain"
if [ ! -d "${VAULT_PATH}" ]; then
    mkdir -p "${VAULT_PATH}"
fi
chown -R 1000:1000 "${VAULT_PATH}"
chmod -R o+w "${VAULT_PATH}"
if [ -d "${VAULT_BASE}/.git" ]; then
    chown -R "${TARGET_USER}:${TARGET_GROUP}" "${VAULT_BASE}/.git"
    echo "  ✓ Vault git repository ownership ensured (${TARGET_USER}:${TARGET_GROUP})"
fi
echo "  ✓ Charon directories ready"

echo "=== [7/10] Setting up Kratos (Workout Tracker) ==="
mkdir -p /opt/kratos/data
chown 1000:1000 /opt/kratos
# Note: /opt/kratos/data is managed by PostgreSQL's entrypoint directly (do not recursive chown)
echo "  ✓ Kratos directories ready"

echo "=== [8/10] Setting up Palworld Game Server ==="
mkdir -p /opt/palworld/data /opt/palworld/backups
chown -R 1000:1000 /opt/palworld
echo "  ✓ Palworld directories ready"

echo "=== [9/10] Git Global Optimizations ==="
git config --global status.submoduleSummary false
git config --global diff.ignoreSubmodules all
git config --global submodule.recurse false
if [ -n "${SUDO_USER}" ] && [ "${SUDO_USER}" != "root" ]; then
    sudo -u "${TARGET_USER}" git config --global status.submoduleSummary false
    sudo -u "${TARGET_USER}" git config --global diff.ignoreSubmodules all
    sudo -u "${TARGET_USER}" git config --global submodule.recurse false
fi
echo "  ✓ Git submodule optimizations applied"

echo "=== [10/10] Installing Centralized Deployment Utilities (/usr/local/bin) ==="
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/deploy.sh -o /usr/local/bin/deploy.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/ensure_secret.sh -o /usr/local/bin/ensure_secret.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/prune_secrets.sh -o /usr/local/bin/prune_secrets.sh
chmod +x /usr/local/bin/deploy.sh /usr/local/bin/ensure_secret.sh /usr/local/bin/prune_secrets.sh
echo "  ✓ Deployment utilities installed to /usr/local/bin"

# Final sweep: Ensure non-root ownership of vault git repository
if [ -d "${VAULT_BASE}/.git" ]; then
    chown -R "${TARGET_USER}:${TARGET_GROUP}" "${VAULT_BASE}/.git"
fi

echo ""
echo "=============================================================================="
echo "✓ Gaia Host Provisioning Complete! Ready for Swarm deployments."
echo "=============================================================================="

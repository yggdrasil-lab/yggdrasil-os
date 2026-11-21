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

echo "=== [1/14] Installing Host Prerequisites (gh, jq, base tooling) ==="
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
fi
if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/install_prerequisites.sh" ]; then
    bash "${SCRIPT_DIR}/install_prerequisites.sh"
else
    curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/install_prerequisites.sh | bash
fi

# Clean up deprecated standalone notify-discord.sh (migrated to central Iris gateway)
rm -f /usr/local/bin/notify-discord.sh

echo "=== [2/14] Setting up Olympus (Ingress & DNS) ==="
mkdir -p /opt/olympus/adguard/conf /opt/olympus/adguard/work
chown -R 1000:1000 /opt/olympus/adguard
echo "  ✓ Olympus directories ready"

echo "=== [3/14] Setting up Odin (Hermes & AI Lab) ==="
mkdir -p /opt/odin/hermes/home /opt/odin/mnemosyne /mnt/storage/backups/odin/hermes
chown -R 1000:1000 /opt/odin /mnt/storage/backups/odin/hermes
chmod -R 775 /opt/odin/mnemosyne
# Preserve strict private-node socket/profile directory permissions (go-w) for Hermes gateway_runtime_discovery
chmod 755 /opt/odin/hermes /opt/odin/hermes/profiles /opt/odin/hermes/profiles/* 2>/dev/null || true
chmod 600 /opt/odin/hermes/gateway.sock /opt/odin/hermes/gateway.lock /opt/odin/hermes/profiles/*/gateway.lock 2>/dev/null || true

# Ensure agy CLI symlink and canonical turnkey CLI symlinks exist in user directory
TARGET_USER=${SUDO_USER:-$(id -un)}
TARGET_GROUP="$(id -gn "${TARGET_USER}" 2>/dev/null || echo "${TARGET_USER}")"
USER_LOCAL_BIN="${USER_LOCAL_BIN:-/home/${TARGET_USER}/.local/bin}"
mkdir -p "${USER_LOCAL_BIN}"
if [ -f "${USER_LOCAL_BIN}/agy" ]; then
    ln -sf "${USER_LOCAL_BIN}/agy" /usr/local/bin/agy
fi
VAULT_ROOT="${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/morning_copilot_aggregator.py" "${USER_LOCAL_BIN}/morning-copilot"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/mnemosyne_consolidate_daemon.py" "${USER_LOCAL_BIN}/mnemosyne-dreaming"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/mnemosyne_associative.py" "${USER_LOCAL_BIN}/mnemosyne-associative"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/mnemosyne_temporal_hygiene.py" "${USER_LOCAL_BIN}/mnemosyne-hygiene-audit"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/mimir_code_graph.py" "${USER_LOCAL_BIN}/mimir-code"
ln -sf "${VAULT_ROOT}/Forge/odin/hermes-agent/scripts/forseti_visual_gatekeeper.py" "${USER_LOCAL_BIN}/forseti-visual-gatekeeper"
ln -sf "/opt/odin/hermes/skills/devops/forge-pr-workflow/scripts/publish_pwa_visual_review.py" "${USER_LOCAL_BIN}/publish-pwa-visual-review"
ln -sf "/opt/odin/hermes/skills/health/workout/scripts/kratos_workout_cli.py" "${USER_LOCAL_BIN}/kratos-workout"
ln -sf "${VAULT_ROOT}/.agents/skills/romm-ops/scripts/romm_manage.py" "${USER_LOCAL_BIN}/romm-manage"
chown -h "${TARGET_USER}:${TARGET_GROUP}" "${USER_LOCAL_BIN}"/* 2>/dev/null || true
echo "  ✓ Odin directories and turnkey CLI symlinks ready"

echo "=== [4/14] Setting up Apollo Core (Manager Services) ==="
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

echo "=== [5/14] Setting up Cerberus (Auth & Identity) ==="
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

echo "=== [6/14] Setting up Charon (Sync & Backup) ==="
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

echo "=== [7/14] Setting up Kratos (Workout Tracker) ==="
mkdir -p /opt/kratos/data
chown 1000:1000 /opt/kratos
# Note: /opt/kratos/data is managed by PostgreSQL's entrypoint directly (do not recursive chown)
echo "  ✓ Kratos directories ready"

echo "=== [8/14] Setting up Eir (Health Telemetry & Wearables) ==="
mkdir -p /opt/eir/data
chown 1000:1000 /opt/eir
# Note: /opt/eir/data is managed by PostgreSQL's entrypoint directly (do not recursive chown)
mkdir -p /mnt/storage/backups/eir
chown -R 1000:1000 /mnt/storage/backups/eir
echo "  ✓ Eir directories ready"

echo "=== [9/14] Setting up Iris (Notification & Inbound Gateway) ==="
mkdir -p /opt/iris/data
chown 1000:1000 /opt/iris
# Note: /opt/iris/data is managed by PostgreSQL's entrypoint directly (do not recursive chown)
echo "  ✓ Iris directories ready"

echo "=== [10/14] Setting up Palworld Game Server ==="
mkdir -p /opt/palworld/data /opt/palworld/backups
chown -R 1000:1000 /opt/palworld
echo "  ✓ Palworld directories ready"

echo "=== [11/14] Setting up Hephaestus (Private Container Registry) ==="
mkdir -p /mnt/storage/registry
chown 1000:1000 /mnt/storage/registry
echo "  ✓ Hephaestus directories ready"

echo "=== [12/14] Git Global Optimizations ==="
git config --global status.submoduleSummary false
git config --global diff.ignoreSubmodules all
git config --global submodule.recurse false
if [ -n "${SUDO_USER}" ] && [ "${SUDO_USER}" != "root" ]; then
    sudo -u "${TARGET_USER}" git config --global status.submoduleSummary false
    sudo -u "${TARGET_USER}" git config --global diff.ignoreSubmodules all
    sudo -u "${TARGET_USER}" git config --global submodule.recurse false
fi
echo "  ✓ Git submodule optimizations applied"

echo "=== [13/14] Installing Centralized Deployment Utilities (/usr/local/bin) ==="
RAW_BASE="${RAW_BASE:-https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main}"
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/deploy.sh -o /usr/local/bin/deploy.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/ensure_secret.sh -o /usr/local/bin/ensure_secret.sh
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/prune_secrets.sh -o /usr/local/bin/prune_secrets.sh
chmod +x /usr/local/bin/deploy.sh /usr/local/bin/ensure_secret.sh /usr/local/bin/prune_secrets.sh

install_central_cli() {
    local script_name="${1:?script_name required}"
    local bin_name="${2:?bin_name required}"
    local central_bin_dir="${CENTRAL_BIN_DIR:-/usr/local/bin}"
    local target_bin="${central_bin_dir}/${bin_name}"

    mkdir -p "${central_bin_dir}"
    if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/scripts/${script_name}" ]; then
        install -m 0755 "${SCRIPT_DIR}/scripts/${script_name}" "${target_bin}"
    else
        curl -fsSL "${RAW_BASE}/hosts/scripts/${script_name}" -o "${target_bin}"
        chmod 0755 "${target_bin}"
    fi

    if [ ! -d "${USER_LOCAL_BIN}" ]; then
        mkdir -p "${USER_LOCAL_BIN}"
        chown "${TARGET_USER}:${TARGET_GROUP}" "${USER_LOCAL_BIN}" 2>/dev/null || true
    fi
    ln -sf "${target_bin}" "${USER_LOCAL_BIN}/${bin_name}"
    chown -h "${TARGET_USER}:${TARGET_GROUP}" "${USER_LOCAL_BIN}/${bin_name}" 2>/dev/null || true
}

CENTRAL_CLI_SPECS=(
    "charon-restore.sh:charon-restore"
    "swarm-exec.sh:swarm-exec"
    "mnemosyne.sh:mnemosyne"
)
for cli_spec in "${CENTRAL_CLI_SPECS[@]}"; do
    install_central_cli "${cli_spec%%:*}" "${cli_spec#*:}"
done

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/../network/prune_network_endpoints.py" ]; then
    install -m 0755 "${SCRIPT_DIR}/../network/prune_network_endpoints.py" /usr/local/bin/prune-network-endpoints
else
    curl -fsSL "${RAW_BASE}/network/prune_network_endpoints.py" -o /usr/local/bin/prune-network-endpoints
    chmod 0755 /usr/local/bin/prune-network-endpoints
fi

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/../network/pin_registry_host.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/../network/pin_registry_host.sh" /usr/local/bin/pin-registry-host
else
    curl -fsSL "${RAW_BASE}/network/pin_registry_host.sh" -o /usr/local/bin/pin-registry-host
    chmod 0755 /usr/local/bin/pin-registry-host
fi
echo "  ✓ Deployment utilities, Charon restore CLI, swarm-exec, mnemosyne, prune-network-endpoints, and pin-registry-host installed to /usr/local/bin"

echo "  -> Pinning local registry hostname in /etc/hosts (NODE_ROLE=manager)..."
NODE_ROLE=manager /usr/local/bin/pin-registry-host

echo "=== [14/14] Setting up GitHub Runner Auto-Update Timer ==="
if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/scripts/update-github-runner.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/update-github-runner.sh" /usr/local/bin/update-github-runner.sh
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/update-github-runner.sh" -o /usr/local/bin/update-github-runner.sh
    chmod 0755 /usr/local/bin/update-github-runner.sh
fi

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/systemd/update-github-runner.service" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/update-github-runner.service" /etc/systemd/system/update-github-runner.service
else
    curl -fsSL "${RAW_BASE}/hosts/systemd/update-github-runner.service" -o /etc/systemd/system/update-github-runner.service
    chmod 0644 /etc/systemd/system/update-github-runner.service
fi

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/systemd/update-github-runner.timer" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/update-github-runner.timer" /etc/systemd/system/update-github-runner.timer
else
    curl -fsSL "${RAW_BASE}/hosts/systemd/update-github-runner.timer" -o /etc/systemd/system/update-github-runner.timer
    chmod 0644 /etc/systemd/system/update-github-runner.timer
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable --now update-github-runner.timer
    echo "  ✓ GitHub runner auto-update timer enabled"
fi

# Final sweep: Ensure non-root ownership of vault git repository
if [ -d "${VAULT_BASE}/.git" ]; then
    chown -R "${TARGET_USER}:${TARGET_GROUP}" "${VAULT_BASE}/.git"
fi

# Ensure non-root ownership of Forge frontend build and Playwright artifact directories
for dir_name in dist test-results playwright-report "node_modules/.vite"; do
    for artifact_dir in "${VAULT_BASE}"/Forge/*/${dir_name} "${VAULT_BASE}"/Forge/*/web/${dir_name}; do
        if [ -d "${artifact_dir}" ]; then
            chown -R "${TARGET_USER}:${TARGET_GROUP}" "${artifact_dir}"
        fi
    done
done

echo ""
echo "=============================================================================="
echo "✓ Gaia Host Provisioning Complete! Ready for Swarm deployments."
echo "=============================================================================="

#!/bin/bash
# docker/restart_docker.sh
# Gracefully restarts Docker daemon on Gaia host and orchestrates clean Swarm recovery.

set -e

# Self-update if running from /usr/local/bin/gaia-restart-docker.sh
if [ "$0" = "/usr/local/bin/gaia-restart-docker.sh" ]; then
    UPDATE_URL="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/docker/restart_docker.sh"
    TMP_SCRIPT="/tmp/gaia-restart-docker-update.sh"
    if curl -fsSL --connect-timeout 5 "$UPDATE_URL" -o "$TMP_SCRIPT" 2>/dev/null; then
        if ! cmp -s "$TMP_SCRIPT" "$0"; then
            echo "Updating $0 to latest version from GitHub..."
            mv "$TMP_SCRIPT" "$0"
            chmod 0755 "$0"
            exec "$0" "$@"
        fi
        rm -f "$TMP_SCRIPT"
    fi
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

echo "=== [1/3] Flushing Storage Buffers ==="
# Ensure databases (Postgres, SQLite, Valkey) write pending buffers to disk
sync
echo "Buffers synced."

echo "=== [2/3] Restarting Docker Service ==="
systemctl restart docker
echo "Docker daemon restarted."

echo "=== [3/3] Orchestrating Swarm Recovery & Health Convergence ==="
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

if [ -x "/usr/local/bin/gaia-swarm-startup.sh" ]; then
    /usr/local/bin/gaia-swarm-startup.sh
elif [ -f "${SCRIPT_DIR}/../swarm/startup_orchestrator.sh" ]; then
    bash "${SCRIPT_DIR}/../swarm/startup_orchestrator.sh"
else
    curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/swarm/startup_orchestrator.sh | bash
fi

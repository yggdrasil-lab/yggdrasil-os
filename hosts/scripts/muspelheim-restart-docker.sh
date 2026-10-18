#!/bin/bash
# ==============================================================================
# Yggdrasil OS - Graceful Docker Restart for Muspelheim (Worker Node)
# ==============================================================================
# Flushes disk buffers, cleanly stops standalone Apollo Supply downloaders,
# restarts the Docker daemon, restores Apollo Supply with VPN health check,
# and prunes dead container task shells.
# ==============================================================================

set -e

# Self-update if running from /usr/local/bin/muspelheim-restart-docker.sh
if [ "$0" = "/usr/local/bin/muspelheim-restart-docker.sh" ]; then
    UPDATE_URL="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/scripts/muspelheim-restart-docker.sh"
    TMP_SCRIPT="/tmp/muspelheim-restart-docker-update.sh"
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

echo "=== [1/5] Flushing Storage Buffers ==="
# Protect Arr databases, torrent states, and filesystem metadata on /mnt/storage
sync
echo "  ✓ Buffers synced to disk"

echo "=== [2/5] Gracefully Stopping Apollo Supply Downloaders ==="
# Stop dependents before Gluetun VPN drops to prevent connection errors
if docker info >/dev/null 2>&1; then
    docker stop slskd sabnzbd qbittorrent gluetun 2>/dev/null || true
    echo "  ✓ Apollo Supply containers stopped"
else
    echo "  ! Docker daemon not running; skipping container stop"
fi

echo "=== [3/5] Restarting Docker Service ==="
systemctl restart docker
echo "  ✓ Docker service restarted"

echo "=== [4/5] Waiting for Docker Daemon Readiness ==="
MAX_WAIT=30
WAITED=0
until docker info >/dev/null 2>&1 || [ "$WAITED" -ge "$MAX_WAIT" ]; do
    sleep 2
    WAITED=$((WAITED + 2))
done

if ! docker info >/dev/null 2>&1; then
    echo "Error: Docker daemon failed to become responsive after ${MAX_WAIT}s"
    exit 1
fi
echo "  ✓ Docker daemon is online and responsive"

echo "=== [5/5] Restoring Apollo Supply & Pruning Exited Debris ==="
# Invoke restore orchestrator to start Gluetun, wait for health, and start dependents
if [ -x "/usr/local/bin/apollo-supply-restore.sh" ]; then
    /usr/local/bin/apollo-supply-restore.sh
elif [ -f "$(dirname "${BASH_SOURCE[0]}")/apollo-supply-restore.sh" ]; then
    bash "$(dirname "${BASH_SOURCE[0]}")/apollo-supply-restore.sh"
else
    curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/scripts/apollo-supply-restore.sh | bash
fi

# Prune dead/exited containers from Swarm worker tasks
docker container prune -f >/dev/null 2>&1 || true
echo "  ✓ Exited container debris pruned"

echo ""
echo "=============================================================================="
echo "✓ Muspelheim Docker restart and recovery complete!"
echo "=============================================================================="

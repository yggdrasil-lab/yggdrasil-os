#!/bin/bash
# maintenance/cleanup_storage.sh
# Automated monthly disk storage cleanup for Gaia host.
# Prunes stale Docker build cache (>168h), unused Docker images, and vacuums systemd journal.

set -e

# Self-update if installed at /usr/local/bin/gaia-cleanup.sh and online
if [ "$0" = "/usr/local/bin/gaia-cleanup.sh" ]; then
    UPDATE_URL="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/maintenance/cleanup_storage.sh"
    TMP_SCRIPT="/tmp/gaia-cleanup-update.sh"
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

echo "=== Gaia Storage Cleanup Started: $(date) ==="
echo "Disk space before cleanup:"
df -h /

echo "--- Pruning Docker build cache (older than 168h / 7 days) ---"
docker builder prune -a -f --filter "until=168h"

echo "--- Pruning unused Docker images ---"
docker image prune -a -f

echo "--- Pruning Hephaestus Docker Registry unreferenced blobs ---"
REGISTRY_CID=$(docker ps -q --filter label=com.docker.swarm.service.name=hephaestus_registry 2>/dev/null || true)
if [ -n "$REGISTRY_CID" ]; then
    echo "Running registry garbage-collect inside container $REGISTRY_CID..."
    docker exec "$REGISTRY_CID" registry garbage-collect /etc/docker/registry/config.yml -m || true
else
    echo "Hephaestus registry container not running on this host; skipping registry GC."
fi

echo "--- Vacuuming systemd journal logs to 500M ---"
journalctl --vacuum-size=500M

echo "Disk space after cleanup:"
df -h /
echo "=== Gaia Storage Cleanup Finished: $(date) ==="

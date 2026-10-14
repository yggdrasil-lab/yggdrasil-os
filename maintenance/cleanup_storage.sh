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

echo "--- Vacuuming systemd journal logs to 500M ---"
journalctl --vacuum-size=500M

echo "Disk space after cleanup:"
df -h /
echo "=== Gaia Storage Cleanup Finished: $(date) ==="

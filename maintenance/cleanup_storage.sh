#!/bin/bash
# maintenance/cleanup_storage.sh
# Automated monthly disk storage cleanup for Gaia host.
# Prunes stale Docker build cache (>168h), unused Docker images, and vacuums systemd journal.

set -e

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

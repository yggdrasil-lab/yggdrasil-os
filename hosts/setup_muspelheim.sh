#!/bin/bash
set -e

# ==============================================================================
# Yggdrasil OS - Muspelheim Host Setup & Directory Provisioning
# ==============================================================================
# Prepares host paths, permissions, and directories on the Muspelheim Worker node.
# Idempotent: safe to run multiple times.
#
# Usage (Zero-Clone):
#   curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/hosts/setup_muspelheim.sh | sudo bash
# ==============================================================================

echo "=== [1/5] Setting up Comics (Komga & Suwayomi) ==="
mkdir -p /opt/comics/komga /opt/comics/suwayomi
mkdir -p /mnt/storage/comics/library
chown -R 1000:1000 /opt/comics /mnt/storage/comics/library
echo "  ✓ Comics directories ready"

echo "=== [2/5] Setting up Apollo Core (Media & Storage) ==="
APOLLO_MUSPEL_SERVICES=("jellyfin" "sonarr" "radarr" "lidarr" "audiobookshelf" "bazarr" "shelfarr")
for svc in "${APOLLO_MUSPEL_SERVICES[@]}"; do
    mkdir -p "/opt/apollo-core/${svc}"
    chown -R 1000:1000 "/opt/apollo-core/${svc}"
done

MEDIA_SUBDIRS=("Movies" "TV" "Music" "Audiobooks" "Books" "Youtube/audio" "Youtube/video")
for subdir in "${MEDIA_SUBDIRS[@]}"; do
    mkdir -p "/mnt/storage/media/${subdir}"
    chown -R 1000:1000 "/mnt/storage/media/${subdir}"
done

mkdir -p /mnt/storage/downloads
chown -R 1000:1000 /mnt/storage/downloads

APOLLO_BACKUPS=("sonarr" "radarr" "lidarr" "prowlarr" "audiobookshelf" "bazarr")
for bkp in "${APOLLO_BACKUPS[@]}"; do
    mkdir -p "/mnt/storage/backups/apollo/${bkp}"
    chown -R 1000:1000 "/mnt/storage/backups/apollo/${bkp}"
done
echo "  ✓ Apollo Core (Muspelheim) directories ready"

echo "=== [3/5] Setting up Apollo Supply (VPN Downloader Stack) ==="
mkdir -p /opt/apollo-supply/gluetun /opt/apollo-supply/qbittorrent /opt/apollo-supply/sabnzbd /opt/apollo-supply/slskd
chown -R 1000:1000 /opt/apollo-supply
echo "  ✓ Apollo Supply directories ready"

echo "=== [4/5] Setting up Immich (Photos & AI) ==="
mkdir -p /mnt/storage/immich/library /opt/immich/model-cache /opt/immich/postgres
chown -R 1000:1000 /mnt/storage/immich/library /opt/immich/model-cache
# Note: /opt/immich/postgres is managed by PostgreSQL's entrypoint directly
echo "  ✓ Immich directories ready"

echo "=== [5/5] Setting up Cerberus (Vaultwarden on Muspelheim) ==="
mkdir -p /opt/cerberus/vaultwarden /mnt/storage/backups/vaultwarden
chown -R 1000:1000 /opt/cerberus/vaultwarden /mnt/storage/backups/vaultwarden
echo "  ✓ Cerberus (Vaultwarden) directories ready"

echo ""
echo "=============================================================================="
echo "✓ Muspelheim Host Provisioning Complete! Ready for deployments."
echo "=============================================================================="

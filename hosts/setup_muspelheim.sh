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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
RAW_BASE="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main"

# Clean up deprecated standalone notify-discord.sh (migrated to central Iris gateway)
rm -f /usr/local/bin/notify-discord.sh

echo "=== [1/8] Setting up Comics (Komga, Suwayomi & Komf) ==="
mkdir -p /opt/comics/komga /opt/comics/suwayomi /opt/comics/komf
mkdir -p /mnt/storage/comics/library
chown -R 1000:1000 /opt/comics /mnt/storage/comics/library
echo "  ✓ Comics directories ready"

echo "=== [2/8] Setting up Apollo Core (Media & Storage) ==="
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

echo "=== [3/8] Setting up Apollo Supply (VPN Downloader Stack) ==="
mkdir -p /opt/apollo-supply/gluetun /opt/apollo-supply/qbittorrent /opt/apollo-supply/sabnzbd /opt/apollo-supply/slskd
mkdir -p /mnt/storage/downloads/slskd/.incomplete
chown -R 1000:1000 /opt/apollo-supply /mnt/storage/downloads/slskd
echo "  ✓ Apollo Supply directories ready"

echo "  → Installing Apollo Supply boot persistence service..."
if [ -f "${SCRIPT_DIR}/scripts/apollo-supply-restore.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/apollo-supply-restore.sh" /usr/local/bin/apollo-supply-restore.sh
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/apollo-supply-restore.sh" -o /usr/local/bin/apollo-supply-restore.sh
    chmod 0755 /usr/local/bin/apollo-supply-restore.sh
fi

if [ -f "${SCRIPT_DIR}/systemd/apollo-supply.service" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/apollo-supply.service" /etc/systemd/system/apollo-supply.service
else
    curl -fsSL "${RAW_BASE}/hosts/systemd/apollo-supply.service" -o /etc/systemd/system/apollo-supply.service
    chmod 0644 /etc/systemd/system/apollo-supply.service
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl daemon-reload
    systemctl enable apollo-supply.service
    echo "  ✓ Apollo Supply persistence service enabled"
fi

echo "  → Installing Muspelheim graceful Docker restart tool..."
if [ -f "${SCRIPT_DIR}/scripts/muspelheim-restart-docker.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/muspelheim-restart-docker.sh" /usr/local/bin/muspelheim-restart-docker.sh
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/muspelheim-restart-docker.sh" -o /usr/local/bin/muspelheim-restart-docker.sh
    chmod 0755 /usr/local/bin/muspelheim-restart-docker.sh
fi
echo "  ✓ muspelheim-restart-docker.sh installed to /usr/local/bin"

echo "=== [4/8] Setting up Immich (Photos & AI) ==="
mkdir -p /mnt/storage/immich/library /opt/immich/model-cache /opt/immich/postgres
chown -R 1000:1000 /mnt/storage/immich/library /opt/immich/model-cache
# Note: /opt/immich/postgres is managed by PostgreSQL's entrypoint directly
echo "  ✓ Immich directories ready"

echo "=== [5/8] Setting up Cerberus (Vaultwarden on Muspelheim) ==="
mkdir -p /opt/cerberus/vaultwarden /mnt/storage/backups/vaultwarden
chown -R 1000:1000 /opt/cerberus/vaultwarden /mnt/storage/backups/vaultwarden
echo "  ✓ Cerberus (Vaultwarden) directories ready"

echo "=== [6/8] Setting up Charon (Sync & Backup Archive) ==="
mkdir -p /opt/charon/rclone/archive /mnt/storage/backups
chown -R 1000:1000 /opt/charon

if [ -f "${SCRIPT_DIR}/scripts/charon-restore.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/charon-restore.sh" /usr/local/bin/charon-restore
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/charon-restore.sh" -o /usr/local/bin/charon-restore
    chmod 0755 /usr/local/bin/charon-restore
fi

if [ -f "${SCRIPT_DIR}/scripts/swarm-exec.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/swarm-exec.sh" /usr/local/bin/swarm-exec
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/swarm-exec.sh" -o /usr/local/bin/swarm-exec
    chmod 0755 /usr/local/bin/swarm-exec
fi
echo "  ✓ Charon Archive directories, restore CLI, and swarm-exec ready"

echo "=== [7/8] Setting up RomM (Gaming & ROM Warehouse) ==="
mkdir -p /opt/romm/db /opt/romm/redis /opt/romm/config /opt/romm/syncthing
mkdir -p /mnt/storage/roms/library /mnt/storage/roms/assets /mnt/storage/roms/resources /mnt/storage/emulation/saves
chown -R 1000:1000 /opt/romm /mnt/storage/roms /mnt/storage/emulation
echo "  ✓ RomM directories ready"

echo "=== [8/8] Setting up GitHub Runner Auto-Update Timer ==="
if [ -f "${SCRIPT_DIR}/scripts/update-github-runner.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/scripts/update-github-runner.sh" /usr/local/bin/update-github-runner.sh
else
    curl -fsSL "${RAW_BASE}/hosts/scripts/update-github-runner.sh" -o /usr/local/bin/update-github-runner.sh
    chmod 0755 /usr/local/bin/update-github-runner.sh
fi

if [ -f "${SCRIPT_DIR}/systemd/update-github-runner.service" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/update-github-runner.service" /etc/systemd/system/update-github-runner.service
else
    curl -fsSL "${RAW_BASE}/hosts/systemd/update-github-runner.service" -o /etc/systemd/system/update-github-runner.service
    chmod 0644 /etc/systemd/system/update-github-runner.service
fi

if [ -f "${SCRIPT_DIR}/systemd/update-github-runner.timer" ]; then
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

echo ""
echo "=============================================================================="
echo "✓ Muspelheim Host Provisioning Complete! Ready for deployments."
echo "You can now run: sudo muspelheim-restart-docker.sh to safely restart Docker."
echo "=============================================================================="


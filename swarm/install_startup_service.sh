#!/bin/bash
# swarm/install_startup_service.sh
# Installs the Swarm Startup Orchestrator and Docker Graceful Restart tools on Gaia.
# Supports running locally from repo or directly piped via curl.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
RAW_BASE="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main"

echo "=== Installing Gaia Swarm Orchestration Tools ==="

echo "1. Installing startup orchestrator to /usr/local/bin/gaia-swarm-startup.sh..."
if [ -f "${SCRIPT_DIR}/startup_orchestrator.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/startup_orchestrator.sh" /usr/local/bin/gaia-swarm-startup.sh
else
    curl -fsSL "${RAW_BASE}/swarm/startup_orchestrator.sh" -o /usr/local/bin/gaia-swarm-startup.sh
    chmod 0755 /usr/local/bin/gaia-swarm-startup.sh
fi

echo "2. Installing graceful restart tool to /usr/local/bin/gaia-restart-docker.sh..."
if [ -f "${SCRIPT_DIR}/../docker/restart_docker.sh" ]; then
    install -m 0755 "${SCRIPT_DIR}/../docker/restart_docker.sh" /usr/local/bin/gaia-restart-docker.sh
else
    curl -fsSL "${RAW_BASE}/docker/restart_docker.sh" -o /usr/local/bin/gaia-restart-docker.sh
    chmod 0755 /usr/local/bin/gaia-restart-docker.sh
fi

echo "3. Installing Docker network boot delay drop-in..."
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/wait-for-network.conf <<EOF
[Unit]
After=network-online.target firewalld.service
Wants=network-online.target

[Service]
ExecStartPre=/bin/sleep 15
EOF

echo "4. Installing systemd startup service unit..."
if [ -f "${SCRIPT_DIR}/systemd/gaia-swarm-startup.service" ]; then
    install -m 0644 "${SCRIPT_DIR}/systemd/gaia-swarm-startup.service" /etc/systemd/system/gaia-swarm-startup.service
else
    curl -fsSL "${RAW_BASE}/swarm/systemd/gaia-swarm-startup.service" -o /etc/systemd/system/gaia-swarm-startup.service
    chmod 0644 /etc/systemd/system/gaia-swarm-startup.service
fi

echo "5. Reloading systemd daemon..."
systemctl daemon-reload

echo "6. Enabling gaia-swarm-startup.service on boot..."
systemctl enable gaia-swarm-startup.service

echo "7. Configuring Swarm task history retention limit to 1..."
if docker info >/dev/null 2>&1; then
    docker swarm update --task-history-limit 1 >/dev/null 2>&1 || true
fi

echo ""
echo "=== Installation Complete ==="
echo "You can now run:"
echo "  - sudo gaia-swarm-startup.sh   : Reconcile services, auto-heal backoffs, prune dead containers"
echo "  - sudo gaia-restart-docker.sh  : Gracefully sync buffers, restart dockerd, and recover clean state"
echo "At system boot (including after power outages), gaia-swarm-startup will execute automatically."

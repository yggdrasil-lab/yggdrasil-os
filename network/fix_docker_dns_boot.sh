#!/bin/bash
# scripts/fix_docker_dns_boot.sh

# This script configures Docker to wait for the network to be fully online before starting.
# It helps prevent DNS resolution issues (127.0.0.11 misbehaving) after power outages
# by ensuring the host has received valid DNS config from the router before Docker grabs it.

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

echo "Configuring Docker systemd override..."

# Create the systemd drop-in directory for Docker if it doesn't exist
mkdir -p /etc/systemd/system/docker.service.d

# Write the configuration to ensure Docker waits for the network
cat > /etc/systemd/system/docker.service.d/wait-for-network.conf <<EOF
[Unit]
# Wait until the network is actually "online" (not just the interface being up)
# This usually means having an IP address and connectivity.
After=network-online.target firewalld.service
Wants=network-online.target

[Service]
# Add a conservative 15-second delay to the startup.
# This is crucial after a power outage where the Router might still be booting up
# while the Server is already asking for DHCP/DNS.
ExecStartPre=/bin/sleep 15
EOF

echo "Reloading systemd daemon configuration..."
systemctl daemon-reload

echo "Success! Docker is now configured to wait for the network + 15s delay on boot."

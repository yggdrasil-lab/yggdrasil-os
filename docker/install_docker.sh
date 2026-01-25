#!/bin/bash
# scripts/install_docker.sh

# Installs Docker Engine on Debian/Ubuntu systems using the official repository.
# Reference: https://docs.docker.com/engine/install/ubuntu/

set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)."
    exit 1
fi

echo "Installing Docker Engine..."

# 1. Add Docker's official GPG key:
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

# 2. Add the repository to Apt sources:
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update

# 3. Install Docker packages
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. Post-installation steps (Manage as non-root)
# Get the actual user who invoked sudo, not root
REAL_USER=${SUDO_USER:-$USER}

if [ "$REAL_USER" != "root" ]; then
    echo "Configuring Docker for user: $REAL_USER"
    groupadd -f docker
    usermod -aG docker "$REAL_USER"
    echo "User $REAL_USER added to 'docker' group."
    echo "Please log out and log back in for this to take effect."
fi

echo "Docker installation complete!"
docker --version

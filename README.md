# Yggdrasil OS (Bootstrap)

> The roots of the World Tree. These scripts provision the bare-metal OS (Gaia & Muspelheim) to prepare them for the Docker Swarm.

## Directory Structure

*   `docker/`: Docker Engine installation and configuration.
*   `maintenance/`: Host storage maintenance scripts and systemd timers.
*   `network/`: Host networking fixes and overlay network creation.
*   `swarm/`: Swarm cluster initialization and management.

## Setup Instructions

### 0. Install Docker (Bare Metal)
Run this on a fresh Ubuntu/Debian server to install Docker Engine and configure user permissions.

```bash
sudo ./docker/install_docker.sh
```

### 1. Fix Docker DNS (All Nodes)
Run this on **every node** (Gaia & Muspelheim) immediately after installing Docker. It prevents the 127.0.0.11 DNS failure after power outages.

```bash
sudo ./network/fix_docker_dns_boot.sh
```

### 2. Initialize Swarm (Gaia Only)
Run this on the **Manager Node** (Gaia) to create the cluster.

```bash
./swarm/init_swarm.sh
```

### 3. Create Network (Gaia Only)
Run this once on the Manager Node to establish the encrypted overlay network.

```bash
./network/create_network.sh
```

### 4. Setup Monthly Storage Cleanup (Gaia Only)
Run this on the **Manager Node** (Gaia) to install the autonomous systemd timer that prunes Docker build cache (>7d), removes unused images, and vacuums systemd journals on the 1st of each month at 3 AM.

```bash
sudo ./maintenance/install_cleanup_timer.sh
```

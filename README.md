# Yggdrasil OS (Bootstrap)

> The roots of the World Tree. These scripts provision the bare-metal OS (Gaia & Muspelheim) to prepare them for the Docker Swarm.

## Directory Structure

*   `docker/`: Docker Engine installation and configuration.
*   `maintenance/`: Host storage maintenance scripts and systemd timers.
*   `network/`: Host networking fixes and overlay network creation.
*   `swarm/`: Swarm cluster initialization and management.

## Convention: Running via curl (Zero-Clone)

Going forward, scripts in this repository are designed to be executed or installed directly on bare-metal hosts using `curl` from GitHub raw URLs without needing to clone or pull the repository:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/<path/to/script.sh> | sudo bash
```

Scripts installed locally to `/usr/local/bin` (such as `gaia-cleanup.sh`) automatically self-update from GitHub on each run, ensuring host scripts stay up to date with zero manual steps.

## Setup Instructions

### 0. Install Docker (Bare Metal)
Run this on a fresh Ubuntu/Debian server to install Docker Engine and configure user permissions:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/docker/install_docker.sh | sudo bash
```

*(Or locally: `sudo ./docker/install_docker.sh`)*

### 1. Fix Docker DNS (All Nodes)
Run this on **every node** (Gaia & Muspelheim) immediately after installing Docker. It prevents the 127.0.0.11 DNS failure after power outages:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/network/fix_docker_dns_boot.sh | sudo bash
```

*(Or locally: `sudo ./network/fix_docker_dns_boot.sh`)*

### 2. Initialize Swarm (Gaia Only)
Run this on the **Manager Node** (Gaia) to create the cluster:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/swarm/init_swarm.sh | bash
```

*(Or locally: `./swarm/init_swarm.sh`)*

### 3. Create Network (Gaia Only)
Run this once on the Manager Node to establish the encrypted overlay network:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/network/create_network.sh | bash
```

*(Or locally: `./network/create_network.sh`)*

### 4. Setup Monthly Storage Cleanup (Gaia Only)
Run this on the **Manager Node** (Gaia) to install the autonomous systemd timer that prunes Docker build cache (>7d), removes unused images, and vacuums systemd journals on the 1st of each month at 3 AM:

```bash
curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/maintenance/install_cleanup_timer.sh | sudo bash
```

*(Or locally: `sudo ./maintenance/install_cleanup_timer.sh`)*

Once installed:
* **Automatic:** Runs on the 1st of each month at 3:00 AM NZDT via `gaia-cleanup.timer` and self-updates from GitHub before running.
* **Manual trigger:** Run `sudo gaia-cleanup.sh` from any directory, or run one-off via `curl -fsSL https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/maintenance/cleanup_storage.sh | sudo bash`.

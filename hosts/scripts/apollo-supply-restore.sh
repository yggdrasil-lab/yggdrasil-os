#!/bin/bash
# ==============================================================================
# Apollo Supply - Boot Persistence & Resume Orchestrator
# ==============================================================================
# Restores Gluetun VPN container and waits for healthy status before launching
# dependent download containers (qbittorrent, sabnzbd, slskd).
# ==============================================================================

set -e

VPN_CONTAINER="gluetun"
DEPENDENT_CONTAINERS=("slskd" "qbittorrent" "sabnzbd")

echo "Starting Apollo Supply Resume..."

# 1. Wait for Docker daemon to become responsive
MAX_DOCKER_WAIT=30
DOCKER_COUNT=0
until /usr/bin/docker info >/dev/null 2>&1 || [ "$DOCKER_COUNT" -ge "$MAX_DOCKER_WAIT" ]; do
    echo "Waiting for Docker daemon... (${DOCKER_COUNT}s)"
    sleep 2
    DOCKER_COUNT=$((DOCKER_COUNT + 2))
done

if ! /usr/bin/docker info >/dev/null 2>&1; then
    echo "Error: Docker daemon is unavailable after ${MAX_DOCKER_WAIT}s. Aborting resume."
    exit 1
fi

# If gluetun container does not exist yet (e.g. stack not yet deployed), exit cleanly
if ! /usr/bin/docker inspect "$VPN_CONTAINER" >/dev/null 2>&1; then
    echo "Notice: Container $VPN_CONTAINER does not exist yet. Exiting cleanly."
    exit 0
fi

# 2. Prevent race conditions: if dependent containers were auto-started by dockerd
# on reboot before Gluetun is healthy, pause them
for container in "${DEPENDENT_CONTAINERS[@]}"; do
    if [ "$(/usr/bin/docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null)" = "true" ]; then
        HEALTH_STATUS=$(/usr/bin/docker inspect --format '{{.State.Health.Status}}' "$VPN_CONTAINER" 2>/dev/null || echo "unknown")
        if [ "$HEALTH_STATUS" != "healthy" ]; then
            echo "Stopping prematurely running $container while waiting for $VPN_CONTAINER..."
            /usr/bin/docker stop "$container" 2>/dev/null || true
        fi
    fi
done

# 3. Ensure VPN container is started
if [ "$(/usr/bin/docker inspect --format '{{.State.Running}}' "$VPN_CONTAINER" 2>/dev/null)" != "true" ]; then
    echo "Starting $VPN_CONTAINER..."
    /usr/bin/docker start "$VPN_CONTAINER"
else
    echo "$VPN_CONTAINER is already running."
fi

# 4. Wait for Gluetun to report healthy status
echo "Waiting for $VPN_CONTAINER to report healthy status..."
MAX_RETRIES=60
COUNT=0

while [ "$COUNT" -lt "$MAX_RETRIES" ]; do
    HEALTH_STATUS=$(/usr/bin/docker inspect --format '{{.State.Health.Status}}' "$VPN_CONTAINER" 2>/dev/null || echo "unknown")

    if [ "$HEALTH_STATUS" = "healthy" ]; then
        echo "$VPN_CONTAINER is healthy."
        break
    fi

    if [ "$HEALTH_STATUS" = "unhealthy" ]; then
        echo "Error: $VPN_CONTAINER is unhealthy! Aborting startup of dependent containers."
        exit 1
    fi

    echo "Status: $HEALTH_STATUS. Waiting 2s... ($((COUNT * 2))s elapsed)"
    sleep 2
    COUNT=$((COUNT + 1))
done

if [ "$COUNT" -ge "$MAX_RETRIES" ]; then
    echo "Error: Timed out waiting for $VPN_CONTAINER to become healthy."
    exit 1
fi

# 5. Start Dependent Containers
for container in "${DEPENDENT_CONTAINERS[@]}"; do
    if /usr/bin/docker inspect "$container" >/dev/null 2>&1; then
        echo "Starting $container..."
        /usr/bin/docker start "$container"
    fi
done

echo "Apollo Supply resume complete."

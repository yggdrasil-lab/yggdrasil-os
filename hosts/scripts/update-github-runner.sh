#!/bin/bash
set -e

# ==============================================================================
# Update Self-Hosted GitHub Runner Container
# ==============================================================================
# Discovers local runner compose in /home/raymund/github-runner, checks if a job
# is currently in progress, pulls the latest image, and safely updates the container.
# Idempotent: safe to run repeatedly via systemd timer or manual invocation.
# ==============================================================================

RUNNER_DIR="/home/raymund/github-runner"
COMPOSE_FILE="${RUNNER_DIR}/docker-compose.yml"

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "No github-runner compose configuration found at ${COMPOSE_FILE}. Skipping."
    exit 0
fi

# Determine running container name from compose or fallback
RUNNER_CONTAINER=$(docker compose -f "${COMPOSE_FILE}" ps -q 2>/dev/null || true)

if [ -n "${RUNNER_CONTAINER}" ]; then
    # Check if runner is actively executing a workflow job
    if docker top "${RUNNER_CONTAINER}" 2>/dev/null | grep -q "Runner.Worker"; then
        echo "Runner is currently executing a job. Postponing update to avoid aborting build."
        exit 0
    fi
fi

echo "Pulling latest runner image in ${RUNNER_DIR}..."
docker compose -f "${COMPOSE_FILE}" pull

echo "Applying update to github-runner container..."
docker compose -f "${COMPOSE_FILE}" up -d

echo "Pruning dangling runner images..."
docker image prune -f --filter "label=maintainer=myoung34@my.apsu.edu" || true

echo "GitHub runner update check complete."

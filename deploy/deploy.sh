#!/bin/bash
set -e

# Usage: ./deploy.sh [--skip-build] <STACK_NAME> [COMPOSE_FILES...]
# Example: ./deploy.sh cerberus_dev docker-compose.yml docker-compose.dev.yml

SKIP_BUILD=false
STACK_NAME=""
COMPOSE_FILES=()

# Load environment variables from .env if it exists
if [ -f .env ]; then
    echo "Loading environment variables from .env..."
    set -a
    source .env
    set +a
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-build)
            SKIP_BUILD=true
            shift
            ;;
        *)
            if [ -z "$STACK_NAME" ]; then
                STACK_NAME="$1"
            else
                COMPOSE_FILES+=("$1")
            fi
            shift
            ;;
    esac
done

if [ -z "$STACK_NAME" ]; then
    echo "Usage: $0 [--skip-build] <STACK_NAME> [COMPOSE_FILES...]"
    exit 1
fi

# Default to docker-compose.yml if no files provided
if [ ${#COMPOSE_FILES[@]} -eq 0 ]; then
    COMPOSE_FILES=("docker-compose.yml")
fi

# Construct flag strings
BUILD_ARGS=""
DEPLOY_ARGS=""

for file in "${COMPOSE_FILES[@]}"; do
    BUILD_ARGS="$BUILD_ARGS -f $file"
    DEPLOY_ARGS="$DEPLOY_ARGS -c $file"
done

export STACK_NAME
# Set project name so built images are labeled with the correct stack name
export COMPOSE_PROJECT_NAME="${STACK_NAME}"

echo "Starting deployment for stack: ${STACK_NAME}"
echo "Using compose files: ${COMPOSE_FILES[*]}"

# --- Cleanup ---
echo "Removing existing ${STACK_NAME} stack..."
docker stack rm ${STACK_NAME} || true

echo "Waiting for stack to be removed..."
while docker service ls | grep -q "${STACK_NAME}_"; do
    echo "Stack services still active, waiting..."
    sleep 2
done

echo "Waiting for network to be removed..."
while docker network ls | grep -q "${STACK_NAME}_internal"; do
    echo "Stack network still active, waiting..."
    sleep 2
done

# --- Build ---
if [ "$SKIP_BUILD" = true ]; then
    echo "Skipping build step..."
else
    echo "Building images..."
    # shellcheck disable=SC2086
    docker compose $BUILD_ARGS build --pull
    
    if [ -n "$REGISTRY_PREFIX" ]; then
        echo "Pushing images to registry..."
        docker compose $BUILD_ARGS push
    fi
fi

# --- Deploy ---
echo "Deploying ${STACK_NAME} stack to Swarm..."
# --prune: Remove services that are no longer referenced matched by the compose files
# shellcheck disable=SC2086
docker stack deploy --with-registry-auth --prune $DEPLOY_ARGS "${STACK_NAME}"

echo "Deployment command submitted. Check status with: docker stack services ${STACK_NAME}"

# --- Kanban Auto-Sync ---
# If this deployment corresponds to an autonomous Kanban card (e.g. [KB-XXXX]),
# automatically mark the task done and notify its Discord thread.
if command -v git >/dev/null 2>&1; then
    CANDIDATES=""
    CANDIDATES=$(git log -1 --pretty=%B 2>/dev/null | grep -ioE 'kb-[a-f0-9]{8}' || true)
    if [ -z "$CANDIDATES" ]; then
        CANDIDATES=$(git log -2 --pretty=%B 2>/dev/null | grep -ioE 'kb-[a-f0-9]{8}' || true)
    fi
    if [ -z "$CANDIDATES" ] && command -v gh >/dev/null 2>&1; then
        PR_NUM=$(git log -1 --pretty=%s 2>/dev/null | grep -oE '#[0-9]+' | tr -d '#' | head -n 1 || true)
        if [ -n "$PR_NUM" ]; then
            CANDIDATES=$(gh pr view "$PR_NUM" --json title,body,headRefName --jq '.title + " " + .body + " " + .headRefName' 2>/dev/null | grep -ioE 'kb-[a-f0-9]{8}' || true)
        fi
    fi

    # Deduplicate candidate task IDs
    TASK_IDS=$(echo "$CANDIDATES" | tr '[:upper:]' '[:lower:]' | sort -u | tr '\n' ' ')

    if [ -n "$TASK_IDS" ]; then
        echo "Found candidate Kanban tasks for deployment: ${TASK_IDS}"

        # Resolve Discord webhook credentials if not already exported
        DISCORD_HOOK="${KANBAN_DISCORD_WEBHOOK_URL:-}"
        DISCORD_UID="${DISCORD_USER_ID:-}"
        if [ -z "$DISCORD_HOOK" ] && command -v docker >/dev/null 2>&1; then
            DISCORD_HOOK=$(docker service inspect --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}' odin_huginn-gateway 2>/dev/null | grep '^KANBAN_DISCORD_WEBHOOK_URL=' | cut -d'=' -f2- || true)
        fi
        if [ -z "$DISCORD_UID" ] && command -v docker >/dev/null 2>&1; then
            DISCORD_UID=$(docker service inspect --format '{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}' odin_huginn-gateway 2>/dev/null | grep '^DISCORD_USER_ID=' | cut -d'=' -f2- || true)
        fi

        for TID in $TASK_IDS; do
            if command -v kanban-gateway >/dev/null 2>&1 && [ -f "/opt/odin/hermes/kanban.db" ]; then
                echo "Resolving Kanban card ${TID} via local CLI..."
                KANBAN_DISCORD_WEBHOOK_URL="$DISCORD_HOOK" DISCORD_USER_ID="$DISCORD_UID" kanban-gateway update \
                    --task-id "$TID" \
                    --status done \
                    --message "PR merged to main. Swarm deployment active for ${STACK_NAME}." || true
            elif command -v docker >/dev/null 2>&1; then
                echo "Resolving Kanban card ${TID} via Swarm container..."
                docker run --rm \
                    -v /opt/odin/hermes:/opt/data \
                    -e KANBAN_DISCORD_WEBHOOK_URL="$DISCORD_HOOK" \
                    -e DISCORD_USER_ID="$DISCORD_UID" \
                    --entrypoint python3 \
                    registry.tienzo.net/hermes-agent:odin \
                    /usr/local/bin/kanban_jev_gateway.py update \
                    --task-id "$TID" \
                    --status done \
                    --message "PR merged to main. Swarm deployment active for ${STACK_NAME}." 2>/dev/null || true
            fi
        done
    fi
fi


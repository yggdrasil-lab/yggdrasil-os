#!/bin/bash
set -euo pipefail

# Usage: ./deploy.sh [--skip-build] [--recreate] <STACK_NAME> [COMPOSE_FILES...]
# Example: ./deploy.sh cerberus_dev docker-compose.yml docker-compose.dev.yml

SKIP_BUILD=false
RECREATE=false
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
        --recreate)
            RECREATE=true
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
    echo "Usage: $0 [--skip-build] [--recreate] <STACK_NAME> [COMPOSE_FILES...]"
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

# --- Build ---
if [ "$SKIP_BUILD" = true ]; then
    echo "Skipping build step..."
else
    echo "Building images..."
    # shellcheck disable=SC2086
    docker compose $BUILD_ARGS build --pull
    
    if [ -n "${REGISTRY_PREFIX:-}" ]; then
        echo "Resolving built images to push..."
        # shellcheck disable=SC2086
        COMPOSE_JSON=$(docker compose $BUILD_ARGS config --format json)
        PUSH_IMAGES_RAW=$(printf '%s' "$COMPOSE_JSON" | python3 -c '
import json, sys

data = json.load(sys.stdin)
seen = set()
for svc in (data.get("services") or {}).values():
    if not isinstance(svc, dict) or svc.get("build") is None:
        continue
    image = str(svc.get("image") or "").strip()
    if image and image not in seen:
        seen.add(image)
        print(image)
')
        PUSH_IMAGES=()
        if [ -n "$PUSH_IMAGES_RAW" ]; then
            mapfile -t PUSH_IMAGES <<< "$PUSH_IMAGES_RAW"
        fi

        if [ "${#PUSH_IMAGES[@]}" -eq 0 ]; then
            echo "No built images to push."
        else
            MAX_PUSH_ATTEMPTS=3
            for PUSH_IMAGE_REF in "${PUSH_IMAGES[@]}"; do
                ATTEMPT=1
                while true; do
                    echo "Pushing ${PUSH_IMAGE_REF} (attempt ${ATTEMPT}/${MAX_PUSH_ATTEMPTS})..."
                    if docker push "$PUSH_IMAGE_REF"; then
                        break
                    fi
                    if [ "$ATTEMPT" -ge "$MAX_PUSH_ATTEMPTS" ]; then
                        echo "Error: Failed to push ${PUSH_IMAGE_REF} after ${MAX_PUSH_ATTEMPTS} attempts." >&2
                        exit 1
                    fi
                    BACKOFF_SECONDS=$((ATTEMPT * 5))
                    echo "Warning: Push failed for ${PUSH_IMAGE_REF}; retrying in ${BACKOFF_SECONDS}s..." >&2
                    sleep "$BACKOFF_SECONDS"
                    ATTEMPT=$((ATTEMPT + 1))
                done
            done
        fi
    fi
fi

# --- Stack Teardown Helper ---
remove_stack() {
    echo "Removing existing ${STACK_NAME} stack..."
    docker stack rm "${STACK_NAME}" || true

    echo "Waiting for stack to be removed..."
    local WAIT_SERVICES=0
    local MAX_WAIT=35
    while [ "$WAIT_SERVICES" -lt "$MAX_WAIT" ] && (timeout 5 docker service ls 2>/dev/null || true) | grep -q "${STACK_NAME}_"; do
        echo "Stack services still active, waiting... ($((WAIT_SERVICES + 1))/${MAX_WAIT})"
        sleep 2
        WAIT_SERVICES=$((WAIT_SERVICES + 1))
    done

    echo "Waiting for stack containers to stop..."
    local WAIT_CONTAINERS=0
    while [ "$WAIT_CONTAINERS" -lt "$MAX_WAIT" ] && [ -n "$(timeout 5 docker ps -q --filter "label=com.docker.stack.namespace=${STACK_NAME}" 2>/dev/null || true)" ]; do
        echo "Stack containers still stopping, waiting... ($((WAIT_CONTAINERS + 1))/${MAX_WAIT})"
        sleep 2
        WAIT_CONTAINERS=$((WAIT_CONTAINERS + 1))
    done

    echo "Waiting for network to be removed..."
    local WAIT_NETWORK=0
    while [ "$WAIT_NETWORK" -lt "$MAX_WAIT" ] && (timeout 5 docker network ls 2>/dev/null || true) | grep -q "${STACK_NAME}_internal"; do
        echo "Stack network still active, waiting... ($((WAIT_NETWORK + 1))/${MAX_WAIT})"
        sleep 2
        WAIT_NETWORK=$((WAIT_NETWORK + 1))
    done
}

# --- Deploy ---
# --prune: Remove services that are no longer referenced by the compose files
# --resolve-image always: Resolve mutable tags to the latest registry digest during rolling updates
if [ "$RECREATE" = true ]; then
    remove_stack
    echo "Deploying ${STACK_NAME} stack to Swarm..."
    # shellcheck disable=SC2086
    docker stack deploy --with-registry-auth --prune --resolve-image always $DEPLOY_ARGS "${STACK_NAME}"
else
    echo "Deploying ${STACK_NAME} stack to Swarm (in-place rolling update)..."
    set +e
    # shellcheck disable=SC2086
    DEPLOY_OUTPUT=$(docker stack deploy --with-registry-auth --prune --resolve-image always $DEPLOY_ARGS "${STACK_NAME}" 2>&1)
    DEPLOY_RC=$?
    set -e

    if [ "$DEPLOY_RC" -ne 0 ]; then
        if [ -n "$DEPLOY_OUTPUT" ]; then
            printf '%s\n' "$DEPLOY_OUTPUT" >&2
        fi
        if grep -qiE 'only updates to Labels are allowed|failed to update config|failed to update secret|is not allowed|is not supported|immutable' <<< "$DEPLOY_OUTPUT"; then
            echo "Warning: In-place deploy rejected due to immutable Swarm config/spec change; falling back to stack recreate for ${STACK_NAME}..." >&2
            remove_stack
            echo "Retrying ${STACK_NAME} stack deploy after recreate..."
            # shellcheck disable=SC2086
            docker stack deploy --with-registry-auth --prune --resolve-image always $DEPLOY_ARGS "${STACK_NAME}"
        else
            exit "$DEPLOY_RC"
        fi
    elif [ -n "$DEPLOY_OUTPUT" ]; then
        printf '%s\n' "$DEPLOY_OUTPUT"
    fi
fi

echo "Deployment command submitted. Check status with: docker stack services ${STACK_NAME}"

# --- Prune Unused Secrets ---
echo "Pruning unused versioned Swarm secrets..."
ALL_SECRETS=$(docker secret ls --format '{{.Name}}' | grep -E '_[0-9a-f]{8}$' || true)
if [ -n "$ALL_SECRETS" ]; then
    # shellcheck disable=SC2046
    USED_SECRETS=$(docker service inspect $(docker service ls -q) --format '{{ range .Spec.TaskTemplate.ContainerSpec.Secrets }}{{ .SecretName }} {{ end }}' 2>/dev/null || true)
    for SECRET in $ALL_SECRETS; do
        if echo "$USED_SECRETS" | grep -q "$SECRET"; then
            echo "[KEEP] $SECRET (In use)"
        else
            echo "[DELETE] $SECRET"
            docker secret rm "$SECRET" || echo "Failed to delete $SECRET"
        fi
    done
fi


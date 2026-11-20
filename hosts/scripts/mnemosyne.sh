#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Yggdrasil OS - Host Mnemosyne CLI Wrapper
# ==============================================================================
# Executes the Mnemosyne CLI inside the running odin_huginn-gateway Swarm
# container, retrying briefly across rolling Swarm updates and failing loudly
# when no running container is available.
# ==============================================================================

DOCKER_BIN="${DOCKER_BIN:-docker}"
CONTAINER_NAME="${CONTAINER_NAME:-odin_huginn-gateway}"
WAIT_RETRIES="${MNEMOSYNE_WAIT_RETRIES:-5}"
WAIT_SLEEP="${MNEMOSYNE_WAIT_SLEEP:-2}"

find_running_container() {
    local output=""
    if output="$(timeout 5 "${DOCKER_BIN}" ps -q -f "name=${CONTAINER_NAME}" -f status=running 2>/dev/null)"; then
        if [ -n "${output}" ]; then
            printf '%s\n' "${output}" | head -n 1
        fi
    fi
}

CONTAINER=""
attempt=0
while true; do
    CONTAINER="$(find_running_container)"
    if [ -n "${CONTAINER}" ]; then
        break
    fi
    if [ "${attempt}" -ge "${WAIT_RETRIES}" ]; then
        break
    fi
    attempt=$((attempt + 1))
    sleep "${WAIT_SLEEP}"
done

if [ -z "${CONTAINER}" ]; then
    echo "Error: no running ${CONTAINER_NAME} container found after $((attempt + 1)) attempt(s)." >&2
    echo "Check Swarm service status: timeout 5 docker service ps ${CONTAINER_NAME}" >&2
    exit 1
fi

if [ -t 0 ] && [ -t 1 ]; then
    exec "${DOCKER_BIN}" exec -it "${CONTAINER}" /opt/hermes/.venv/bin/mnemosyne "$@"
else
    exec "${DOCKER_BIN}" exec -i "${CONTAINER}" /opt/hermes/.venv/bin/mnemosyne "$@"
fi

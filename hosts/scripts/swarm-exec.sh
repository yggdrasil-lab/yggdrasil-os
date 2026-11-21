#!/bin/bash
# ==============================================================================
# Swarm Service Exec Helper (swarm-exec)
# ==============================================================================
# Safely resolves the running Docker container for a Swarm service on the
# local host and executes commands directly without nested subshells.
# ==============================================================================

set -e

if [ $# -lt 2 ]; then
    echo "Usage: swarm-exec <service_pattern> [--] <command...>"
    echo ""
    echo "Examples:"
    echo "  swarm-exec dumper ls -la /backups"
    echo "  swarm-exec iris-db -- psql -U iris"
    echo "  swarm-exec hermes python3 -c 'print(1)'"
    echo "  swarm-exec eir-api sh"
    exit 1
fi

PATTERN="$1"
shift

if [ "${1:-}" = "--" ]; then
    shift
fi

if [ $# -lt 1 ]; then
    echo "Usage: swarm-exec <service_pattern> [--] <command...>" >&2
    exit 1
fi

# Query running containers matching pattern
MATCHES=$(docker ps --filter "name=${PATTERN}" --format "{{.ID}} {{.Names}}" 2>/dev/null)

if [ -z "$MATCHES" ]; then
    echo "ERROR: No active container found matching '${PATTERN}' on this host." >&2
    exit 1
fi

COUNT=$(echo "$MATCHES" | wc -l)
if [ "$COUNT" -gt 1 ]; then
    CID=$(echo "$MATCHES" | head -n 1 | awk '{print $1}')
    CNAME=$(echo "$MATCHES" | head -n 1 | awk '{print $2}')
    echo "[INFO] Multiple containers matched '${PATTERN}'. Targeting newest: ${CNAME} (${CID})" >&2
else
    CID=$(echo "$MATCHES" | awk '{print $1}')
fi

if [ -t 0 ] && [ -t 1 ]; then
    exec docker exec -it "$CID" "$@"
elif [ ! -t 0 ]; then
    exec docker exec -i "$CID" "$@"
else
    exec docker exec "$CID" "$@"
fi

#!/bin/bash
# ==============================================================================
# Swarm Overlay Network Endpoint Pruner (prune-network-endpoints)
# ==============================================================================
# Audits Docker Swarm overlay networks (e.g. aether-net) for leaked, stale,
# or orphaned endpoints and force-disconnects them to reclaim exhausted IPAM
# allocations without restarting the Docker daemon.
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." >/dev/null 2>&1 && pwd)"

PY_SCRIPT="${REPO_ROOT}/network/prune_network_endpoints.py"

# If running standalone from /usr/local/bin, fallback to canonical path
if [ ! -f "$PY_SCRIPT" ]; then
    PY_SCRIPT="/usr/local/share/yggdrasil-os/network/prune_network_endpoints.py"
fi

if [ ! -f "$PY_SCRIPT" ]; then
    # Direct execution fallback from raw repo or local clone
    if [ -f "/opt/atlas/vault/Forge/yggdrasil-os/network/prune_network_endpoints.py" ]; then
        PY_SCRIPT="/opt/atlas/vault/Forge/yggdrasil-os/network/prune_network_endpoints.py"
    fi
fi

if [ -f "$PY_SCRIPT" ]; then
    exec python3 "$PY_SCRIPT" "$@"
else
    echo "ERROR: prune_network_endpoints.py not found." >&2
    exit 1
fi

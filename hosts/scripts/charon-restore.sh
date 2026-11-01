#!/bin/bash
# ==============================================================================
# Charon Host Restore CLI
# ==============================================================================
# Turnkey wrapper that executes the Charon restore utility inside the local
# running charon-dumper container on Gaia or Muspelheim.
# ==============================================================================

set -e

# Find running charon-dumper container on the local node
CID=$(docker ps -q --filter "label=com.docker.compose.project=charon" --filter "name=charon-dumper" 2>/dev/null | head -n 1)

if [ -z "$CID" ]; then
    echo "ERROR: No active charon-dumper container found running on this host."
    echo "Ensure the charon stack is deployed and running (docker service ps charon_charon-dumper-...)."
    exit 1
fi

# Pass interactive flags if run from terminal
if [ -t 0 ]; then
    DOCKER_FLAGS="-it"
else
    DOCKER_FLAGS="-i"
fi

exec docker exec ${DOCKER_FLAGS} "$CID" /usr/local/bin/restore.sh "$@"

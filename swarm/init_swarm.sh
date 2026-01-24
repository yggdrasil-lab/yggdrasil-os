#!/bin/bash
# scripts/init_swarm.sh

# Initializes Docker Swarm or outputs the join token if already initialized.
# Usage: ./init_swarm.sh [manager|worker]

set -e

ROLE=${1:-manager}

# Check if Swarm is already active
if [ "$(docker info --format '{{.Swarm.LocalNodeState}}')" == "active" ]; then
    echo "Swarm is already active."
    echo "Manager Join Token:"
    docker swarm join-token manager -q
    echo "Worker Join Token:"
    docker swarm join-token worker -q
    exit 0
fi

if [ "$ROLE" == "manager" ]; then
    echo "Initializing new Swarm Cluster..."
    # Auto-detect primary interface IP (generic approach)
    ADVERTISE_ADDR=$(hostname -I | awk '{print $1}')
    docker swarm init --advertise-addr "$ADVERTISE_ADDR"
    
    echo "Swarm initialized!"
    echo "Manager Join Token:"
    docker swarm join-token manager -q
    echo "Worker Join Token:"
    docker swarm join-token worker -q
else
    echo "To join as a worker, run the join command on this node."
    echo "Get the token from an existing Manager node: 'docker swarm join-token worker'"
fi

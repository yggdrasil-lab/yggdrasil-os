#!/bin/bash
# scripts/create_network.sh

# Creates the 'aether-net' encryption-enabled overlay network.
# This is the primary network for inter-stack communication (Olympus <-> Charon <-> ...).

NETWORK_NAME="aether-net"

if docker network ls --format '{{.Name}}' | grep -q "^${NETWORK_NAME}$"; then
    echo "Network '${NETWORK_NAME}' already exists."
else
    echo "Creating overlay network '${NETWORK_NAME}'..."
    docker network create \
      --driver overlay \
      --attachable \
      --opt encrypted \
      "${NETWORK_NAME}"
    echo "Network created successfully."
fi

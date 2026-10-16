#!/bin/bash
set -e

# Usage: ./prune_secrets.sh <SECRET_BASE_NAME>
# Example: ./prune_secrets.sh "cerberus_admin_token"

SECRET_BASE_NAME="$1"

if [ -z "$SECRET_BASE_NAME" ]; then
    echo "Error: Secret base name is required." >&2
    exit 1
fi

echo "Pruning secrets with prefix: ${SECRET_BASE_NAME}_"

# 1. Get List of all secrets matching the prefix
# We look for ${SECRET_BASE_NAME}_ followed by anything
ALL_SECRETS=$(docker secret ls --format '{{.Name}}' | grep "^${SECRET_BASE_NAME}_")

if [ -z "$ALL_SECRETS" ]; then
    echo "No secrets found with prefix ${SECRET_BASE_NAME}_"
    exit 0
fi

# 2. Get List of secrets currently in use by any service
# We iterate over all services to find used secrets.
# Note: This checks the whole swarm, which is safer.
USED_SECRETS=$(docker service inspect $(docker service ls -q) --format '{{ range .Spec.TaskTemplate.ContainerSpec.Secrets }}{{ .SecretName }} {{ end }}' 2>/dev/null)

# 3. Iterate and Delete
# We can't delete a secret if it's in use anyway (Docker will error), 
# but checking first is cleaner and less noisy.

for SECRET in $ALL_SECRETS; do
    if echo "$USED_SECRETS" | grep -q "$SECRET"; then
        echo "[KEEP] $SECRET (In use)"
    else
        echo "[DELETE] $SECRET"
        docker secret rm "$SECRET" || echo "Failed to delete $SECRET"
    fi
done

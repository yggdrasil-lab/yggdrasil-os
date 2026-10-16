#!/bin/bash
set -e

# Usage: echo "secret_content" | ./ensure_secret.sh <SECRET_BASE_NAME>
# Example: echo "super_secret" | ./ensure_secret.sh "cerberus_admin_token"
# Output: cerberus_admin_token_<hash>

SECRET_BASE_NAME="$1"

if [ -z "$SECRET_BASE_NAME" ]; then
    echo "Error: Secret base name is required." >&2
    echo "Usage: echo \"content\" | ./ensure_secret.sh <SECRET_BASE_NAME>" >&2
    exit 1
fi

# Read secret content from stdin
SECRET_CONTENT=$(cat)

if [ -z "$SECRET_CONTENT" ]; then
    echo "Error: Secret content must be provided via stdin." >&2
    exit 1
fi

# Calculate MD5 hash of the content (first 8 chars)
# standardizing on md5sum (Linux) or md5 (BSD/Mac)
if command -v md5sum >/dev/null 2>&1; then
    HASH=$(echo -n "$SECRET_CONTENT" | md5sum | cut -d' ' -f1 | head -c 8)
elif command -v md5 >/dev/null 2>&1; then
    HASH=$(echo -n "$SECRET_CONTENT" | md5 | head -c 8)
else
    echo "Error: neither md5sum nor md5 found." >&2
    exit 1
fi

FULL_SECRET_NAME="${SECRET_BASE_NAME}_${HASH}"

# Check if secret already exists
if docker secret inspect "$FULL_SECRET_NAME" >/dev/null 2>&1; then
    # Secret exists, nothing to do
    :
else
    # Create the secret
    printf "%s" "$SECRET_CONTENT" | docker secret create "$FULL_SECRET_NAME" - >/dev/null
fi

# Output only the name to stdout so it can be captured
echo "$FULL_SECRET_NAME"

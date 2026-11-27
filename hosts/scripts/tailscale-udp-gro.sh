#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Yggdrasil OS - Enable UDP GRO Forwarding for Tailscale Subnet Routing
# ==============================================================================

if ! command -v ip >/dev/null 2>&1; then
    echo "Error: ip command not found." >&2
    exit 1
fi

if ! command -v ethtool >/dev/null 2>&1; then
    echo "Error: ethtool command not found." >&2
    exit 1
fi

NETDEV="$(ip -o route show default | awk '{for (i=1; i<=NF; i++) if ($i=="dev") {print $(i+1); exit}}')"
if [ -z "$NETDEV" ]; then
    echo "Error: Could not determine default network interface." >&2
    exit 1
fi

ethtool -K "$NETDEV" rx-udp-gro-forwarding on rx-gro-list off

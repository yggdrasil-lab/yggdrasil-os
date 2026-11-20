#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Yggdrasil OS - Idempotent Local Registry /etc/hosts Pinning Helper
# ==============================================================================
# Pins the private container registry hostname (registry.<domain>) directly to
# the Swarm manager LAN IPv4 address in /etc/hosts so local `docker push` and
# Swarm worker `docker pull` traffic stays on the LAN instead of traversing the
# public Cloudflare Tunnel (which enforces a 100MB upload limit / 413 error).
#
# Optional environment overrides:
#   HOSTS_FILE       Target hosts file path (default: /etc/hosts)
#   REGISTRY_HOST    Explicit registry hostname (e.g. registry.example.com)
#   DOMAIN_NAME      Base domain used to build registry.<DOMAIN_NAME>
#   REGISTRY_LAN_IP  Explicit IPv4 address for the registry host
#   NODE_ROLE        Swarm node role: manager (default) or worker
# ==============================================================================

HOSTS_FILE="${HOSTS_FILE:-/etc/hosts}"
REGISTRY_HOST="${REGISTRY_HOST:-}"
DOMAIN_NAME="${DOMAIN_NAME:-}"
REGISTRY_LAN_IP="${REGISTRY_LAN_IP:-}"
NODE_ROLE="${NODE_ROLE:-manager}"

usage_hint() {
    cat >&2 <<'EOF'
Override examples:
  DOMAIN_NAME=<domain> NODE_ROLE=manager pin-registry-host
  REGISTRY_HOST=registry.<domain> REGISTRY_LAN_IP=<lan-ipv4> pin-registry-host
EOF
}

is_valid_ipv4() {
    local ip="$1"
    local IFS='.'
    local -a octets=()
    local o

    [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    read -r -a octets <<< "$ip"
    [ "${#octets[@]}" -eq 4 ] || return 1

    for o in "${octets[@]}"; do
        if [[ "$o" =~ ^0[0-9]+$ ]]; then
            return 1
        fi
        if [ "$((10#$o))" -gt 255 ]; then
            return 1
        fi
    done

    if [ "$ip" = "0.0.0.0" ] || [ "$ip" = "255.255.255.255" ]; then
        return 1
    fi
    return 0
}

is_valid_hostname() {
    local host="$1"
    [ -n "$host" ] || return 1
    [ "${#host}" -le 253 ] || return 1
    if [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
        return 1
    fi
    [[ "$host" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]]
}

if [ "$NODE_ROLE" != "manager" ] && [ "$NODE_ROLE" != "worker" ]; then
    echo "Error: Invalid NODE_ROLE '${NODE_ROLE}'. Expected 'manager' or 'worker'." >&2
    usage_hint
    exit 1
fi

# 1. Resolve REGISTRY_HOST: REGISTRY_HOST -> registry.DOMAIN_NAME -> auto-detected domain
if [ -z "$REGISTRY_HOST" ]; then
    if [ -n "$DOMAIN_NAME" ]; then
        REGISTRY_HOST="registry.${DOMAIN_NAME}"
    else
        DETECTED_DOMAIN=""
        DETECTED_DOMAIN="$(hostname -d 2>/dev/null | tr -d '[:space:]' || true)"
        if [ "$DETECTED_DOMAIN" = "(none)" ]; then
            DETECTED_DOMAIN=""
        fi

        if [ -z "$DETECTED_DOMAIN" ]; then
            FQDN="$(hostname -f 2>/dev/null | tr -d '[:space:]' || true)"
            if [ -n "$FQDN" ] && [ "$FQDN" != "(none)" ] && [[ "$FQDN" == *.* ]]; then
                DETECTED_DOMAIN="${FQDN#*.}"
            fi
        fi

        if [ -z "$DETECTED_DOMAIN" ] && [ -f "$HOSTS_FILE" ]; then
            DETECTED_DOMAIN="$(awk '{sub(/#.*/, ""); for (i=2; i<=NF; i++) if ($i ~ /^gaia\.[A-Za-z0-9.-]+$/) { sub(/^gaia\./, "", $i); print $i; exit }}' "$HOSTS_FILE" 2>/dev/null || true)"
        fi

        DETECTED_DOMAIN="${DETECTED_DOMAIN%.}"
        if [ -n "$DETECTED_DOMAIN" ]; then
            REGISTRY_HOST="registry.${DETECTED_DOMAIN}"
        fi
    fi
fi

if ! is_valid_hostname "$REGISTRY_HOST"; then
    echo "Error: Could not resolve a valid REGISTRY_HOST (got: '${REGISTRY_HOST:-<empty>}')." >&2
    echo "Ensure 'hostname -d' returns a domain or set DOMAIN_NAME / REGISTRY_HOST explicitly." >&2
    usage_hint
    exit 1
fi

# 2. Resolve REGISTRY_LAN_IP: REGISTRY_LAN_IP -> manager route/hostname-I or worker Swarm manager addr
if [ -z "$REGISTRY_LAN_IP" ]; then
    if [ "$NODE_ROLE" = "manager" ]; then
        REGISTRY_LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' || true)"
        if [ -z "$REGISTRY_LAN_IP" ]; then
            REGISTRY_LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
        fi
    else
        if command -v timeout >/dev/null 2>&1; then
            REGISTRY_LAN_IP="$(timeout 5 docker info --format '{{(index .Swarm.RemoteManagers 0).Addr}}' 2>/dev/null | cut -d: -f1 || true)"
        else
            REGISTRY_LAN_IP="$(docker info --format '{{(index .Swarm.RemoteManagers 0).Addr}}' 2>/dev/null | cut -d: -f1 || true)"
        fi
    fi
fi

if ! is_valid_ipv4 "$REGISTRY_LAN_IP"; then
    echo "Error: Could not resolve a valid IPv4 REGISTRY_LAN_IP for NODE_ROLE=${NODE_ROLE} (got: '${REGISTRY_LAN_IP:-<empty>}')." >&2
    if [ "$NODE_ROLE" = "worker" ]; then
        echo "Ensure this node has joined the Swarm or pass REGISTRY_LAN_IP=<manager-lan-ipv4>." >&2
    else
        echo "Ensure the primary LAN route is up or pass REGISTRY_LAN_IP=<manager-lan-ipv4>." >&2
    fi
    usage_hint
    exit 1
fi

# 3. Validate target hosts file path and write permissions before editing
HOSTS_DIR="$(dirname "$HOSTS_FILE")"
if [ ! -d "$HOSTS_DIR" ]; then
    echo "Error: Parent directory for HOSTS_FILE does not exist: ${HOSTS_DIR}" >&2
    exit 1
fi
if [ -e "$HOSTS_FILE" ] && [ ! -w "$HOSTS_FILE" ]; then
    echo "Error: HOSTS_FILE is not writable: ${HOSTS_FILE}. Run with sudo or set HOSTS_FILE." >&2
    exit 1
fi
if [ ! -w "$HOSTS_DIR" ]; then
    echo "Error: Directory is not writable for atomic update: ${HOSTS_DIR}. Run with sudo or set HOSTS_FILE." >&2
    exit 1
fi

TMP_HOSTS="$(mktemp "${HOSTS_DIR}/.pin-registry-host.XXXXXX")"
trap 'rm -f "$TMP_HOSTS"' EXIT

INPUT_HOSTS="/dev/null"
if [ -f "$HOSTS_FILE" ]; then
    INPUT_HOSTS="$HOSTS_FILE"
fi

awk -v target_ip="$REGISTRY_LAN_IP" -v target_host="$REGISTRY_HOST" '
BEGIN {
    pin_marker = "# yggdrasil-registry-pin"
    pin_line = target_ip " " target_host " " pin_marker
    emitted = 0
    target_lower = tolower(target_host)
}
{
    line = $0
    sub(/\r$/, "", line)

    if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) {
        print line
        next
    }

    if (index(line, pin_marker) > 0) {
        if (!emitted) {
            print pin_line
            emitted = 1
        }
        next
    }

    comment = ""
    body = line
    cpos = index(line, "#")
    if (cpos > 0) {
        body = substr(line, 1, cpos - 1)
        comment = substr(line, cpos)
    }

    n = split(body, fields, /[[:space:]]+/)
    ip_tok = ""
    kept_count = 0
    matched = 0
    for (i = 1; i <= n; i++) {
        if (fields[i] == "") {
            continue
        }
        if (ip_tok == "") {
            ip_tok = fields[i]
        } else if (tolower(fields[i]) == target_lower) {
            matched = 1
        } else {
            kept_count++
            kept[kept_count] = fields[i]
        }
    }

    if (!matched) {
        print line
        next
    }

    if (kept_count == 0) {
        if (!emitted) {
            print pin_line
            emitted = 1
        }
    } else {
        out = ip_tok
        for (i = 1; i <= kept_count; i++) {
            out = out " " kept[i]
        }
        if (comment != "") {
            out = out " " comment
        }
        print out
    }
}
END {
    if (!emitted) {
        print pin_line
    }
}
' "$INPUT_HOSTS" > "$TMP_HOSTS"

chmod 0644 "$TMP_HOSTS"

if [ -f "$HOSTS_FILE" ] && cmp -s "$TMP_HOSTS" "$HOSTS_FILE"; then
    chmod 0644 "$HOSTS_FILE"
    echo "Registry pin already up to date in ${HOSTS_FILE}: ${REGISTRY_LAN_IP} ${REGISTRY_HOST}"
else
    mv -f "$TMP_HOSTS" "$HOSTS_FILE"
    echo "Pinned ${REGISTRY_HOST} -> ${REGISTRY_LAN_IP} in ${HOSTS_FILE}"
fi

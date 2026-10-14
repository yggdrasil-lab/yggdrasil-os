#!/bin/bash
# swarm/startup_orchestrator.sh
# Autonomous Swarm startup, health convergence, and container cleanup orchestrator for Gaia host.
# Handles post-boot power restoration recovery and post-restart Swarm convergence.

set -e

# Self-update if running from /usr/local/bin/gaia-swarm-startup.sh
if [ "$0" = "/usr/local/bin/gaia-swarm-startup.sh" ]; then
    UPDATE_URL="https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/swarm/startup_orchestrator.sh"
    TMP_SCRIPT="/tmp/gaia-swarm-startup-update.sh"
    if curl -fsSL --connect-timeout 5 "$UPDATE_URL" -o "$TMP_SCRIPT" 2>/dev/null; then
        if ! cmp -s "$TMP_SCRIPT" "$0"; then
            echo "Updating $0 to latest version from GitHub..."
            mv "$TMP_SCRIPT" "$0"
            chmod 0755 "$0"
            exec "$0" "$@"
        fi
        rm -f "$TMP_SCRIPT"
    fi
fi

# Ensure the caller has docker permissions (either root or docker group member)
if [ "$(id -u)" -ne 0 ] && ! docker info >/dev/null 2>&1; then
    echo "Error: This script must be run as root (sudo) or by a member of the 'docker' group."
    exit 1
fi

echo "=== [1/5] Checking Docker Daemon Availability ==="
MAX_WAIT=60
WAITED=0
until docker info >/dev/null 2>&1 || [ "$WAITED" -ge "$MAX_WAIT" ]; do
    echo "Waiting for Docker daemon to respond... (${WAITED}s/${MAX_WAIT}s)"
    sleep 3
    WAITED=$((WAITED + 3))
done

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon did not respond within ${MAX_WAIT} seconds."
    exit 1
fi
echo "Docker daemon is online and responsive."

echo "=== [2/5] Optimizing Swarm Task History Retention ==="
# Retain at most 1 previous task per service slot instead of default 5
# Prevents accumulation of dead/exited containers after restarts
docker swarm update --task-history-limit 1 >/dev/null 2>&1 || true
echo "Swarm task history retention limit set to 1."

echo "=== [3/5] Verifying Core Infrastructure (DNS, Proxy, Registry) ==="
# Core services that provide image resolution and ingress routing:
CORE_SERVICES=("poseidon_adguardhome" "olympus_traefik" "hephaestus_registry")
for svc in "${CORE_SERVICES[@]}"; do
    if docker service inspect "$svc" >/dev/null 2>&1; then
        echo -n "Checking $svc... "
        CORE_WAITED=0
        while [ "$CORE_WAITED" -lt 30 ]; do
            replicas=$(docker service ls --format '{{.Name}} {{.Replicas}}' | awk -v s="$svc" '$1 == s {print $2}')
            actual="${replicas%/*}"
            target="${replicas#*/}"
            if [ -n "$actual" ] && [ -n "$target" ] && [ "$actual" -ge "$target" ] && [ "$actual" -gt 0 ]; then
                echo "ready ($replicas)"
                break
            fi
            sleep 2
            CORE_WAITED=$((CORE_WAITED + 2))
        done
        if [ "$CORE_WAITED" -ge 30 ]; then
            echo "warning: not fully ready after 30s (continuing)"
        fi
    fi
done

echo "=== [4/5] Monitoring Swarm Service Convergence & Auto-Healing ==="
MAX_CONVERGE_WAIT=120
START_TIME=$(date +%s)

while true; do
    CURRENT_TIME=$(date +%s)
    ELAPSED=$((CURRENT_TIME - START_TIME))

    STILL_CONVERGING=()
    STUCK_SERVICES=()

    while IFS=$'\t' read -r name replicas; do
        [ -z "$name" ] && continue
        actual="${replicas%/*}"
        target="${replicas#*/}"

        # Only evaluate services where running < desired replicas
        if [ -n "$actual" ] && [ -n "$target" ] && [ "$target" -gt 0 ] && [ "$actual" -lt "$target" ]; then
            # Verify if Swarm actually desires running instances (ignores completed batch tasks)
            has_running_desired=$(docker service ps "$name" --filter "desired-state=running" -q 2>/dev/null)
            if [ -n "$has_running_desired" ]; then
                STILL_CONVERGING+=("$name")

                # Detect if container failed during cold boot because local registry was not up yet
                latest_err=$(docker service ps "$name" --no-trunc --format '{{.Error}}' 2>/dev/null | head -n 1)
                if echo "$latest_err" | grep -qiE "No such image|registry|network"; then
                    STUCK_SERVICES+=("$name")
                fi
            fi
        fi
    done < <(docker service ls --format '{{.Name}}\t{{.Replicas}}')

    if [ ${#STILL_CONVERGING[@]} -eq 0 ]; then
        echo "All active Swarm services converged successfully!"
        break
    fi

    # Kick services that entered backoff due to early registry unavailability
    if [ ${#STUCK_SERVICES[@]} -gt 0 ]; then
        for stuck_svc in "${STUCK_SERVICES[@]}"; do
            echo "Auto-healing $stuck_svc (kicking out of cold-boot registry backoff)..."
            docker service update --force "$stuck_svc" >/dev/null 2>&1 || true
        done
    fi

    if [ "$ELAPSED" -ge "$MAX_CONVERGE_WAIT" ]; then
        echo "Notice: Convergence window reached (${MAX_CONVERGE_WAIT}s). Proceeding to cleanup."
        break
    fi

    echo "Converging: ${#STILL_CONVERGING[@]} services pending (${STILL_CONVERGING[*]})... waiting (${ELAPSED}s/${MAX_CONVERGE_WAIT}s)"
    sleep 5
done

echo "=== [5/5] Pruning Stale/Dead Containers & Restoring Clean State ==="
# Ensure standalone containers like gaia-runner are up
if docker ps -a --format '{{.Names}}' | grep -q "^gaia-runner$"; then
    RUNNER_STATE=$(docker inspect -f '{{.State.Running}}' gaia-runner 2>/dev/null || echo "false")
    if [ "$RUNNER_STATE" != "true" ]; then
        echo "Starting standalone runner: gaia-runner..."
        docker start gaia-runner >/dev/null 2>&1 || true
    fi
fi

# Prune dead and exited task containers left behind by restart or power failure
STOPPED_COUNT=$(docker ps -a --filter "status=exited" --filter "status=dead" -q | wc -l)
if [ "$STOPPED_COUNT" -gt 0 ]; then
    echo "Pruning $STOPPED_COUNT stopped/dead container shells..."
    docker container prune -f >/dev/null 2>&1 || true
    DEAD_IDS=$(docker ps -a --filter "status=dead" -q)
    if [ -n "$DEAD_IDS" ]; then
        docker rm -f $DEAD_IDS >/dev/null 2>&1 || true
    fi
    echo "Dead container shells cleared."
else
    echo "No dead/stopped containers found."
fi

echo ""
echo "======================================================"
echo " Gaia Swarm Startup & Health Summary"
echo "======================================================"
docker node ls
echo ""
RUNNING_COUNT=$(docker ps -q --filter "status=running" | wc -l)
REMAINING_STOPPED=$(docker ps -a --filter "status=exited" --filter "status=dead" -q | wc -l)
echo "Active Running Containers: $RUNNING_COUNT"
echo "Stopped/Dead Containers:   $REMAINING_STOPPED"
echo "======================================================"
echo "Success: Gaia Swarm is operational and clean."

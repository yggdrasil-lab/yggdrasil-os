#!/bin/bash
set -euo pipefail

# Usage: ./verify_convergence.sh [--timeout <sec>] [--poll <sec>] [--dwell-checks <n>] [--dwell-interval <sec>] [--log-tail <n>] <STACK_NAME> <SERVICE_1> [SERVICE_2 ...]
# Verifies Docker Swarm post-deploy convergence (N/N replicas with N >= 1) and
# task-ID stability across a configurable dwell window, dumping diagnostics on failure.

CONVERGENCE_TIMEOUT="${CONVERGENCE_TIMEOUT:-120}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
DWELL_CHECKS="${DWELL_CHECKS:-3}"
DWELL_INTERVAL="${DWELL_INTERVAL:-10}"
LOG_TAIL="${LOG_TAIL:-80}"

STACK_NAME=""
SERVICES=()
BASELINE_COUNTS=()
BASELINE_IDS=()
CANDIDATE_COUNTS=()
CANDIDATE_IDS=()

UPDATE_STATUS_RESULT=""
CONVERGED_REPLICAS=""
INSPECTED_TASK_IDS=""
INSPECTED_FAIL_REASON=""

print_usage() {
    cat <<'EOF'
Usage: verify_convergence.sh [--timeout <sec>] [--poll <sec>] [--dwell-checks <n>] [--dwell-interval <sec>] [--log-tail <n>] <STACK_NAME> <SERVICE_1> [SERVICE_2 ...]

Options (also configurable via env vars CONVERGENCE_TIMEOUT, POLL_INTERVAL, DWELL_CHECKS, DWELL_INTERVAL, LOG_TAIL):
  --timeout <sec>         Convergence timeout in seconds (default: 120)
  --poll <sec>            Poll interval in seconds (default: 5)
  --dwell-checks <n>      Number of stability dwell checks after convergence (default: 3)
  --dwell-interval <sec>  Seconds between stability dwell checks (default: 10)
  --log-tail <n>          Number of service log lines to dump on failure (default: 80)
  -h, --help              Show this help message and exit
EOF
}

die() {
    echo "Error: $*" >&2
    exit 1
}

require_opt_arg() {
    local argc="$1"
    local flag="$2"
    local val="${3:-}"
    if [ "$argc" -lt 2 ] || [ -z "$val" ] || [[ "$val" == --* ]]; then
        die "Option '${flag}' requires a non-empty value."
    fi
}

validate_positive_int() {
    local name="$1"
    local val="$2"
    if ! [[ "$val" =~ ^[0-9]+$ ]] || [ "$((10#$val))" -lt 1 ]; then
        die "Invalid integer for ${name}: '${val}' (expected integer >= 1)."
    fi
}

assign_timing_flag() {
    case "$1" in
        --timeout) CONVERGENCE_TIMEOUT="$2" ;;
        --poll) POLL_INTERVAL="$2" ;;
        --dwell-checks) DWELL_CHECKS="$2" ;;
        --dwell-interval) DWELL_INTERVAL="$2" ;;
        --log-tail) LOG_TAIL="$2" ;;
        *) return 1 ;;
    esac
}

validate_timing_config() {
    validate_positive_int "CONVERGENCE_TIMEOUT" "$CONVERGENCE_TIMEOUT"
    validate_positive_int "POLL_INTERVAL" "$POLL_INTERVAL"
    validate_positive_int "DWELL_CHECKS" "$DWELL_CHECKS"
    validate_positive_int "DWELL_INTERVAL" "$DWELL_INTERVAL"
    validate_positive_int "LOG_TAIL" "$LOG_TAIL"

    CONVERGENCE_TIMEOUT="$((10#$CONVERGENCE_TIMEOUT))"
    POLL_INTERVAL="$((10#$POLL_INTERVAL))"
    DWELL_CHECKS="$((10#$DWELL_CHECKS))"
    DWELL_INTERVAL="$((10#$DWELL_INTERVAL))"
    LOG_TAIL="$((10#$LOG_TAIL))"
}

normalize_services() {
    local raw_svc normalized existing dup
    for raw_svc in "$@"; do
        [ -n "$raw_svc" ] || die "Service name must not be empty."
        if [[ "$raw_svc" == "${STACK_NAME}_"* ]]; then
            normalized="$raw_svc"
        else
            normalized="${STACK_NAME}_${raw_svc}"
        fi
        dup=0
        if [ "${#SERVICES[@]}" -gt 0 ]; then
            for existing in "${SERVICES[@]}"; do
                if [ "$existing" = "$normalized" ]; then
                    dup=1
                    break
                fi
            done
        fi
        if [ "$dup" -eq 0 ]; then
            SERVICES+=("$normalized")
        fi
    done
}

parse_args() {
    local positional=() opt val
    while [ "$#" -gt 0 ]; do
        case "$1" in
            -h|--help)
                print_usage
                exit 0
                ;;
            --timeout=*|--poll=*|--dwell-checks=*|--dwell-interval=*|--log-tail=*)
                opt="${1%%=*}"
                val="${1#*=}"
                [ -n "$val" ] || die "Option '${opt}' requires a non-empty value."
                assign_timing_flag "$opt" "$val"
                shift
                ;;
            --timeout|--poll|--dwell-checks|--dwell-interval|--log-tail)
                require_opt_arg "$#" "$1" "${2:-}"
                assign_timing_flag "$1" "$2"
                shift 2
                ;;
            --)
                shift
                if [ "$#" -gt 0 ]; then
                    positional+=("$@")
                fi
                break
                ;;
            -*)
                echo "Error: Unknown option '$1'." >&2
                print_usage >&2
                exit 1
                ;;
            *)
                positional+=("$1")
                shift
                ;;
        esac
    done

    validate_timing_config
    if [ "${#positional[@]}" -lt 2 ]; then
        echo "Error: <STACK_NAME> and at least one <SERVICE> argument are required." >&2
        print_usage >&2
        exit 1
    fi

    STACK_NAME="${positional[0]}"
    [ -n "$STACK_NAME" ] || die "<STACK_NAME> must not be empty."
    normalize_services "${positional[@]:1}"
}

dump_diagnostics() {
    local diag_svc
    echo "=== Post-Deploy Swarm Diagnostics (${STACK_NAME}) ===" >&2
    timeout 10 docker service ls --filter "name=${STACK_NAME}_" >&2 || true
    for diag_svc in "${SERVICES[@]}"; do
        echo "--- docker service ps --no-trunc ${diag_svc} ---" >&2
        timeout 10 docker service ps "$diag_svc" --no-trunc >&2 || true
        echo "--- docker service logs --tail ${LOG_TAIL} ${diag_svc} ---" >&2
        timeout 15 docker service logs --tail "$LOG_TAIL" "$diag_svc" >&2 || true
    done
}

check_update_state() {
    local svc="$1"
    local state
    if ! state=$(timeout 10 docker service inspect "$svc" --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' 2>&1); then
        echo "Warning: docker service inspect failed for ${svc}: ${state}" >&2
        UPDATE_STATUS_RESULT="transient_error"
        return 0
    fi
    state="${state//[[:space:]]/}"
    case "$state" in
        paused|rollback_*)
            echo "::error::Service ${svc} entered abnormal Swarm update state '${state}'." >&2
            dump_diagnostics
            exit 1
            ;;
        updating)
            echo "Warning: Service ${svc} update state is 'updating'; continuing poll." >&2
            UPDATE_STATUS_RESULT="updating"
            ;;
        *)
            UPDATE_STATUS_RESULT="ready"
            ;;
    esac
}

find_converged_replica_count() {
    local svc="$1"
    local svc_ls="$2"
    local ls_name ls_replicas replicas_core actual target

    CONVERGED_REPLICAS=""
    while IFS='|' read -r ls_name ls_replicas; do
        ls_name="${ls_name//$'\r'/}"
        ls_replicas="${ls_replicas//$'\r'/}"
        [ "$ls_name" = "$svc" ] || continue
        replicas_core="${ls_replicas%% *}"
        if [[ "$replicas_core" =~ ^([0-9]+)/([0-9]+)$ ]]; then
            actual="${BASH_REMATCH[1]}"
            target="${BASH_REMATCH[2]}"
            if [ "$((10#$target))" -ge 1 ] && [ "$((10#$actual))" -eq "$((10#$target))" ]; then
                CONVERGED_REPLICAS="$((10#$target))"
                return 0
            fi
        fi
        return 1
    done <<< "$svc_ls"
    return 1
}

validate_task_fields() {
    local svc="$1"
    local task_id="$2"
    local task_state="$3"
    local task_err="$4"

    if [ -z "$task_id" ]; then
        INSPECTED_FAIL_REASON="service ${svc} returned empty task ID"
        return 1
    fi
    case "$task_state" in
        Running*) ;;
        *)
            INSPECTED_FAIL_REASON="service ${svc} task ${task_id} is not Running (state='${task_state}', error='${task_err}')"
            return 1
            ;;
    esac
    if [ -n "$task_err" ]; then
        INSPECTED_FAIL_REASON="service ${svc} task ${task_id} reported error: ${task_err}"
        return 1
    fi
    return 0
}

inspect_running_tasks() {
    local svc="$1"
    local expected_n="$2"
    local tasks task_id task_state task_err
    local task_ids=()

    INSPECTED_TASK_IDS=""
    INSPECTED_FAIL_REASON=""

    if ! tasks=$(timeout 10 docker service ps "$svc" --filter desired-state=running --no-trunc --format "{{.ID}}|{{.CurrentState}}|{{.Error}}" 2>&1); then
        INSPECTED_FAIL_REASON="docker service ps failed for ${svc}: ${tasks}"
        return 1
    fi

    while IFS='|' read -r task_id task_state task_err; do
        task_id="${task_id//$'\r'/}"
        task_state="${task_state//$'\r'/}"
        task_err="${task_err//$'\r'/}"
        if [ -z "$task_id" ] && [ -z "$task_state" ] && [ -z "$task_err" ]; then
            continue
        fi
        validate_task_fields "$svc" "$task_id" "$task_state" "$task_err" || return 1
        task_ids+=("$task_id")
    done <<< "$tasks"

    if [ "${#task_ids[@]}" -ne "$expected_n" ]; then
        INSPECTED_FAIL_REASON="service ${svc} has ${#task_ids[@]} running task(s) (expected ${expected_n})"
        return 1
    fi

    INSPECTED_TASK_IDS=$(printf '%s\n' "${task_ids[@]}" | LC_ALL=C sort | paste -sd ',' -)
    return 0
}

poll_services_once() {
    local svc_ls svc all_ready=1

    CANDIDATE_COUNTS=()
    CANDIDATE_IDS=()
    if ! svc_ls=$(timeout 10 docker service ls --filter "name=${STACK_NAME}_" --format "{{.Name}}|{{.Replicas}}" 2>&1); then
        echo "Warning: docker service ls failed during convergence poll: ${svc_ls}" >&2
        return 1
    fi

    for svc in "${SERVICES[@]}"; do
        check_update_state "$svc"
        if [ "$UPDATE_STATUS_RESULT" != "ready" ]; then
            all_ready=0
            continue
        fi
        if ! find_converged_replica_count "$svc" "$svc_ls"; then
            all_ready=0
            continue
        fi
        if ! inspect_running_tasks "$svc" "$CONVERGED_REPLICAS"; then
            echo "Warning: ${INSPECTED_FAIL_REASON}" >&2
            all_ready=0
            continue
        fi
        CANDIDATE_COUNTS+=("$CONVERGED_REPLICAS")
        CANDIDATE_IDS+=("$INSPECTED_TASK_IDS")
    done

    [ "$all_ready" -eq 1 ]
}

wait_for_convergence() {
    local deadline

    echo "Waiting up to ${CONVERGENCE_TIMEOUT}s for stack '${STACK_NAME}' services to converge (${SERVICES[*]})..."
    deadline=$(( $(date +%s) + CONVERGENCE_TIMEOUT ))

    while true; do
        if poll_services_once; then
            BASELINE_COUNTS=("${CANDIDATE_COUNTS[@]}")
            BASELINE_IDS=("${CANDIDATE_IDS[@]}")
            return 0
        fi
        if [ "$(date +%s)" -ge "$deadline" ]; then
            echo "::error::Post-deploy convergence timed out after ${CONVERGENCE_TIMEOUT}s for stack '${STACK_NAME}' (${SERVICES[*]})." >&2
            dump_diagnostics
            exit 1
        fi
        sleep "$POLL_INTERVAL"
    done
}

verify_dwell_service() {
    local check_idx="$1"
    local svc="$2"
    local expected_n="$3"
    local expected_ids="$4"

    check_update_state "$svc"
    if [ "$UPDATE_STATUS_RESULT" != "ready" ]; then
        echo "::error::Stability dwell check ${check_idx}/${DWELL_CHECKS} failed for ${svc}: update state '${UPDATE_STATUS_RESULT}'." >&2
        dump_diagnostics
        exit 1
    fi
    if ! inspect_running_tasks "$svc" "$expected_n"; then
        echo "::error::Stability dwell check ${check_idx}/${DWELL_CHECKS} failed for ${svc}: ${INSPECTED_FAIL_REASON}." >&2
        dump_diagnostics
        exit 1
    fi
    if [ "$INSPECTED_TASK_IDS" != "$expected_ids" ]; then
        echo "::error::Task ID churn detected for ${svc} during dwell check ${check_idx}/${DWELL_CHECKS}: expected [${expected_ids}], got [${INSPECTED_TASK_IDS}]." >&2
        dump_diagnostics
        exit 1
    fi
}

run_dwell_checks() {
    local i check_idx

    echo "Initial convergence reached. Running ${DWELL_CHECKS} stability dwell checks (${DWELL_INTERVAL}s apart)..."
    for i in "${!SERVICES[@]}"; do
        echo "  baseline ${SERVICES[$i]} (${BASELINE_COUNTS[$i]}/${BASELINE_COUNTS[$i]}) -> tasks [${BASELINE_IDS[$i]}]"
    done

    for check_idx in $(seq 1 "$DWELL_CHECKS"); do
        sleep "$DWELL_INTERVAL"
        for i in "${!SERVICES[@]}"; do
            verify_dwell_service "$check_idx" "${SERVICES[$i]}" "${BASELINE_COUNTS[$i]}" "${BASELINE_IDS[$i]}"
        done
        echo "Dwell check ${check_idx}/${DWELL_CHECKS} passed (task IDs stable and Running with zero errors)."
    done
}

main() {
    parse_args "$@"
    wait_for_convergence
    run_dwell_checks
    echo "Post-deploy Swarm convergence and stability verified for stack '${STACK_NAME}'."
}

main "$@"

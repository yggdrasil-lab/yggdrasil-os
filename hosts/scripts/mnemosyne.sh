#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Yggdrasil OS - Host Mnemosyne CLI Wrapper
# ==============================================================================
# Executes the Mnemosyne CLI inside the running odin_huginn-gateway Swarm
# container, retrying briefly across rolling Swarm updates and failing loudly
# when no running container is available.
# Fast-paths --help/-h/help/no-args (<1ms, zero docker exec) and `get <id>`
# (read-only sqlite3 query without loading ONNX embeddings).
# ==============================================================================

DOCKER_BIN="${DOCKER_BIN:-docker}"
CONTAINER_NAME="${CONTAINER_NAME:-odin_huginn-gateway}"
WAIT_RETRIES="${MNEMOSYNE_WAIT_RETRIES:-5}"
WAIT_SLEEP="${MNEMOSYNE_WAIT_SLEEP:-2}"

print_usage() {
    cat <<'EOF'
Mnemosyne - Host CLI Wrapper (runs inside Odin Swarm container)

Usage:
  mnemosyne <command> [args...]

Common Commands:
  recall <query> [top_k]        Search memories (loads ONNX inside container)
  remember <content> [...]      Store a memory (CLI subcommand: store)
  get <id>                      Fetch a memory row by ID as JSON (fast, no ONNX)
  status                        Show sync status (CLI subcommand: sync-status)
  stats                         Show memory store statistics
  export [file.json]            Export memories

Environment:
  CONTAINER_NAME                Target container filter (default: odin_huginn-gateway)
  MNEMOSYNE_WAIT_RETRIES        Container lookup retries (default: 5)
  MNEMOSYNE_WAIT_SLEEP          Seconds between retries (default: 2)

Note:
  Inside Antigravity IDE, prefer the mnemosyne_get and mnemosyne_recall MCP tools.
EOF
}

if [ "$#" -eq 0 ]; then
    print_usage
    exit 0
fi

case "$1" in
    -h|--help|help)
        print_usage
        exit 0
        ;;
    get)
        if [ "$#" -ne 2 ] || [ -z "${2:-}" ]; then
            echo "Error: memory_id is required. Usage: mnemosyne get <memory_id>" >&2
            exit 2
        fi
        ;;
esac

find_running_container() {
    local output=""
    if output="$(timeout 5 "${DOCKER_BIN}" ps -q -f "name=${CONTAINER_NAME}" -f status=running 2>/dev/null)"; then
        if [ -n "${output}" ]; then
            printf '%s\n' "${output}" | head -n 1
        fi
    fi
}

CONTAINER=""
attempt=0
while true; do
    CONTAINER="$(find_running_container)"
    if [ -n "${CONTAINER}" ]; then
        break
    fi
    if [ "${attempt}" -ge "${WAIT_RETRIES}" ]; then
        break
    fi
    attempt=$((attempt + 1))
    sleep "${WAIT_SLEEP}"
done

if [ -z "${CONTAINER}" ]; then
    echo "Error: no running ${CONTAINER_NAME} container found after $((attempt + 1)) attempt(s)." >&2
    echo "Check Swarm service status: timeout 5 docker service ps ${CONTAINER_NAME}" >&2
    exit 1
fi

if [ "$1" = "get" ]; then
    GET_PYTHON_SCRIPT='
import contextlib, json, os, sqlite3, sys

memory_id = sys.argv[1]
db_path = os.environ.get("MNEMOSYNE_DB_PATH") or os.path.join(
    os.environ.get("MNEMOSYNE_DATA_DIR", "/opt/data/.hermes/mnemosyne/data"),
    "mnemosyne.db",
)
if not os.path.exists(db_path):
    print(f"Error: Mnemosyne database not found at {db_path}", file=sys.stderr)
    sys.exit(1)

queries = (
    (
        "working",
        "SELECT id, content, source, timestamp, session_id, importance, metadata_json, "
        "veracity, created_at, author_id, author_type, scope, channel_id, valid_until, "
        "superseded_by, recall_count, last_recalled FROM working_memory WHERE id = ?",
    ),
    (
        "episodic",
        "SELECT id, content, source, timestamp, session_id, importance, metadata_json, "
        "veracity, created_at, author_id, author_type, scope, channel_id, valid_until, "
        "superseded_by, recall_count, last_recalled, event_date, event_date_precision "
        "FROM episodic_memory WHERE id = ?",
    ),
    (
        "legacy",
        "SELECT id, content, source, timestamp, session_id, importance, metadata_json, "
        "created_at FROM memories WHERE id = ?",
    ),
)

found = None
with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
    conn.row_factory = sqlite3.Row
    for tier, sql in queries:
        row = conn.execute(sql, (memory_id,)).fetchone()
        if row is not None:
            found = dict(row)
            found["tier"] = tier
            break

if found is None:
    print(f"Error: memory {memory_id!r} not found", file=sys.stderr)
    sys.exit(1)

print(json.dumps({"status": "ok", "memory": found}, indent=2, ensure_ascii=False))
'
    exec "${DOCKER_BIN}" exec -i "${CONTAINER}" /opt/hermes/.venv/bin/python -c "${GET_PYTHON_SCRIPT}" "$2"
fi

if [ -t 0 ] && [ -t 1 ]; then
    exec "${DOCKER_BIN}" exec -it "${CONTAINER}" /opt/hermes/.venv/bin/mnemosyne "$@"
else
    exec "${DOCKER_BIN}" exec -i "${CONTAINER}" /opt/hermes/.venv/bin/mnemosyne "$@"
fi

#!/usr/bin/env bash
set -euo pipefail

# swarm/install_startup_service.sh (Zero-Clone Shim)
# Installs the Swarm Startup Orchestrator and Docker Graceful Restart tools on Gaia.

if [ "$(id -u)" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)." >&2
    exit 1
fi

SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
fi

RUN_ROOT=""
TMP_DIR=""
cleanup_tmp_dir() {
    if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
        rm -rf "${TMP_DIR}"
    fi
}
trap cleanup_tmp_dir EXIT

if [ -n "${SCRIPT_DIR}" ] && [ -f "${SCRIPT_DIR}/../yggdrasil_os/__init__.py" ]; then
    RUN_ROOT="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
elif [ -n "${YGGDRASIL_OS_ROOT:-}" ]; then
    if [ ! -f "${YGGDRASIL_OS_ROOT}/yggdrasil_os/__init__.py" ]; then
        echo "Error: YGGDRASIL_OS_ROOT=${YGGDRASIL_OS_ROOT} does not contain yggdrasil_os/__init__.py" >&2
        exit 1
    fi
    RUN_ROOT="${YGGDRASIL_OS_ROOT}"
else
    TMP_DIR="$(mktemp -d)"
    ARCHIVE_URL="${YGGDRASIL_OS_ARCHIVE_URL:-https://github.com/yggdrasil-lab/yggdrasil-os/archive/refs/heads/${YGGDRASIL_OS_REF:-main}.tar.gz}"
    curl -fsSL "${ARCHIVE_URL}" -o "${TMP_DIR}/yggdrasil-os.tar.gz"
    tar -xzf "${TMP_DIR}/yggdrasil-os.tar.gz" -C "${TMP_DIR}"
    INIT_PATH="$(find "${TMP_DIR}" -maxdepth 3 -path "*/yggdrasil_os/__init__.py" | head -n 1 || true)"
    if [ -z "${INIT_PATH}" ]; then
        echo "Error: Downloaded archive from ${ARCHIVE_URL} lacks yggdrasil_os/__init__.py" >&2
        exit 1
    fi
    RUN_ROOT="$(cd "$(dirname "${INIT_PATH}")/.." >/dev/null 2>&1 && pwd)"
fi

PYTHONPATH="${RUN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 -m yggdrasil_os install-startup-service "$@"

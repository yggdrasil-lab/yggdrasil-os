"""Versioned Docker Swarm secret creation and pruning."""

from __future__ import annotations

import hashlib
import re
import sys
from typing import Sequence, TextIO

from yggdrasil_os.docker_port import DockerPort

VERSIONED_SECRET_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}$")


class SecretConfigError(ValueError):
    """Raised when secret CLI arguments or inputs are invalid."""


def scoped_secret_pattern(base: str) -> re.Pattern[str]:
    """Return canonical regex matching only <base>_<8 hex chars>."""
    clean_base = _validate_base(base)
    return re.compile(rf"^{re.escape(clean_base)}_[0-9a-f]{{8}}$")


def prepare_secret(base: str, raw_value: str) -> tuple[str, str]:
    """Strip trailing newlines, hash with MD5-8, and return (full_name, payload)."""
    clean_base = _validate_base(base)
    stripped = raw_value.rstrip("\n")
    if not stripped:
        raise SecretConfigError("Error: Secret content must be provided via stdin.")
    digest = hashlib.md5(stripped.encode("utf-8")).hexdigest()[:8]
    return f"{clean_base}_{digest}", stripped


def compute_secret_name(base: str, raw_value: str) -> str:
    """Compute canonical <base>_<md5[:8]> secret name matching bash $(cat)."""
    full_name, _ = prepare_secret(base, raw_value)
    return full_name


def ensure_secret(base: str, raw_value: str, docker: DockerPort) -> str:
    """Idempotently create a versioned Swarm secret and return its full name."""
    full_name, payload = prepare_secret(base, raw_value)
    inspect_res = docker.run(["secret", "inspect", full_name], check=False)
    if inspect_res.returncode == 0:
        return full_name
    docker.run(["secret", "create", full_name, "-"], stdin_text=payload, check=True)
    return full_name


def prune_secrets(
    docker: DockerPort,
    base: str | None = None,
    *,
    out: TextIO | None = None,
) -> tuple[str, ...]:
    """Prune unused versioned Swarm secrets globally or scoped to base."""
    out_stream = sys.stdout if out is None else out
    clean_base: str | None = None
    if base is not None:
        clean_base = _validate_base(base)
        out_stream.write(f"Pruning secrets with prefix: {clean_base}_\n")
        pattern = scoped_secret_pattern(clean_base)
    else:
        pattern = VERSIONED_SECRET_SUFFIX_RE

    candidates = _list_candidate_secrets(docker, pattern)
    if not candidates:
        if clean_base is not None:
            out_stream.write(f"No secrets found with prefix {clean_base}_\n")
        return ()

    used_secrets = _collect_used_secrets(docker)
    return _remove_unused_secrets(candidates, used_secrets, docker, out_stream)


def parse_secret_base_arg(argv: Sequence[str], *, usage: str | None = None) -> str:
    """Parse a single <SECRET_BASE_NAME> argument, failing loudly if missing."""
    args = list(argv)
    if not args or not args[0].strip():
        detail = "Error: Secret base name is required."
        if usage:
            detail = f"{detail}\n{usage}"
        raise SecretConfigError(detail)
    if len(args) > 1:
        raise SecretConfigError(f"Error: Unexpected extra argument: {args[1]}")
    return _validate_base(args[0])


def _validate_base(base: str) -> str:
    clean_base = base.strip()
    if not clean_base:
        raise SecretConfigError("Error: Secret base name is required.")
    return clean_base


def _list_candidate_secrets(docker: DockerPort, pattern: re.Pattern[str]) -> list[str]:
    ls_res = docker.run(["secret", "ls", "--format", "{{.Name}}"], check=True)
    candidates: list[str] = []
    for raw_line in ls_res.stdout.splitlines():
        name = raw_line.strip()
        if name and pattern.search(name):
            candidates.append(name)
    return candidates


def _collect_used_secrets(docker: DockerPort) -> set[str]:
    svc_ls = docker.run(["service", "ls", "-q"], check=False)
    if svc_ls.returncode != 0:
        return set()
    service_ids = [tok for tok in svc_ls.stdout.split() if tok]
    if not service_ids:
        return set()
    inspect_res = docker.run(
        [
            "service",
            "inspect",
            *service_ids,
            "--format",
            "{{ range .Spec.TaskTemplate.ContainerSpec.Secrets }}{{ .SecretName }} {{ end }}",
        ],
        check=False,
    )
    if inspect_res.returncode != 0:
        return set()
    return set(inspect_res.stdout.split())


def _remove_unused_secrets(
    candidates: Sequence[str],
    used_secrets: set[str],
    docker: DockerPort,
    out_stream: TextIO,
) -> tuple[str, ...]:
    removed: list[str] = []
    for secret in candidates:
        if secret in used_secrets:
            out_stream.write(f"[KEEP] {secret} (In use)\n")
            continue
        out_stream.write(f"[DELETE] {secret}\n")
        rm_res = docker.run(["secret", "rm", secret], check=False)
        if rm_res.returncode != 0:
            out_stream.write(f"Failed to delete {secret}\n")
            continue
        removed.append(secret)
    return tuple(removed)

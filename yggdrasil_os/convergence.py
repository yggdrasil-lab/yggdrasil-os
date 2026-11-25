"""Post-deploy Docker Swarm convergence and stability dwell verification."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence, TextIO

from yggdrasil_os.docker_port import DockerPort
from yggdrasil_os.ports import Clock, CommandResult

CONVERGENCE_USAGE = """Usage: verify_convergence.sh [--timeout <sec>] [--poll <sec>] [--dwell-checks <n>] [--dwell-interval <sec>] [--log-tail <n>] <STACK_NAME> <SERVICE_1> [SERVICE_2 ...]

Options (also configurable via env vars CONVERGENCE_TIMEOUT, POLL_INTERVAL, DWELL_CHECKS, DWELL_INTERVAL, LOG_TAIL):
  --timeout <sec>         Convergence timeout in seconds (default: 120)
  --poll <sec>            Poll interval in seconds (default: 5)
  --dwell-checks <n>      Number of stability dwell checks after convergence (default: 3)
  --dwell-interval <sec>  Seconds between stability dwell checks (default: 10)
  --log-tail <n>          Number of service log lines to dump on failure (default: 80)
  -h, --help              Show this help message and exit"""

_FLAG_TO_ENV = {
    "--timeout": "CONVERGENCE_TIMEOUT",
    "--poll": "POLL_INTERVAL",
    "--dwell-checks": "DWELL_CHECKS",
    "--dwell-interval": "DWELL_INTERVAL",
    "--log-tail": "LOG_TAIL",
}
_DEFAULT_TIMINGS = {
    "CONVERGENCE_TIMEOUT": "120",
    "POLL_INTERVAL": "5",
    "DWELL_CHECKS": "3",
    "DWELL_INTERVAL": "10",
    "LOG_TAIL": "80",
}
_POSITIVE_INT_RE = re.compile(r"^[0-9]+$")
_REPLICAS_RE = re.compile(r"^([0-9]+)/([0-9]+)$")


class ConvergenceConfigError(ValueError):
    """Raised when verify-convergence CLI arguments or env vars are invalid."""


class ConvergenceFailureError(RuntimeError):
    """Raised when Swarm convergence or stability dwell verification fails."""


@dataclass(frozen=True)
class ConvergenceConfig:
    """Immutable configuration for post-deploy Swarm convergence verification."""

    stack_name: str
    services: tuple[str, ...]
    timeout: int = 120
    poll_interval: int = 5
    dwell_checks: int = 3
    dwell_interval: int = 10
    log_tail: int = 80


@dataclass(frozen=True)
class ServiceBaseline:
    """Immutable converged baseline snapshot for a single Swarm service."""

    service: str
    replicas: int
    task_ids: str


@dataclass(frozen=True)
class ConvergenceResult:
    """Immutable verification summary after convergence and dwell checks pass."""

    stack_name: str
    baselines: tuple[ServiceBaseline, ...]


def parse_convergence_args(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> ConvergenceConfig | None:
    """Parse interleaved convergence flags, env defaults, stack, and services."""
    out_stream = sys.stdout if out is None else out
    source_env = os.environ if env is None else env
    raw_timings = {key: source_env.get(key, default) for key, default in _DEFAULT_TIMINGS.items()}
    positionals: list[str] = []

    if _scan_convergence_tokens(argv, raw_timings, positionals, out_stream):
        return None

    timings = {key: _validate_positive_int(key, val) for key, val in raw_timings.items()}
    if len(positionals) < 2:
        raise ConvergenceConfigError(
            "Error: <STACK_NAME> and at least one <SERVICE> argument are required.\n"
            f"{CONVERGENCE_USAGE}"
        )

    stack_name = positionals[0]
    if not stack_name:
        raise ConvergenceConfigError("Error: <STACK_NAME> must not be empty.")
    services = _normalize_services(stack_name, positionals[1:])
    return ConvergenceConfig(
        stack_name=stack_name,
        services=services,
        timeout=timings["CONVERGENCE_TIMEOUT"],
        poll_interval=timings["POLL_INTERVAL"],
        dwell_checks=timings["DWELL_CHECKS"],
        dwell_interval=timings["DWELL_INTERVAL"],
        log_tail=timings["LOG_TAIL"],
    )


def verify_convergence(
    config: ConvergenceConfig,
    docker: DockerPort,
    clock: Clock,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> ConvergenceResult:
    """Poll Swarm until N/N convergence, then verify task-ID stability over dwell."""
    out_stream = sys.stdout if out is None else out
    err_stream = sys.stderr if err is None else err

    baselines = _wait_for_convergence(config, docker, clock, out_stream, err_stream)
    _run_dwell_checks(config, baselines, docker, clock, out_stream, err_stream)
    out_stream.write(
        f"Post-deploy Swarm convergence and stability verified for stack '{config.stack_name}'.\n"
    )
    return ConvergenceResult(stack_name=config.stack_name, baselines=baselines)


def dump_diagnostics(
    config: ConvergenceConfig,
    docker: DockerPort,
    *,
    err: TextIO | None = None,
) -> None:
    """Emit Swarm service ls, ps, and tail logs to stderr, ignoring command errors."""
    err_stream = sys.stderr if err is None else err
    err_stream.write(f"=== Post-Deploy Swarm Diagnostics ({config.stack_name}) ===\n")
    _write_diag_cmd(
        docker,
        ["service", "ls", "--filter", f"name={config.stack_name}_"],
        timeout=10.0,
        err_stream=err_stream,
    )
    for svc in config.services:
        err_stream.write(f"--- docker service ps --no-trunc {svc} ---\n")
        _write_diag_cmd(docker, ["service", "ps", svc, "--no-trunc"], timeout=10.0, err_stream=err_stream)
        err_stream.write(f"--- docker service logs --tail {config.log_tail} {svc} ---\n")
        _write_diag_cmd(
            docker,
            ["service", "logs", "--tail", str(config.log_tail), svc],
            timeout=15.0,
            err_stream=err_stream,
        )


def _scan_convergence_tokens(
    argv: Sequence[str],
    raw_timings: dict[str, str],
    positionals: list[str],
    out_stream: TextIO,
) -> bool:
    idx = 0
    tokens = list(argv)
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in ("-h", "--help"):
            out_stream.write(f"{CONVERGENCE_USAGE}\n")
            return True
        if tok == "--":
            positionals.extend(tokens[idx + 1 :])
            return False
        if "=" in tok and tok.split("=", 1)[0] in _FLAG_TO_ENV:
            opt, val = tok.split("=", 1)
            if not val:
                raise ConvergenceConfigError(f"Error: Option '{opt}' requires a non-empty value.")
            raw_timings[_FLAG_TO_ENV[opt]] = val
            idx += 1
            continue
        if tok in _FLAG_TO_ENV:
            val = tokens[idx + 1] if idx + 1 < len(tokens) else ""
            if not val or val.startswith("--"):
                raise ConvergenceConfigError(f"Error: Option '{tok}' requires a non-empty value.")
            raw_timings[_FLAG_TO_ENV[tok]] = val
            idx += 2
            continue
        if tok.startswith("-"):
            raise ConvergenceConfigError(f"Error: Unknown option '{tok}'.\n{CONVERGENCE_USAGE}")
        positionals.append(tok)
        idx += 1
    return False


def _validate_positive_int(name: str, raw: str) -> int:
    if not _POSITIVE_INT_RE.match(raw):
        raise ConvergenceConfigError(
            f"Error: Invalid integer for {name}: '{raw}' (expected integer >= 1)."
        )
    value = int(raw, 10)
    if value < 1:
        raise ConvergenceConfigError(
            f"Error: Invalid integer for {name}: '{raw}' (expected integer >= 1)."
        )
    return value


def _normalize_services(stack_name: str, raw_services: Sequence[str]) -> tuple[str, ...]:
    prefix = f"{stack_name}_"
    seen: dict[str, None] = {}
    for raw_svc in raw_services:
        if not raw_svc:
            raise ConvergenceConfigError("Error: Service name must not be empty.")
        normalized = raw_svc if raw_svc.startswith(prefix) else f"{prefix}{raw_svc}"
        if normalized not in seen:
            seen[normalized] = None
    return tuple(seen.keys())


def _wait_for_convergence(
    config: ConvergenceConfig,
    docker: DockerPort,
    clock: Clock,
    out_stream: TextIO,
    err_stream: TextIO,
) -> tuple[ServiceBaseline, ...]:
    services_str = " ".join(config.services)
    out_stream.write(
        f"Waiting up to {config.timeout}s for stack '{config.stack_name}' "
        f"services to converge ({services_str})...\n"
    )
    deadline = clock.monotonic() + config.timeout

    while True:
        baselines = _poll_services_once(config, docker, err_stream)
        if baselines is not None:
            return baselines
        if clock.monotonic() >= deadline:
            msg = (
                f"::error::Post-deploy convergence timed out after {config.timeout}s "
                f"for stack '{config.stack_name}' ({services_str})."
            )
            err_stream.write(f"{msg}\n")
            dump_diagnostics(config, docker, err=err_stream)
            raise ConvergenceFailureError(msg)
        clock.sleep(config.poll_interval)


def _poll_services_once(
    config: ConvergenceConfig,
    docker: DockerPort,
    err_stream: TextIO,
) -> tuple[ServiceBaseline, ...] | None:
    ls_res = docker.run(
        ["service", "ls", "--filter", f"name={config.stack_name}_", "--format", "{{.Name}}|{{.Replicas}}"],
        timeout=10.0,
        check=False,
    )
    if ls_res.returncode != 0:
        err_stream.write(f"Warning: docker service ls failed during convergence poll: {_detail(ls_res)}\n")
        return None

    baselines: list[ServiceBaseline] = []
    all_ready = True
    for svc in config.services:
        state_result = _check_update_state(svc, config, docker, err_stream)
        if state_result != "ready":
            all_ready = False
            continue
        replicas = _find_converged_replica_count(svc, ls_res.stdout)
        if replicas is None:
            all_ready = False
            continue
        task_ids, fail_reason = _inspect_running_tasks(svc, replicas, docker)
        if fail_reason is not None:
            err_stream.write(f"Warning: {fail_reason}\n")
            all_ready = False
            continue
        baselines.append(ServiceBaseline(service=svc, replicas=replicas, task_ids=task_ids))

    return tuple(baselines) if all_ready else None


def _check_update_state(
    svc: str,
    config: ConvergenceConfig,
    docker: DockerPort,
    err_stream: TextIO,
) -> str:
    res = docker.run(
        ["service", "inspect", svc, "--format", "{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}"],
        timeout=10.0,
        check=False,
    )
    if res.returncode != 0:
        err_stream.write(f"Warning: docker service inspect failed for {svc}: {_detail(res)}\n")
        return "transient_error"

    state = "".join(res.stdout.split())
    if state == "paused" or state.startswith("rollback_"):
        msg = f"::error::Service {svc} entered abnormal Swarm update state '{state}'."
        err_stream.write(f"{msg}\n")
        dump_diagnostics(config, docker, err=err_stream)
        raise ConvergenceFailureError(msg)
    if state == "updating":
        err_stream.write(f"Warning: Service {svc} update state is 'updating'; continuing poll.\n")
        return "updating"
    return "ready"


def _find_converged_replica_count(svc: str, svc_ls_stdout: str) -> int | None:
    for line in svc_ls_stdout.splitlines():
        clean_line = line.replace("\r", "")
        if "|" not in clean_line:
            continue
        ls_name, ls_replicas = clean_line.split("|", 1)
        if ls_name != svc:
            continue
        core = ls_replicas.split(" ", 1)[0]
        match = _REPLICAS_RE.match(core)
        if match is None:
            return None
        actual = int(match.group(1), 10)
        target = int(match.group(2), 10)
        return target if target >= 1 and actual == target else None
    return None


def _inspect_running_tasks(
    svc: str,
    expected_n: int,
    docker: DockerPort,
) -> tuple[str, str | None]:
    res = docker.run(
        [
            "service",
            "ps",
            svc,
            "--filter",
            "desired-state=running",
            "--no-trunc",
            "--format",
            "{{.ID}}|{{.CurrentState}}|{{.Error}}",
        ],
        timeout=10.0,
        check=False,
    )
    if res.returncode != 0:
        return "", f"docker service ps failed for {svc}: {_detail(res)}"

    task_ids: list[str] = []
    for raw_line in res.stdout.splitlines():
        clean_line = raw_line.replace("\r", "")
        parts = clean_line.split("|", 2)
        task_id = parts[0] if len(parts) > 0 else ""
        task_state = parts[1] if len(parts) > 1 else ""
        task_err = parts[2] if len(parts) > 2 else ""
        if not task_id and not task_state and not task_err:
            continue
        reason = _validate_task_fields(svc, task_id, task_state, task_err)
        if reason is not None:
            return "", reason
        task_ids.append(task_id)

    if len(task_ids) != expected_n:
        return "", f"service {svc} has {len(task_ids)} running task(s) (expected {expected_n})"
    return ",".join(sorted(task_ids)), None


def _validate_task_fields(
    svc: str,
    task_id: str,
    task_state: str,
    task_err: str,
) -> str | None:
    if not task_id:
        return f"service {svc} returned empty task ID"
    if not task_state.startswith("Running"):
        return f"service {svc} task {task_id} is not Running (state='{task_state}', error='{task_err}')"
    if task_err:
        return f"service {svc} task {task_id} reported error: {task_err}"
    return None


def _run_dwell_checks(
    config: ConvergenceConfig,
    baselines: tuple[ServiceBaseline, ...],
    docker: DockerPort,
    clock: Clock,
    out_stream: TextIO,
    err_stream: TextIO,
) -> None:
    out_stream.write(
        f"Initial convergence reached. Running {config.dwell_checks} stability dwell checks "
        f"({config.dwell_interval}s apart)...\n"
    )
    for baseline in baselines:
        out_stream.write(
            f"  baseline {baseline.service} ({baseline.replicas}/{baseline.replicas}) "
            f"-> tasks [{baseline.task_ids}]\n"
        )

    for check_idx in range(1, config.dwell_checks + 1):
        clock.sleep(config.dwell_interval)
        for baseline in baselines:
            _verify_dwell_service(check_idx, baseline, config, docker, err_stream)
        out_stream.write(
            f"Dwell check {check_idx}/{config.dwell_checks} passed "
            "(task IDs stable and Running with zero errors).\n"
        )


def _verify_dwell_service(
    check_idx: int,
    baseline: ServiceBaseline,
    config: ConvergenceConfig,
    docker: DockerPort,
    err_stream: TextIO,
) -> None:
    svc = baseline.service
    state_result = _check_update_state(svc, config, docker, err_stream)
    if state_result != "ready":
        msg = (
            f"::error::Stability dwell check {check_idx}/{config.dwell_checks} failed "
            f"for {svc}: update state '{state_result}'."
        )
        _fail_with_diagnostics(msg, config, docker, err_stream)

    actual_ids, fail_reason = _inspect_running_tasks(svc, baseline.replicas, docker)
    if fail_reason is not None:
        msg = (
            f"::error::Stability dwell check {check_idx}/{config.dwell_checks} failed "
            f"for {svc}: {fail_reason}."
        )
        _fail_with_diagnostics(msg, config, docker, err_stream)

    if actual_ids != baseline.task_ids:
        msg = (
            f"::error::Task ID churn detected for {svc} during dwell check "
            f"{check_idx}/{config.dwell_checks}: expected [{baseline.task_ids}], got [{actual_ids}]."
        )
        _fail_with_diagnostics(msg, config, docker, err_stream)


def _fail_with_diagnostics(
    msg: str,
    config: ConvergenceConfig,
    docker: DockerPort,
    err_stream: TextIO,
) -> None:
    err_stream.write(f"{msg}\n")
    dump_diagnostics(config, docker, err=err_stream)
    raise ConvergenceFailureError(msg)


def _write_diag_cmd(
    docker: DockerPort,
    args: Sequence[str],
    *,
    timeout: float,
    err_stream: TextIO,
) -> None:
    res = docker.run(args, timeout=timeout, check=False)
    for chunk in (res.stdout, res.stderr):
        if chunk:
            err_stream.write(chunk)
            if not chunk.endswith("\n"):
                err_stream.write("\n")


def _detail(res: CommandResult) -> str:
    return (res.stderr or res.stdout).strip()

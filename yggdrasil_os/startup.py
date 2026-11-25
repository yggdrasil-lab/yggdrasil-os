"""Gaia Swarm startup, convergence, auto-healing, and safe prune orchestrator."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from yggdrasil_os.docker_port import DockerPort
from yggdrasil_os.ports import Clock

CORE_SERVICES = (
    "olympus_adguardhome",
    "olympus_traefik",
    "hephaestus_registry",
)

_BACKOFF_ERROR_RE = re.compile(r"No such image|registry|network", re.IGNORECASE)


@dataclass(frozen=True)
class StartupContext:
    """Immutable configuration for Swarm startup orchestration."""

    euid: int | None = None
    readiness_timeout_seconds: int = 60
    readiness_poll_seconds: int = 3
    core_timeout_seconds: int = 30
    core_poll_seconds: int = 2
    converge_timeout_seconds: int = 120
    converge_poll_seconds: int = 5

    @classmethod
    def from_env(cls, *, euid: int | None = None) -> StartupContext:
        """Build StartupContext with standard Gaia timeouts."""
        return cls(euid=euid)

    @property
    def effective_euid(self) -> int:
        return os.geteuid() if self.euid is None else self.euid


@dataclass(frozen=True)
class StartupResult:
    """Immutable outcome summary from Swarm startup orchestration."""

    core_ready: tuple[str, ...]
    core_timed_out: tuple[str, ...]
    converged: bool
    healed_services: tuple[str, ...]
    started_runner: bool
    pruned_stopped_containers: bool
    running_count: int
    stopped_count: int


def wait_for_docker_ready(
    docker: DockerPort,
    clock: Clock,
    *,
    timeout_seconds: int,
    poll_interval: int,
    progress_label: str | None = None,
) -> bool:
    """Poll `docker info` until responsive or timeout_seconds elapses."""
    waited = 0
    while waited < timeout_seconds:
        if docker.run(["info"], check=False).returncode == 0:
            return True
        if progress_label:
            print(f"{progress_label} ({waited}s/{timeout_seconds}s)")
        clock.sleep(poll_interval)
        waited += poll_interval
    return docker.run(["info"], check=False).returncode == 0


def run_startup_orchestrator(
    context: StartupContext,
    *,
    docker: DockerPort,
    clock: Clock,
) -> StartupResult:
    """Run Gaia Swarm boot convergence, auto-healing, runner guard, and safe prune."""
    _ensure_docker_access(context, docker)
    _wait_daemon_and_tune_history(context, docker, clock)

    print("=== [3/5] Verifying Core Infrastructure (DNS, Proxy, Registry) ===")
    core_ready, core_timed_out = _wait_for_core_services(context, docker, clock)

    print("=== [4/5] Monitoring Swarm Service Convergence & Auto-Healing ===")
    converged, healed_services = _converge_services(context, docker, clock)

    print("=== [5/5] Pruning Stale/Dead Containers & Restoring Clean State ===")
    started_runner = _ensure_runner_started(docker)
    pruned = _prune_stopped_containers(docker)
    running_count, stopped_count = _summarize_swarm(docker)

    return StartupResult(
        core_ready=core_ready,
        core_timed_out=core_timed_out,
        converged=converged,
        healed_services=healed_services,
        started_runner=started_runner,
        pruned_stopped_containers=pruned,
        running_count=running_count,
        stopped_count=stopped_count,
    )


def _wait_daemon_and_tune_history(
    context: StartupContext,
    docker: DockerPort,
    clock: Clock,
) -> None:
    print("=== [1/5] Checking Docker Daemon Availability ===")
    ready = wait_for_docker_ready(
        docker,
        clock,
        timeout_seconds=context.readiness_timeout_seconds,
        poll_interval=context.readiness_poll_seconds,
        progress_label="Waiting for Docker daemon to respond...",
    )
    if not ready:
        raise RuntimeError(
            f"ERROR: Docker daemon did not respond within {context.readiness_timeout_seconds} seconds."
        )
    print("Docker daemon is online and responsive.")
    print("=== [2/5] Optimizing Swarm Task History Retention ===")
    docker.run(["swarm", "update", "--task-history-limit", "1"], check=False)
    print("Swarm task history retention limit set to 1.")


def _ensure_docker_access(context: StartupContext, docker: DockerPort) -> None:
    if context.effective_euid == 0:
        return
    if docker.run(["info"], check=False).returncode == 0:
        return
    raise PermissionError(
        "Error: This script must be run as root (sudo) or by a member of the 'docker' group."
    )


def _wait_for_core_services(
    context: StartupContext,
    docker: DockerPort,
    clock: Clock,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ready: list[str] = []
    timed_out: list[str] = []
    for svc in CORE_SERVICES:
        outcome = _wait_single_core_service(svc, context, docker, clock)
        if outcome is True:
            ready.append(svc)
        elif outcome is False:
            timed_out.append(svc)
    return tuple(ready), tuple(timed_out)


def _wait_single_core_service(
    svc: str,
    context: StartupContext,
    docker: DockerPort,
    clock: Clock,
) -> bool | None:
    if docker.run(["service", "inspect", svc], check=False).returncode != 0:
        return None
    waited = 0
    while waited < context.core_timeout_seconds:
        replicas = _find_service_replicas(docker, svc)
        parsed = _parse_replicas(replicas) if replicas else None
        if parsed is not None and parsed[0] >= parsed[1] and parsed[0] > 0:
            print(f"Checking {svc}... ready ({replicas})")
            return True
        clock.sleep(context.core_poll_seconds)
        waited += context.core_poll_seconds
    print(f"Checking {svc}... warning: not fully ready after {context.core_timeout_seconds}s (continuing)")
    return False


def _find_service_replicas(docker: DockerPort, service_name: str) -> str:
    res = docker.run(
        ["service", "ls", "--format", "{{.Name}} {{.Replicas}}"],
        check=False,
    )
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == service_name:
            return parts[1]
    return ""


def _converge_services(
    context: StartupContext,
    docker: DockerPort,
    clock: Clock,
) -> tuple[bool, tuple[str, ...]]:
    start_time = clock.monotonic()
    healed: list[str] = []
    while True:
        elapsed = int(clock.monotonic() - start_time)
        still_converging, stuck = _inspect_converging_services(docker)
        if not still_converging:
            print("All active Swarm services converged successfully!")
            return True, tuple(healed)
        _auto_heal_services(docker, stuck, healed)
        if elapsed >= context.converge_timeout_seconds:
            print(
                f"Notice: Convergence window reached ({context.converge_timeout_seconds}s). "
                "Proceeding to cleanup."
            )
            return False, tuple(healed)
        pending = " ".join(still_converging)
        print(
            f"Converging: {len(still_converging)} services pending ({pending})... "
            f"waiting ({elapsed}s/{context.converge_timeout_seconds}s)"
        )
        clock.sleep(context.converge_poll_seconds)


def _inspect_converging_services(docker: DockerPort) -> tuple[list[str], list[str]]:
    res = docker.run(["service", "ls", "--format", "{{.Name}}\t{{.Replicas}}"], check=False)
    still_converging: list[str] = []
    stuck: list[str] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        name, _, replicas = line.partition("\t")
        parsed = _parse_replicas(replicas.strip())
        if parsed is None or parsed[1] <= 0 or parsed[0] >= parsed[1]:
            continue
        desired = docker.run(
            ["service", "ps", name, "--filter", "desired-state=running", "-q"],
            check=False,
        )
        if not desired.stdout.strip():
            continue
        still_converging.append(name)
        err_res = docker.run(
            ["service", "ps", name, "--no-trunc", "--format", "{{.Error}}"],
            check=False,
        )
        lines = err_res.stdout.splitlines()
        first_err = lines[0] if lines else ""
        if _BACKOFF_ERROR_RE.search(first_err):
            stuck.append(name)
    return still_converging, stuck


def _auto_heal_services(docker: DockerPort, stuck: list[str], healed: list[str]) -> None:
    for svc in stuck:
        print(f"Auto-healing {svc} (kicking out of cold-boot registry backoff)...")
        docker.run(["service", "update", "--force", svc], check=False)
        healed.append(svc)


def _ensure_runner_started(docker: DockerPort) -> bool:
    names_res = docker.run(["ps", "-a", "--format", "{{.Names}}"], check=False)
    if "gaia-runner" not in names_res.stdout.splitlines():
        return False
    inspect_res = docker.run(
        ["inspect", "-f", "{{.State.Running}}", "gaia-runner"],
        check=False,
    )
    if inspect_res.stdout.strip() == "true":
        return False
    print("Starting standalone runner: gaia-runner...")
    return docker.run(["start", "gaia-runner"], check=False).returncode == 0


def _prune_stopped_containers(docker: DockerPort) -> bool:
    stopped_ids = _list_container_ids(
        docker,
        ("ps", "-a", "--filter", "status=created", "--filter", "status=exited", "--filter", "status=dead", "-q"),
    )
    if not stopped_ids:
        print("No dead/stopped containers found.")
        return False
    print(f"Pruning {len(stopped_ids)} stopped/dead container shells...")
    docker.run(["container", "prune", "-f"], check=False)
    dead_ids = _list_container_ids(docker, ("ps", "-a", "--filter", "status=dead", "-q"))
    if dead_ids:
        docker.run(["rm", "-f", "-v", *dead_ids], check=False)
    docker.run(["volume", "prune", "-f"], check=False)
    print("Dead/stopped container shells and anonymous volumes cleared.")
    return True


def _summarize_swarm(docker: DockerPort) -> tuple[int, int]:
    node_res = docker.run(["node", "ls"], check=False)
    if node_res.stdout:
        print(node_res.stdout.rstrip())
    running_ids = _list_container_ids(docker, ("ps", "-q", "--filter", "status=running"))
    stopped_ids = _list_container_ids(
        docker,
        ("ps", "-a", "--filter", "status=created", "--filter", "status=exited", "--filter", "status=dead", "-q"),
    )
    print(f"Active Running Containers: {len(running_ids)}")
    print(f"Stopped/Dead Containers:   {len(stopped_ids)}")
    print("Success: Gaia Swarm is operational and clean.")
    return len(running_ids), len(stopped_ids)


def _list_container_ids(docker: DockerPort, args: tuple[str, ...]) -> list[str]:
    res = docker.run(args, check=False)
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def _parse_replicas(replicas: str) -> tuple[int, int] | None:
    actual_str, sep, target_str = replicas.partition("/")
    if not sep:
        return None
    try:
        return int(actual_str), int(target_str)
    except ValueError:
        return None

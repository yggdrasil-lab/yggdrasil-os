"""Docker daemon restart and stale VXLAN recovery for Gaia and Muspelheim."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from yggdrasil_os.docker_port import DockerPort
from yggdrasil_os.ports import (
    AssetFetcher,
    Clock,
    CommandExecutionError,
    CommandRunner,
)
from yggdrasil_os.recovery_ports import NetlinkPort, SystemdPort
from yggdrasil_os.startup import (
    StartupContext,
    StartupResult,
    run_startup_orchestrator,
    wait_for_docker_ready,
)

DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main"
DEFAULT_APOLLO_RESTORE_SCRIPT = "/usr/local/bin/apollo-supply-restore.sh"
APOLLO_RESTORE_REL_PATH = "hosts/scripts/apollo-supply-restore.sh"
_MUSPELHEIM_STOP_CONTAINERS = ("slskd", "sabnzbd", "qbittorrent", "gluetun")


@dataclass(frozen=True)
class RestartContext:
    """Immutable runtime context for Docker daemon restart orchestration."""

    repo_root: Path | None = None
    raw_base: str = DEFAULT_RAW_BASE
    swarm_startup_script: str | None = None
    apollo_restore_script: str = DEFAULT_APOLLO_RESTORE_SCRIPT
    euid: int | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        repo_root: Path | None = None,
        euid: int | None = None,
    ) -> RestartContext:
        """Build RestartContext from environment variables."""
        env = os.environ if environ is None else environ
        swarm_override = env.get("SWARM_STARTUP_SCRIPT", "").strip() or None
        apollo_script = env.get("APOLLO_RESTORE_SCRIPT", "").strip() or DEFAULT_APOLLO_RESTORE_SCRIPT
        raw_base = env.get("RAW_BASE", "").strip() or DEFAULT_RAW_BASE
        return cls(
            repo_root=repo_root,
            raw_base=raw_base,
            swarm_startup_script=swarm_override,
            apollo_restore_script=apollo_script,
            euid=euid,
        )

    @property
    def effective_euid(self) -> int:
        return os.geteuid() if self.euid is None else self.euid


@dataclass(frozen=True)
class RestartResult:
    """Immutable outcome summary from restart_docker."""

    role: Literal["gaia", "muspelheim"]
    removed_vxlan_links: tuple[str, ...]
    used_systemd_fallback: bool
    startup_result: StartupResult | None = None


def sweep_stale_vxlan_links(netlink: NetlinkPort) -> tuple[str, ...]:
    """Delete only DOWN vx-* VXLAN interfaces; tolerate delete errors."""
    if not netlink.is_available():
        return ()
    removed: list[str] = []
    for link in netlink.list_vxlan_links():
        if link.state != "DOWN" or not link.name.startswith("vx-"):
            continue
        print(f"Removing stale down VXLAN interface: {link.name}")
        try:
            res = netlink.delete_link(link.name)
        except CommandExecutionError:
            continue
        if res.returncode == 0:
            removed.append(link.name)
    return tuple(removed)


def restart_docker(
    role: Literal["gaia", "muspelheim"],
    context: RestartContext,
    *,
    docker: DockerPort,
    systemd: SystemdPort,
    netlink: NetlinkPort,
    runner: CommandRunner,
    fetcher: AssetFetcher,
    clock: Clock,
) -> RestartResult:
    """Restart Docker daemon safely with VXLAN cleanup and role-specific hooks."""
    if role not in ("gaia", "muspelheim"):
        raise ValueError(f"Unsupported restart role: {role!r}")
    if context.effective_euid != 0:
        raise PermissionError("Error: This script must be run as root (sudo).")

    _flush_storage_buffers(role, runner)
    if role == "muspelheim":
        _stop_apollo_downloaders(docker)

    used_fallback, removed = _restart_daemon_with_vxlan_sweep(role, systemd, netlink)
    startup_res: StartupResult | None = None
    if role == "gaia":
        startup_res = _run_gaia_post_hook(context, docker=docker, runner=runner, clock=clock)
    else:
        _run_muspelheim_post_hook(
            context,
            docker=docker,
            runner=runner,
            fetcher=fetcher,
            clock=clock,
        )
    return RestartResult(
        role=role,
        removed_vxlan_links=removed,
        used_systemd_fallback=used_fallback,
        startup_result=startup_res,
    )


def _flush_storage_buffers(role: Literal["gaia", "muspelheim"], runner: CommandRunner) -> None:
    header = "=== [1/3] Flushing Storage Buffers ===" if role == "gaia" else "=== [1/5] Flushing Storage Buffers ==="
    print(header)
    runner.run(["sync"], check=True)
    print("Buffers synced." if role == "gaia" else "Buffers synced to disk.")


def _stop_apollo_downloaders(docker: DockerPort) -> None:
    print("=== [2/5] Gracefully Stopping Apollo Supply Downloaders ===")
    if docker.run(["info"], check=False).returncode == 0:
        docker.run(["stop", *_MUSPELHEIM_STOP_CONTAINERS], check=False)
        print("Apollo Supply containers stopped.")
    else:
        print("Docker daemon not running; skipping container stop.")


def _restart_daemon_with_vxlan_sweep(
    role: Literal["gaia", "muspelheim"],
    systemd: SystemdPort,
    netlink: NetlinkPort,
) -> tuple[bool, tuple[str, ...]]:
    header = "=== [2/3] Restarting Docker Service ===" if role == "gaia" else "=== [3/5] Restarting Docker Service ==="
    print(header)
    stop_res = systemd.stop_units(("docker.socket", "docker.service"))
    used_fallback = False
    if stop_res.returncode != 0:
        used_fallback = True
        fallback_res = systemd.stop_units(("docker",))
        if fallback_res.returncode != 0:
            raise CommandExecutionError(
                ["systemctl", "stop", "docker"],
                fallback_res.returncode,
                fallback_res.stdout,
                fallback_res.stderr,
            )

    removed = sweep_stale_vxlan_links(netlink)
    start_res = systemd.start_unit("docker")
    if start_res.returncode != 0:
        raise CommandExecutionError(
            ["systemctl", "start", "docker"],
            start_res.returncode,
            start_res.stdout,
            start_res.stderr,
        )
    print("Docker daemon restarted." if role == "gaia" else "Docker service restarted.")
    return used_fallback, removed


def _run_gaia_post_hook(
    context: RestartContext,
    *,
    docker: DockerPort,
    runner: CommandRunner,
    clock: Clock,
) -> StartupResult | None:
    print("=== [3/3] Orchestrating Swarm Recovery & Health Convergence ===")
    if context.swarm_startup_script:
        runner.run([context.swarm_startup_script], check=True)
        return None
    startup_ctx = StartupContext(euid=context.effective_euid)
    return run_startup_orchestrator(startup_ctx, docker=docker, clock=clock)


def _run_muspelheim_post_hook(
    context: RestartContext,
    *,
    docker: DockerPort,
    runner: CommandRunner,
    fetcher: AssetFetcher,
    clock: Clock,
) -> None:
    print("=== [4/5] Waiting for Docker Daemon Readiness ===")
    if not wait_for_docker_ready(docker, clock, timeout_seconds=30, poll_interval=2):
        raise RuntimeError("Error: Docker daemon failed to become responsive after 30s")
    print("Docker daemon is online and responsive.")

    print("=== [5/5] Restoring Apollo Supply & Pruning Exited Debris ===")
    _execute_apollo_restore(context, runner=runner, fetcher=fetcher)
    docker.run(["container", "prune", "-f"], check=False)
    print("Exited container debris pruned.")
    print("Muspelheim Docker restart and recovery complete!")


def _execute_apollo_restore(
    context: RestartContext,
    *,
    runner: CommandRunner,
    fetcher: AssetFetcher,
) -> None:
    configured = Path(context.apollo_restore_script)
    if configured.is_file() and os.access(configured, os.X_OK):
        runner.run([str(configured)], check=True)
        return
    local = fetcher.local_path(APOLLO_RESTORE_REL_PATH)
    if local is not None:
        runner.run(["bash", str(local)], check=True)
        return
    payload = fetcher.fetch_bytes(APOLLO_RESTORE_REL_PATH)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as handle:
            handle.write(payload)
            tmp_path = Path(handle.name)
        runner.run(["bash", str(tmp_path)], check=True)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

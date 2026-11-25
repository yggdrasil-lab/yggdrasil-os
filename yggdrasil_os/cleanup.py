"""Gaia Docker, overlay endpoint, registry GC, and systemd journal cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from yggdrasil_os.docker_port import DockerPort
from yggdrasil_os.ports import CommandExecutionError, CommandRunner
from yggdrasil_os.recovery_ports import SystemdPort


@dataclass(frozen=True)
class CleanupConfig:
    """Validated options for Gaia host storage cleanup."""

    containers_only: bool = False
    repo_root: Path | None = None


@dataclass(frozen=True)
class CleanupResult:
    """Immutable outcome summary from cleanup_storage."""

    containers_only: bool
    pruned_network_endpoints: bool
    ran_registry_gc: bool


def parse_cleanup_args(
    argv: Sequence[str],
    *,
    prog: str = "cleanup_storage.sh",
    repo_root: Path | None = None,
) -> CleanupConfig:
    """Parse cleanup CLI arguments and fail loud on unknown flags or positionals."""
    containers_only = False
    for arg in argv:
        if arg == "--containers-only":
            containers_only = True
        else:
            raise ValueError(f"Unknown argument: '{arg}'. Usage: {prog} [--containers-only]")
    return CleanupConfig(containers_only=containers_only, repo_root=repo_root)


def cleanup_storage(
    config: CleanupConfig,
    *,
    docker: DockerPort,
    systemd: SystemdPort,
    runner: CommandRunner,
) -> CleanupResult:
    """Execute Gaia container/volume/image/network/registry/journal cleanup."""
    print("=== Gaia Storage Cleanup Started ===")
    _report_disk_space("before", runner)
    _prune_containers_and_volumes(docker)

    if config.containers_only:
        _report_disk_space("after", runner)
        print("=== Gaia Containers-Only Cleanup Finished ===")
        return CleanupResult(
            containers_only=True,
            pruned_network_endpoints=False,
            ran_registry_gc=False,
        )

    print("--- Pruning Docker build cache (older than 168h / 7 days) ---")
    docker.run(["builder", "prune", "-a", "-f", "--filter", "until=168h"], check=True)
    print("--- Pruning unused Docker images ---")
    docker.run(["image", "prune", "-a", "-f"], check=True)

    pruned_net = _prune_network_endpoints(config.repo_root, runner)
    ran_gc = _run_registry_gc(docker)
    _vacuum_journal(systemd)
    _report_disk_space("after", runner)
    print("=== Gaia Storage Cleanup Finished ===")
    return CleanupResult(
        containers_only=False,
        pruned_network_endpoints=pruned_net,
        ran_registry_gc=ran_gc,
    )


def _prune_containers_and_volumes(docker: DockerPort) -> None:
    print("--- Pruning stopped/created Docker containers (older than 24h) ---")
    docker.run(["container", "prune", "-f", "--filter", "until=24h"], check=True)
    print("--- Pruning anonymous unused Docker volumes ---")
    # Omit -a/--all so named volumes are preserved (Docker 23+ prunes anonymous volumes only)
    docker.run(["volume", "prune", "-f"], check=True)


def _report_disk_space(phase: str, runner: CommandRunner) -> None:
    print(f"Disk space {phase} cleanup:")
    res = runner.run(["df", "-h", "/"], check=True)
    if res.stdout:
        print(res.stdout.rstrip())


def _prune_network_endpoints(repo_root: Path | None, runner: CommandRunner) -> bool:
    print("--- Pruning orphaned Docker Swarm overlay network endpoints ---")
    if runner.has_command("prune-network-endpoints"):
        runner.run(["prune-network-endpoints", "--all", "--force"], check=False)
        return True
    if repo_root is None:
        return False
    in_tree = repo_root / "network" / "prune_network_endpoints.py"
    if not in_tree.is_file():
        return False
    runner.run(["python3", str(in_tree), "--all", "--force"], check=False)
    return True


def _run_registry_gc(docker: DockerPort) -> bool:
    print("--- Pruning Hephaestus Docker Registry unreferenced blobs ---")
    ps_res = docker.run(
        ["ps", "-q", "--filter", "label=com.docker.swarm.service.name=hephaestus_registry"],
        check=False,
    )
    registry_cid = next((line.strip() for line in ps_res.stdout.splitlines() if line.strip()), "")
    if not registry_cid:
        print("Hephaestus registry container not running on this host; skipping registry GC.")
        return False
    print(f"Running registry garbage-collect inside container {registry_cid}...")
    docker.run(
        ["exec", registry_cid, "registry", "garbage-collect", "/etc/docker/registry/config.yml", "-m"],
        check=False,
    )
    return True


def _vacuum_journal(systemd: SystemdPort) -> None:
    print("--- Vacuuming systemd journal logs to 500M ---")
    res = systemd.vacuum_journal("500M")
    if res.returncode != 0:
        raise CommandExecutionError(
            ["journalctl", "--vacuum-size=500M"],
            res.returncode,
            res.stdout,
            res.stderr,
        )

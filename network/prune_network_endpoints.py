#!/usr/bin/env python3
"""
Prune Orphaned Docker Swarm Overlay Network Endpoints.

Audits overlay networks (e.g. aether-net) for leaked, stale, or orphaned
endpoints whose containers or Swarm tasks no longer exist, and force-disconnects
them to reclaim exhausted IPAM allocations without restarting the Docker daemon.

Zero external dependencies (pure Python standard library).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import subprocess
import sys
from typing import Any, Protocol


@dataclass(frozen=True)
class CommandResult:
    """Captured output and exit code from a Docker CLI invocation."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class EndpointRecord:
    """Stale or orphaned overlay endpoint selected for pruning."""

    cid: str
    endpoint_id: str
    name: str
    ipv4: str


@dataclass(frozen=True)
class NetworkAuditResult:
    """Audit and prune summary for a single Docker overlay network."""

    network: str
    subnet: str
    total: int
    active: int
    orphaned: int
    pruned: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize audit result using the legacy JSON output schema."""
        if self.error is not None:
            return {
                "network": self.network,
                "error": self.error,
                "orphaned": self.orphaned,
                "pruned": self.pruned,
            }
        return {
            "network": self.network,
            "subnet": self.subnet,
            "total": self.total,
            "active": self.active,
            "orphaned": self.orphaned,
            "pruned": self.pruned,
        }


@dataclass(frozen=True)
class PruneConfig:
    """Validated CLI options for overlay network endpoint auditing."""

    network: str
    all_networks: bool
    force: bool
    json_output: bool


class DockerAdapter(Protocol):
    """Port for executing Docker CLI commands with captured output."""

    def run(self, args: Sequence[str], *, timeout: float = 5.0) -> CommandResult:
        """Execute `docker <args...>` and return a CommandResult."""


class SubprocessDockerAdapter:
    """Production DockerAdapter backed by Python stdlib subprocess."""

    def run(self, args: Sequence[str], *, timeout: float = 5.0) -> CommandResult:
        """Execute `docker <args...>` and normalize timeouts/OS errors."""
        argv = ["docker", *args]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            return CommandResult(
                returncode=124,
                stdout=stdout,
                stderr=f"Command timed out after {timeout}s: {' '.join(argv)}",
            )
        except OSError as exc:
            return CommandResult(returncode=127, stdout="", stderr=str(exc))

        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    docker: DockerAdapter | None = None,
) -> int:
    """CLI entrypoint for auditing and pruning overlay network endpoints."""
    config = parse_args(argv)
    adapter = docker or SubprocessDockerAdapter()

    active_tasks = get_active_swarm_task_ids(adapter)
    local_cids = get_local_container_ids(adapter)
    targets = get_all_overlay_networks(adapter) if config.all_networks else [config.network]

    results = [
        audit_and_prune_network(
            net,
            active_tasks,
            local_cids,
            force=config.force,
            docker=adapter,
        )
        for net in targets
    ]

    if config.json_output:
        print(json.dumps([res.to_dict() for res in results], indent=2))

    _print_run_summary(results, force=config.force)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> PruneConfig:
    """Parse CLI arguments into a frozen PruneConfig."""
    parser = argparse.ArgumentParser(
        description=(
            "Audit and prune orphaned Docker Swarm overlay network endpoints "
            "to reclaim exhausted IPAM allocations."
        )
    )
    parser.add_argument(
        "network",
        nargs="?",
        default="aether-net",
        help="Target overlay network (default: aether-net)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit and prune across all overlay networks in the cluster",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Perform actual disconnection of orphaned endpoints (default is safe dry-run)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results summary as JSON",
    )
    ns = parser.parse_args(list(argv) if argv is not None else None)
    return PruneConfig(
        network=ns.network,
        all_networks=ns.all,
        force=ns.force,
        json_output=ns.json,
    )


def _print_run_summary(results: Sequence[NetworkAuditResult], *, force: bool) -> None:
    total_orphaned = sum(res.orphaned for res in results)
    total_pruned = sum(res.pruned for res in results)
    if not force and total_orphaned > 0:
        print(
            f"Dry-run complete: {total_orphaned} orphaned endpoint(s) found. "
            "Run with --force to release IP allocations."
        )
    elif force:
        print(
            f"Prune complete: {total_pruned} of {total_orphaned} "
            "orphaned endpoint(s) successfully released."
        )
    else:
        print("All audited overlay networks are clean.")


def audit_and_prune_network(
    net_name: str,
    active_tasks: set[str],
    local_cids: set[str],
    force: bool = False,
    docker: DockerAdapter | None = None,
) -> NetworkAuditResult:
    """Audit and optionally prune orphaned endpoints on a single network."""
    adapter = docker or SubprocessDockerAdapter()
    net_data = inspect_network(net_name, docker=adapter)
    if not net_data:
        return NetworkAuditResult(
            network=net_name,
            subnet="",
            total=0,
            active=0,
            orphaned=0,
            pruned=0,
            error="Inspect failed",
        )

    containers = net_data.get("Containers") or {}
    subnet, ips_in_use, ips_available = _extract_ipam_summary(net_data, len(containers))

    print(f"=== Network: {net_name} ({subnet}) ===")
    print(
        f"Total endpoints: {len(containers)} | "
        f"IPs in use: {ips_in_use} | Dynamic IPs available: {ips_available}"
    )

    active, orphaned = classify_endpoints(containers, active_tasks, local_cids)
    print(f"Active endpoints retained: {len(active)}")
    print(f"Orphaned endpoints found:   {len(orphaned)}")

    pruned_count = disconnect_orphaned_endpoints(net_name, orphaned, adapter, force=force)
    print()
    return NetworkAuditResult(
        network=net_name,
        subnet=subnet,
        total=len(containers),
        active=len(active),
        orphaned=len(orphaned),
        pruned=pruned_count,
    )


def classify_endpoints(
    containers: Mapping[str, Mapping[str, Any]],
    active_tasks: set[str],
    local_cids: set[str],
) -> tuple[list[tuple[str, str, str, str]], list[EndpointRecord]]:
    """Partition network container entries into active vs. orphaned endpoints."""
    active: list[tuple[str, str, str, str]] = []
    orphaned: list[EndpointRecord] = []

    for cid, info in containers.items():
        name = info.get("Name", "")
        ip = info.get("IPv4Address", "")
        ep_id = info.get("EndpointID", "")

        if name.startswith("lb-") or name.endswith("-endpoint"):
            active.append((cid, name, ip, "swarm-lb"))
            continue

        parts = name.split(".")
        task_id = parts[-1] if len(parts) >= 3 else None
        is_active_task = (task_id in active_tasks) if task_id else False
        is_local_present = (cid in local_cids) or (cid[:12] in local_cids)

        if is_active_task or is_local_present:
            reason = "active-task" if is_active_task else "local-container"
            active.append((cid, name, ip, reason))
        else:
            orphaned.append(EndpointRecord(cid=cid, endpoint_id=ep_id, name=name, ipv4=ip))

    return active, orphaned


def disconnect_orphaned_endpoints(
    net_name: str,
    orphaned: Sequence[EndpointRecord],
    docker: DockerAdapter | None = None,
    *,
    force: bool = False,
) -> int:
    """Log orphaned endpoints and force-disconnect them when force=True."""
    if not orphaned:
        return 0

    adapter = docker or SubprocessDockerAdapter()
    pruned_count = 0
    print("\nOrphaned endpoints:")
    for ep in orphaned:
        action = "[PRUNING] Disconnecting" if force else "[DRY-RUN] Would disconnect"
        print(f"  {action} {ep.ipv4:16} {ep.cid[:12]} ({ep.name}) ep:{ep.endpoint_id[:12]}")
        if not force:
            continue

        target = ep.endpoint_id or ep.cid
        res = adapter.run(["network", "disconnect", "-f", net_name, target], timeout=5.0)
        if res.returncode == 0:
            pruned_count += 1
            print(f"    ✓ Successfully released {ep.ipv4}")
        else:
            detail = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            print(f"    ✗ Failed to disconnect {target}: {detail}", file=sys.stderr)

    return pruned_count


def _extract_ipam_summary(
    net_data: Mapping[str, Any],
    container_count: int,
) -> tuple[str, Any, Any]:
    ipam_config = (net_data.get("IPAM") or {}).get("Config") or []
    subnet = ipam_config[0].get("Subnet", "") if ipam_config else ""
    subnets = ((net_data.get("Status") or {}).get("IPAM") or {}).get("Subnets") or {}
    ipam_status = subnets.get(subnet) or {}
    ips_in_use = ipam_status.get("IPsInUse", container_count)
    ips_available = ipam_status.get("DynamicIPsAvailable", "N/A")
    return subnet, ips_in_use, ips_available


def get_active_swarm_task_ids(docker: DockerAdapter | None = None) -> set[str]:
    """Return active Swarm task IDs whose desired state is running."""
    adapter = docker or SubprocessDockerAdapter()
    svc_res = adapter.run(["service", "ls", "-q"], timeout=5.0)
    if svc_res.returncode != 0:
        return set()

    services_out = svc_res.stdout.split()
    if not services_out:
        return set()

    cmd = [
        "service",
        "ps",
        *services_out,
        "--filter",
        "desired-state=running",
        "--format",
        "{{.ID}}",
    ]
    task_res = adapter.run(cmd, timeout=10.0)
    if task_res.returncode != 0:
        detail = task_res.stderr.strip() or f"exit code {task_res.returncode}"
        print(f"Warning: Failed to fetch swarm tasks: {detail}", file=sys.stderr)
        return set()

    return set(task_res.stdout.split())


def get_local_container_ids(docker: DockerAdapter | None = None) -> set[str]:
    """Return all local container IDs and short IDs (running or stopped)."""
    adapter = docker or SubprocessDockerAdapter()
    short_res = adapter.run(["ps", "-a", "-q"], timeout=5.0)
    if short_res.returncode != 0:
        detail = short_res.stderr.strip() or f"exit code {short_res.returncode}"
        print(f"Warning: Failed to fetch local containers: {detail}", file=sys.stderr)
        return set()

    full_res = adapter.run(["ps", "-a", "-q", "--no-trunc"], timeout=5.0)
    if full_res.returncode != 0:
        detail = full_res.stderr.strip() or f"exit code {full_res.returncode}"
        print(f"Warning: Failed to fetch local containers: {detail}", file=sys.stderr)
        return set()

    return set(short_res.stdout.split()).union(set(full_res.stdout.split()))


def get_all_overlay_networks(docker: DockerAdapter | None = None) -> list[str]:
    """Return all non-ingress overlay network names in the cluster."""
    adapter = docker or SubprocessDockerAdapter()
    res = adapter.run(
        ["network", "ls", "--filter", "driver=overlay", "--format", "{{.Name}}"],
        timeout=5.0,
    )
    if res.returncode != 0:
        detail = res.stderr.strip() or f"exit code {res.returncode}"
        print(f"Error listing overlay networks: {detail}", file=sys.stderr)
        return []
    return [net for net in res.stdout.split() if net != "ingress"]


def inspect_network(
    net_name: str,
    docker: DockerAdapter | None = None,
) -> dict[str, Any] | None:
    """Inspect a Docker network and return its decoded JSON object."""
    adapter = docker or SubprocessDockerAdapter()
    res = adapter.run(["network", "inspect", net_name], timeout=5.0)
    if res.returncode != 0:
        detail = res.stderr.strip() or f"exit code {res.returncode}"
        print(f"Error inspecting network '{net_name}': {detail}", file=sys.stderr)
        return None

    try:
        parsed = json.loads(res.stdout)
        return parsed[0]
    except (json.JSONDecodeError, IndexError, TypeError, KeyError) as exc:
        print(f"Error parsing network '{net_name}': {exc}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())

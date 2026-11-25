#!/usr/bin/env python3
"""Deterministic unit tests for the standalone overlay network endpoint pruner."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Callable, Sequence
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
NETWORK_DIR = REPO_ROOT / "network"
if str(NETWORK_DIR) not in sys.path:
    sys.path.insert(0, str(NETWORK_DIR))

import prune_network_endpoints as pne  # noqa: E402


class FakeDockerAdapter:
    """In-memory DockerAdapter recording commands and replaying scripted responses."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], pne.CommandResult] | None = None,
        responder: Callable[[tuple[str, ...], float], pne.CommandResult] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.responder = responder
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, args: Sequence[str], *, timeout: float = 5.0) -> pne.CommandResult:
        key = tuple(args)
        self.calls.append((key, timeout))
        if self.responder is not None:
            return self.responder(key, timeout)
        if key in self.responses:
            return self.responses[key]
        return pne.CommandResult(returncode=1, stdout="", stderr=f"Unexpected command: {key}")


def _build_inspect_payload(
    *,
    subnet: str = "10.0.1.0/24",
    ips_in_use: int = 12,
    available: int = 240,
    containers: dict[str, dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        [
            {
                "IPAM": {"Config": [{"Subnet": subnet}]},
                "Status": {
                    "IPAM": {
                        "Subnets": {
                            subnet: {
                                "IPsInUse": ips_in_use,
                                "DynamicIPsAvailable": available,
                            }
                        }
                    }
                },
                "Containers": containers if containers is not None else {},
            }
        ]
    )


class TestDockerQueryHelpers(unittest.TestCase):
    def test_get_active_swarm_task_ids_empty_services_skips_service_ps(self) -> None:
        docker = FakeDockerAdapter(
            {("service", "ls", "-q"): pne.CommandResult(returncode=0, stdout="", stderr="")}
        )
        self.assertEqual(pne.get_active_swarm_task_ids(docker), set())
        self.assertEqual([cmd for cmd, _ in docker.calls], [("service", "ls", "-q")])

    def test_get_active_swarm_task_ids_returns_running_tasks(self) -> None:
        docker = FakeDockerAdapter(
            {
                ("service", "ls", "-q"): pne.CommandResult(returncode=0, stdout="svc1\nsvc2\n", stderr=""),
                (
                    "service",
                    "ps",
                    "svc1",
                    "svc2",
                    "--filter",
                    "desired-state=running",
                    "--format",
                    "{{.ID}}",
                ): pne.CommandResult(returncode=0, stdout="taskA\ntaskB\n", stderr=""),
            }
        )
        self.assertEqual(pne.get_active_swarm_task_ids(docker), {"taskA", "taskB"})

    def test_get_active_swarm_task_ids_warns_and_returns_empty_on_service_ps_failure(self) -> None:
        docker = FakeDockerAdapter(
            {
                ("service", "ls", "-q"): pne.CommandResult(returncode=0, stdout="svc1\n", stderr=""),
                (
                    "service",
                    "ps",
                    "svc1",
                    "--filter",
                    "desired-state=running",
                    "--format",
                    "{{.ID}}",
                ): pne.CommandResult(returncode=1, stdout="", stderr="swarm rpc error"),
            }
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            tasks = pne.get_active_swarm_task_ids(docker)
        self.assertEqual(tasks, set())
        self.assertIn("Warning: Failed to fetch swarm tasks: swarm rpc error", stderr.getvalue())

    def test_get_local_container_ids_combines_short_and_full_ids(self) -> None:
        docker = FakeDockerAdapter(
            {
                ("ps", "-a", "-q"): pne.CommandResult(returncode=0, stdout="abc123def456\n", stderr=""),
                ("ps", "-a", "-q", "--no-trunc"): pne.CommandResult(
                    returncode=0,
                    stdout="abc123def4567890fedcba0987654321\n",
                    stderr="",
                ),
            }
        )
        self.assertEqual(
            pne.get_local_container_ids(docker),
            {"abc123def456", "abc123def4567890fedcba0987654321"},
        )

    def test_get_all_overlay_networks_excludes_ingress_and_handles_error(self) -> None:
        docker = FakeDockerAdapter(
            {
                ("network", "ls", "--filter", "driver=overlay", "--format", "{{.Name}}"): pne.CommandResult(
                    returncode=0,
                    stdout="ingress\naether-net\nmonitoring-net\n",
                    stderr="",
                )
            }
        )
        self.assertEqual(pne.get_all_overlay_networks(docker), ["aether-net", "monitoring-net"])

        failing = FakeDockerAdapter(
            {
                ("network", "ls", "--filter", "driver=overlay", "--format", "{{.Name}}"): pne.CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="daemon unavailable",
                )
            }
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(pne.get_all_overlay_networks(failing), [])
        self.assertIn("Error listing overlay networks: daemon unavailable", stderr.getvalue())


class TestClassificationAndPruning(unittest.TestCase):
    def test_classify_endpoints_protects_lb_active_tasks_and_local_containers(self) -> None:
        containers = {
            "lb1111111111": {"Name": "lb-aether-net", "IPv4Address": "10.0.1.2/24", "EndpointID": "ep-lb1"},
            "ep2222222222": {"Name": "aether-net-endpoint", "IPv4Address": "10.0.1.3/24", "EndpointID": "ep-lb2"},
            "task3333333333": {"Name": "odin_hermes.1.liveTask99", "IPv4Address": "10.0.1.4/24", "EndpointID": "ep-t1"},
            "full444444444455556666": {"Name": "standalone-full", "IPv4Address": "10.0.1.5/24", "EndpointID": "ep-f1"},
            "short7777888899990000": {"Name": "standalone-short", "IPv4Address": "10.0.1.6/24", "EndpointID": "ep-s1"},
            "stale_cid_0001": {"Name": "odin_hermes.1.deadTask01", "IPv4Address": "10.0.1.7/24", "EndpointID": "ep-dead1"},
        }
        active, orphaned = pne.classify_endpoints(
            containers,
            active_tasks={"liveTask99"},
            local_cids={"full444444444455556666", "short7777888"},
        )
        self.assertEqual(len(active), 5)
        self.assertEqual(
            [item[3] for item in active],
            ["swarm-lb", "swarm-lb", "active-task", "local-container", "local-container"],
        )
        self.assertEqual(
            orphaned,
            [
                pne.EndpointRecord(
                    cid="stale_cid_0001",
                    endpoint_id="ep-dead1",
                    name="odin_hermes.1.deadTask01",
                    ipv4="10.0.1.7/24",
                )
            ],
        )

    def test_audit_dry_run_skips_disconnect_and_extracts_ipam_summary(self) -> None:
        payload = _build_inspect_payload(
            subnet="10.0.1.0/24",
            ips_in_use=9,
            available=245,
            containers={
                "lb1": {"Name": "lb-aether-net", "IPv4Address": "10.0.1.2/24", "EndpointID": "ep-lb"},
                "dead1234567890": {
                    "Name": "kratos.1.staleTask",
                    "IPv4Address": "10.0.1.44/24",
                    "EndpointID": "ep-stale123456",
                },
            },
        )
        docker = FakeDockerAdapter(
            {("network", "inspect", "aether-net"): pne.CommandResult(returncode=0, stdout=payload, stderr="")}
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = pne.audit_and_prune_network("aether-net", set(), set(), force=False, docker=docker)

        self.assertEqual(
            result,
            pne.NetworkAuditResult(
                network="aether-net",
                subnet="10.0.1.0/24",
                total=2,
                active=1,
                orphaned=1,
                pruned=0,
            ),
        )
        self.assertEqual(
            result.to_dict(),
            {
                "network": "aether-net",
                "subnet": "10.0.1.0/24",
                "total": 2,
                "active": 1,
                "orphaned": 1,
                "pruned": 0,
            },
        )
        self.assertEqual([cmd for cmd, _ in docker.calls], [("network", "inspect", "aether-net")])
        text = stdout.getvalue()
        self.assertIn("IPs in use: 9 | Dynamic IPs available: 245", text)
        self.assertIn("[DRY-RUN] Would disconnect", text)

    def test_force_disconnect_uses_endpoint_id_fallback_cid_and_counts_partial_success(self) -> None:
        orphaned = [
            pne.EndpointRecord(cid="cid-1", endpoint_id="ep-1", name="svc.1.dead1", ipv4="10.0.1.10/24"),
            pne.EndpointRecord(cid="cid-2", endpoint_id="", name="svc.1.dead2", ipv4="10.0.1.11/24"),
            pne.EndpointRecord(cid="cid-3", endpoint_id="ep-3", name="svc.1.dead3", ipv4="10.0.1.12/24"),
        ]
        docker = FakeDockerAdapter(
            {
                ("network", "disconnect", "-f", "aether-net", "ep-1"): pne.CommandResult(0, "", ""),
                ("network", "disconnect", "-f", "aether-net", "cid-2"): pne.CommandResult(1, "", "endpoint busy"),
                ("network", "disconnect", "-f", "aether-net", "ep-3"): pne.CommandResult(0, "", ""),
            }
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            pruned = pne.disconnect_orphaned_endpoints("aether-net", orphaned, docker, force=True)

        self.assertEqual(pruned, 2)
        self.assertEqual(
            [cmd for cmd, _ in docker.calls],
            [
                ("network", "disconnect", "-f", "aether-net", "ep-1"),
                ("network", "disconnect", "-f", "aether-net", "cid-2"),
                ("network", "disconnect", "-f", "aether-net", "ep-3"),
            ],
        )
        self.assertIn("Failed to disconnect cid-2: endpoint busy", stderr.getvalue())

    def test_inspect_network_failure_and_invalid_json_return_error_result(self) -> None:
        for res in (
            pne.CommandResult(returncode=1, stdout="", stderr="No such network: ghost-net"),
            pne.CommandResult(returncode=0, stdout="{invalid-json", stderr=""),
        ):
            docker = FakeDockerAdapter({("network", "inspect", "ghost-net"): res})
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                audit = pne.audit_and_prune_network("ghost-net", set(), set(), docker=docker)
            self.assertEqual(audit.error, "Inspect failed")
            self.assertEqual(
                audit.to_dict(),
                {"network": "ghost-net", "error": "Inspect failed", "orphaned": 0, "pruned": 0},
            )


class TestMainCliAndHygiene(unittest.TestCase):
    def test_main_all_with_json_outputs_valid_schema_and_excludes_ingress(self) -> None:
        net_a = _build_inspect_payload(subnet="10.0.1.0/24", ips_in_use=1, available=250, containers={})
        net_b = _build_inspect_payload(
            subnet="10.0.2.0/24",
            ips_in_use=1,
            available=250,
            containers={
                "orphan123456": {"Name": "old_svc.1.oldTask", "IPv4Address": "10.0.2.5/24", "EndpointID": "ep-old"}
            },
        )
        docker = FakeDockerAdapter(
            {
                ("service", "ls", "-q"): pne.CommandResult(0, "", ""),
                ("ps", "-a", "-q"): pne.CommandResult(0, "", ""),
                ("ps", "-a", "-q", "--no-trunc"): pne.CommandResult(0, "", ""),
                ("network", "ls", "--filter", "driver=overlay", "--format", "{{.Name}}"): pne.CommandResult(
                    0, "ingress\naether-net\nmonitoring-net\n", ""
                ),
                ("network", "inspect", "aether-net"): pne.CommandResult(0, net_a, ""),
                ("network", "inspect", "monitoring-net"): pne.CommandResult(0, net_b, ""),
            }
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = pne.main(["--all", "--json"], docker=docker)

        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        json_start = text.index("[\n")
        json_end = text.index("\n]", json_start) + 2
        parsed = json.loads(text[json_start:json_end])
        self.assertEqual(
            parsed,
            [
                {
                    "network": "aether-net",
                    "subnet": "10.0.1.0/24",
                    "total": 0,
                    "active": 0,
                    "orphaned": 0,
                    "pruned": 0,
                },
                {
                    "network": "monitoring-net",
                    "subnet": "10.0.2.0/24",
                    "total": 1,
                    "active": 0,
                    "orphaned": 1,
                    "pruned": 0,
                },
            ],
        )
        self.assertIn("Dry-run complete: 1 orphaned endpoint(s) found.", text)

    def test_main_defaults_to_aether_net_when_no_target_passed(self) -> None:
        docker = FakeDockerAdapter(
            {
                ("service", "ls", "-q"): pne.CommandResult(0, "", ""),
                ("ps", "-a", "-q"): pne.CommandResult(0, "", ""),
                ("ps", "-a", "-q", "--no-trunc"): pne.CommandResult(0, "", ""),
                ("network", "inspect", "aether-net"): pne.CommandResult(0, _build_inspect_payload(), ""),
            }
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = pne.main([], docker=docker)

        self.assertEqual(rc, 0)
        self.assertIn(("network", "inspect", "aether-net"), [cmd for cmd, _ in docker.calls])
        self.assertIn("All audited overlay networks are clean.", stdout.getvalue())

    def test_subprocess_docker_adapter_normalizes_timeout_and_os_error(self) -> None:
        adapter = pne.SubprocessDockerAdapter()
        with mock.patch(
            "prune_network_endpoints.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["docker", "ps"], timeout=5.0),
        ):
            timed_out = adapter.run(["ps"], timeout=5.0)
        self.assertNotEqual(timed_out.returncode, 0)
        self.assertIn("timed out", timed_out.stderr)

        with mock.patch("prune_network_endpoints.subprocess.run", side_effect=FileNotFoundError("no docker")):
            missing = adapter.run(["ps"], timeout=5.0)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("no docker", missing.stderr)

    def test_dead_wrapper_removed_permissions_valid_and_standalone_help_succeeds(self) -> None:
        dead_wrapper = REPO_ROOT / "hosts" / "scripts" / "prune-network-endpoints.sh"
        self.assertFalse(dead_wrapper.exists())

        script_path = NETWORK_DIR / "prune_network_endpoints.py"
        mode = stat.S_IMODE(script_path.stat().st_mode)
        self.assertEqual(mode & 0o111, 0o111)
        self.assertEqual(mode & 0o002, 0)

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = ""
        completed = subprocess.run(
            [sys.executable, str(script_path), "--help"],
            cwd="/tmp",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        self.assertIn("aether-net", completed.stdout)


if __name__ == "__main__":
    unittest.main()

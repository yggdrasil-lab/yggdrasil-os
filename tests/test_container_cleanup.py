#!/usr/bin/env python3
"""Unit tests for Gaia storage cleanup and Swarm startup orchestrator."""

from __future__ import annotations

import configparser
import contextlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from yggdrasil_os import (  # noqa: E402
    CleanupConfig,
    CommandExecutionError,
    CommandResult,
    StartupContext,
    cleanup_storage,
    cli,
    parse_cleanup_args,
    run_startup_orchestrator,
)

CLEANUP_SCRIPT = REPO_ROOT / "maintenance" / "cleanup_storage.sh"
INSTALL_TIMER_SCRIPT = REPO_ROOT / "maintenance" / "install_cleanup_timer.sh"
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"
SYSTEMD_DIR = REPO_ROOT / "maintenance" / "systemd"
MONTHLY_CLEANUP_SERVICE = SYSTEMD_DIR / "gaia-cleanup.service"
MONTHLY_CLEANUP_TIMER = SYSTEMD_DIR / "gaia-cleanup.timer"
DAILY_PRUNE_SERVICE = SYSTEMD_DIR / "gaia-container-prune.service"
DAILY_PRUNE_TIMER = SYSTEMD_DIR / "gaia-container-prune.timer"
STARTUP_SCRIPT = REPO_ROOT / "swarm" / "startup_orchestrator.sh"


def _parse_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCommandRunner:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        available_commands: set[str] | None = None,
        handler: Callable[[tuple[str, ...]], CommandResult] | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.available_commands = available_commands if available_commands is not None else set()
        self._handler = handler

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        cmd = tuple(argv)
        self.calls.append(" ".join(cmd))
        res = self._handler(cmd) if self._handler else CommandResult(0, "", "")
        if check and res.returncode != 0:
            raise CommandExecutionError(cmd, res.returncode, res.stdout, res.stderr)
        return res

    def has_command(self, name: str) -> bool:
        return name in self.available_commands


class FakeDockerPort:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        handler: Callable[[tuple[str, ...]], CommandResult] | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self._handler = handler

    def run(
        self,
        args: Sequence[str],
        *,
        stdin_text: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = False,
    ) -> CommandResult:
        cmd = tuple(args)
        self.calls.append("docker " + " ".join(cmd))
        res = self._handler(cmd) if self._handler else CommandResult(0, "", "")
        if check and res.returncode != 0:
            raise CommandExecutionError(("docker", *cmd), res.returncode, res.stdout, res.stderr)
        return res


class FakeSystemdPort:
    def __init__(self, *, calls: list[str] | None = None, vacuum_rc: int = 0) -> None:
        self.calls = calls if calls is not None else []
        self.vacuum_rc = vacuum_rc

    def stop_units(self, units: Sequence[str]) -> CommandResult:
        self.calls.append("systemctl stop " + " ".join(units))
        return CommandResult(0, "", "")

    def start_unit(self, unit: str) -> CommandResult:
        self.calls.append(f"systemctl start {unit}")
        return CommandResult(0, "", "")

    def vacuum_journal(self, max_size: str) -> CommandResult:
        self.calls.append(f"journalctl --vacuum-size={max_size}")
        return CommandResult(self.vacuum_rc, "", "vacuum error" if self.vacuum_rc else "")


class TestBashSyntax(unittest.TestCase):
    def test_bash_syntax_valid(self) -> None:
        for script in (
            CLEANUP_SCRIPT,
            INSTALL_TIMER_SCRIPT,
            SETUP_GAIA_SCRIPT,
            STARTUP_SCRIPT,
        ):
            with self.subTest(script=script.name):
                res = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"bash -n failed for {script}: {res.stderr}",
                )


class TestCleanupStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-cleanup-storage-"))
        self.calls: list[str] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prunes_containers_and_anonymous_volumes_before_builder_and_image_prune(
        self,
    ) -> None:
        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[:2] == ("ps", "-q"):
                return CommandResult(0, "reg-cid-1\n", "")
            return CommandResult(0, "", "")

        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        systemd = FakeSystemdPort(calls=self.calls)
        runner = FakeCommandRunner(
            calls=self.calls,
            available_commands={"prune-network-endpoints"},
            handler=lambda cmd: CommandResult(0, "/dev/nvme0n1p2 900G 200G 700G 23% /\n", "")
            if cmd[0] == "df"
            else CommandResult(0, "", ""),
        )

        with contextlib.redirect_stdout(io.StringIO()):
            result = cleanup_storage(
                CleanupConfig(containers_only=False, repo_root=REPO_ROOT),
                docker=docker,
                systemd=systemd,
                runner=runner,
            )

        self.assertFalse(result.containers_only)
        self.assertTrue(result.pruned_network_endpoints)
        self.assertTrue(result.ran_registry_gc)

        self.assertEqual(
            self.calls,
            [
                "df -h /",
                "docker container prune -f --filter until=24h",
                "docker volume prune -f",
                "docker builder prune -a -f --filter until=168h",
                "docker image prune -a -f",
                "prune-network-endpoints --all --force",
                "docker ps -q --filter label=com.docker.swarm.service.name=hephaestus_registry",
                "docker exec reg-cid-1 registry garbage-collect /etc/docker/registry/config.yml -m",
                "journalctl --vacuum-size=500M",
                "df -h /",
            ],
        )
        for call in [c for c in self.calls if c.startswith("docker volume prune")]:
            tokens = call.split()
            self.assertNotIn("-a", tokens)
            self.assertNotIn("--all", tokens)

    def test_in_tree_network_prune_fallback_and_absent_registry_container(self) -> None:
        fake_repo = self.tmpdir / "repo"
        (fake_repo / "network").mkdir(parents=True)
        in_tree_prune = fake_repo / "network" / "prune_network_endpoints.py"
        in_tree_prune.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        docker = FakeDockerPort(calls=self.calls)
        systemd = FakeSystemdPort(calls=self.calls)
        runner = FakeCommandRunner(calls=self.calls, available_commands=set())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = cleanup_storage(
                CleanupConfig(containers_only=False, repo_root=fake_repo),
                docker=docker,
                systemd=systemd,
                runner=runner,
            )
        self.assertTrue(result.pruned_network_endpoints)
        self.assertFalse(result.ran_registry_gc)
        self.assertIn(f"python3 {in_tree_prune} --all --force", self.calls)
        self.assertFalse(any(c.startswith("docker exec ") for c in self.calls))
        self.assertIn("skipping registry GC", buf.getvalue())

    def test_containers_only_prunes_containers_and_anonymous_volumes_only(self) -> None:
        docker = FakeDockerPort(calls=self.calls)
        systemd = FakeSystemdPort(calls=self.calls)
        runner = FakeCommandRunner(calls=self.calls, available_commands={"prune-network-endpoints"})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = cleanup_storage(
                CleanupConfig(containers_only=True, repo_root=REPO_ROOT),
                docker=docker,
                systemd=systemd,
                runner=runner,
            )
        self.assertTrue(result.containers_only)
        self.assertFalse(result.pruned_network_endpoints)
        self.assertFalse(result.ran_registry_gc)
        self.assertEqual(
            self.calls,
            [
                "df -h /",
                "docker container prune -f --filter until=24h",
                "docker volume prune -f",
                "df -h /",
            ],
        )
        self.assertIn("Gaia Containers-Only Cleanup Finished", buf.getvalue())

    def test_unknown_flag_fails_loudly_without_running_cleanup(self) -> None:
        for bad_args in (["--bogus"], ["--containers-only", "--bogus"], ["unexpected"]):
            with self.subTest(args=bad_args):
                with self.assertRaisesRegex(ValueError, "Unknown argument"):
                    parse_cleanup_args(bad_args)

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            rc = cli.main(["cleanup-storage", "--bogus"])
        self.assertEqual(rc, 1)
        self.assertIn("Unknown argument", err_buf.getvalue())
        self.assertIn("--bogus", err_buf.getvalue())


class TestContainerPruneUnitsAndInstaller(unittest.TestCase):
    def test_daily_container_prune_service_and_timer_units(self) -> None:
        self.assertTrue(DAILY_PRUNE_SERVICE.is_file(), f"Missing {DAILY_PRUNE_SERVICE}")
        self.assertTrue(DAILY_PRUNE_TIMER.is_file(), f"Missing {DAILY_PRUNE_TIMER}")

        daily_svc = _parse_ini(DAILY_PRUNE_SERVICE)
        self.assertEqual(daily_svc.get("Unit", "After"), "docker.service")
        self.assertEqual(daily_svc.get("Unit", "Wants"), "docker.service")
        self.assertEqual(daily_svc.get("Service", "Type"), "oneshot")
        self.assertEqual(
            daily_svc.get("Service", "ExecStart"),
            "/usr/local/bin/gaia-cleanup.sh --containers-only",
        )

        daily_tmr = _parse_ini(DAILY_PRUNE_TIMER)
        self.assertEqual(daily_tmr.get("Timer", "OnCalendar"), "*-*-* 04:30:00")
        self.assertEqual(daily_tmr.get("Timer", "RandomizedDelaySec"), "15m")
        self.assertEqual(daily_tmr.get("Timer", "Persistent"), "true")
        self.assertEqual(daily_tmr.get("Timer", "Unit"), "gaia-container-prune.service")
        self.assertEqual(daily_tmr.get("Install", "WantedBy"), "timers.target")

        monthly_svc = _parse_ini(MONTHLY_CLEANUP_SERVICE)
        self.assertEqual(monthly_svc.get("Service", "ExecStart"), "/usr/local/bin/gaia-cleanup.sh")
        monthly_tmr = _parse_ini(MONTHLY_CLEANUP_TIMER)
        self.assertEqual(monthly_tmr.get("Timer", "OnCalendar"), "*-*-01 03:00:00")
        self.assertEqual(monthly_tmr.get("Timer", "Persistent"), "true")


class TestStartupOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []

    def test_permission_gate_and_readiness_timeout(self) -> None:
        docker_denied = FakeDockerPort(
            calls=self.calls,
            handler=lambda _cmd: CommandResult(1, "", "permission denied"),
        )
        with self.assertRaisesRegex(PermissionError, "docker"):
            run_startup_orchestrator(
                StartupContext(euid=1000),
                docker=docker_denied,
                clock=FakeClock(),
            )
        self.assertEqual(self.calls, ["docker info"])

        self.calls.clear()
        docker_group = FakeDockerPort(
            calls=self.calls,
            handler=lambda cmd: CommandResult(1, "", "") if cmd[:2] == ("service", "inspect") else CommandResult(0, "", ""),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            res_group = run_startup_orchestrator(
                StartupContext(euid=1000),
                docker=docker_group,
                clock=FakeClock(),
            )
        self.assertTrue(res_group.converged)

        timeout_clock = FakeClock()
        docker_down = FakeDockerPort(
            handler=lambda _cmd: CommandResult(1, "", "daemon down"),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "60 seconds"):
                run_startup_orchestrator(
                    StartupContext(euid=0),
                    docker=docker_down,
                    clock=timeout_clock,
                )
        self.assertEqual(sum(timeout_clock.sleeps), 60)

    def test_core_services_convergence_auto_heal_and_batch_filtering(self) -> None:
        state = {
            "adguard_polls": 0,
            "converge_polls": 0,
            "healed": False,
        }

        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd == ("service", "inspect", "olympus_adguardhome"):
                return CommandResult(0, "ok\n", "")
            if cmd == ("service", "inspect", "olympus_traefik"):
                return CommandResult(0, "ok\n", "")
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(1, "", "not found")
            if cmd == ("service", "ls", "--format", "{{.Name}} {{.Replicas}}"):
                state["adguard_polls"] += 1
                adguard_rep = "0/1" if state["adguard_polls"] == 1 else "1/1"
                return CommandResult(0, f"olympus_adguardhome {adguard_rep}\nolympus_traefik 0/1\n", "")
            if cmd == ("service", "ls", "--format", "{{.Name}}\t{{.Replicas}}"):
                state["converge_polls"] += 1
                app_rep = "1/1" if state["healed"] else "0/1"
                return CommandResult(0, f"app_web\t{app_rep}\nbatch_job\t0/1\n", "")
            if cmd[:3] == ("service", "ps", "batch_job"):
                return CommandResult(0, "", "")
            if cmd == ("service", "ps", "app_web", "--filter", "desired-state=running", "-q"):
                return CommandResult(0, "task-1\n", "")
            if cmd == ("service", "ps", "app_web", "--no-trunc", "--format", "{{.Error}}"):
                return CommandResult(0, "No such image: registry.local/app:latest\n", "")
            if cmd == ("service", "update", "--force", "app_web"):
                state["healed"] = True
                return CommandResult(0, "", "")
            if cmd[:2] == ("node", "ls"):
                return CommandResult(0, "node1 * gaia Ready Active Leader\n", "")
            return CommandResult(0, "", "")

        clock = FakeClock()
        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_startup_orchestrator(
                StartupContext(euid=0),
                docker=docker,
                clock=clock,
            )

        self.assertIn("docker swarm update --task-history-limit 1", self.calls)
        self.assertEqual(result.core_ready, ("olympus_adguardhome",))
        self.assertEqual(result.core_timed_out, ("olympus_traefik",))
        self.assertTrue(result.converged)
        self.assertEqual(result.healed_services, ("app_web",))
        self.assertIn("docker service update --force app_web", self.calls)
        self.assertNotIn("docker service update --force batch_job", self.calls)

    def test_created_only_containers_trigger_prune_after_gaia_runner_guard(self) -> None:
        state = {
            "runner_running": False,
            "created": ["cid-created-1"],
        }

        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(1, "", "")
            if cmd == ("ps", "-a", "--format", "{{.Names}}"):
                return CommandResult(0, "gaia-runner\n", "")
            if cmd == ("inspect", "-f", "{{.State.Running}}", "gaia-runner"):
                return CommandResult(0, "true\n" if state["runner_running"] else "false\n", "")
            if cmd == ("start", "gaia-runner"):
                state["runner_running"] = True
                return CommandResult(0, "", "")
            if cmd == ("ps", "-a", "--filter", "status=created", "--filter", "status=exited", "--filter", "status=dead", "-q"):
                return CommandResult(0, "\n".join(state["created"]), "")
            if cmd == ("container", "prune", "-f"):
                state["created"] = []
                return CommandResult(0, "", "")
            if cmd == ("ps", "-q", "--filter", "status=running"):
                return CommandResult(0, "cid-gaia-runner\n" if state["runner_running"] else "", "")
            return CommandResult(0, "", "")

        buf = io.StringIO()
        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        with contextlib.redirect_stdout(buf):
            result = run_startup_orchestrator(
                StartupContext(euid=0),
                docker=docker,
                clock=FakeClock(),
            )

        self.assertTrue(result.started_runner)
        self.assertTrue(result.pruned_stopped_containers)
        self.assertEqual(result.running_count, 1)
        self.assertEqual(result.stopped_count, 0)

        self.assertIn("docker start gaia-runner", self.calls)
        self.assertIn("docker container prune -f", self.calls)
        self.assertIn("docker volume prune -f", self.calls)
        for call in [c for c in self.calls if c.startswith("docker volume prune")]:
            tokens = call.split()
            self.assertNotIn("-a", tokens)
            self.assertNotIn("--all", tokens)

        self.assertLess(
            self.calls.index("docker start gaia-runner"),
            self.calls.index("docker container prune -f"),
        )
        self.assertLess(
            self.calls.index("docker container prune -f"),
            self.calls.index("docker volume prune -f"),
        )
        ps_created_calls = [
            c
            for c in self.calls
            if c == "docker ps -a --filter status=created --filter status=exited --filter status=dead -q"
        ]
        self.assertEqual(len(ps_created_calls), 2)
        self.assertIn("Stopped/Dead Containers:   0", buf.getvalue())

    def test_dead_containers_invoke_rm_f_v_and_volume_prune(self) -> None:
        state = {"dead": ["cid-dead-1", "cid-dead-2"]}

        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(1, "", "")
            if cmd == ("ps", "-a", "--format", "{{.Names}}"):
                return CommandResult(0, "gaia-runner\n", "")
            if cmd == ("inspect", "-f", "{{.State.Running}}", "gaia-runner"):
                return CommandResult(0, "true\n", "")
            if cmd == ("ps", "-a", "--filter", "status=created", "--filter", "status=exited", "--filter", "status=dead", "-q"):
                return CommandResult(0, "\n".join(state["dead"]), "")
            if cmd == ("ps", "-a", "--filter", "status=dead", "-q"):
                return CommandResult(0, "\n".join(state["dead"]), "")
            if cmd[:3] == ("rm", "-f", "-v"):
                state["dead"] = []
                return CommandResult(0, "", "")
            return CommandResult(0, "", "")

        buf = io.StringIO()
        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        with contextlib.redirect_stdout(buf):
            result = run_startup_orchestrator(
                StartupContext(euid=0),
                docker=docker,
                clock=FakeClock(),
            )
        self.assertFalse(result.started_runner)
        self.assertTrue(result.pruned_stopped_containers)
        self.assertNotIn("docker start gaia-runner", self.calls)
        self.assertLess(
            self.calls.index("docker container prune -f"),
            self.calls.index("docker rm -f -v cid-dead-1 cid-dead-2"),
        )
        self.assertLess(
            self.calls.index("docker rm -f -v cid-dead-1 cid-dead-2"),
            self.calls.index("docker volume prune -f"),
        )
        self.assertIn("Stopped/Dead Containers:   0", buf.getvalue())

    def test_no_stopped_containers_skips_pruning(self) -> None:
        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(1, "", "")
            return CommandResult(0, "", "")

        buf = io.StringIO()
        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        with contextlib.redirect_stdout(buf):
            result = run_startup_orchestrator(
                StartupContext(euid=0),
                docker=docker,
                clock=FakeClock(),
            )
        self.assertFalse(result.pruned_stopped_containers)
        self.assertNotIn("docker container prune -f", self.calls)
        self.assertNotIn("docker volume prune -f", self.calls)
        self.assertFalse(any(c.startswith("docker rm ") for c in self.calls))
        self.assertIn("No dead/stopped containers found.", buf.getvalue())


class TestCleanupAndStartupShims(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-cleanup-shims-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.archive_tmp = self.tmpdir / "archive_tmp"
        self.archive_tmp.mkdir()
        self.py_log = self.tmpdir / "python.log"

        mock_py = self.mock_bin / "python3"
        mock_py.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "import yggdrasil_os\n"
            f"with open({str(self.py_log)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'argv': sys.argv[1:], 'module_file': yggdrasil_os.__file__}) + '\\n')\n",
            encoding="utf-8",
        )
        mock_py.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_shims_allow_non_root_resolve_in_tree_and_clean_tarball_tmp(self) -> None:
        for script, expected_cmd, extra_args in (
            (STARTUP_SCRIPT, ["startup-orchestrator"], []),
            (CLEANUP_SCRIPT, ["cleanup-storage"], ["--containers-only"]),
        ):
            with self.subTest(script=script.name):
                mode = stat.S_IMODE(script.stat().st_mode)
                self.assertEqual(mode & 0o111, 0o111)
                self.assertEqual(mode & 0o002, 0)
                self.py_log.unlink(missing_ok=True)
                res = subprocess.run(
                    ["bash", str(script), *extra_args],
                    env={"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                entry = json.loads(self.py_log.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", *expected_cmd, *extra_args])
                self.assertEqual(
                    Path(entry["module_file"]).resolve(),
                    (REPO_ROOT / "yggdrasil_os" / "__init__.py").resolve(),
                )

        archive_path = self.tmpdir / "yggdrasil-os-main.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(REPO_ROOT / "yggdrasil_os", arcname="yggdrasil-os-main/yggdrasil_os")
        detached = self.tmpdir / "cleanup_storage.sh"
        shutil.copy2(CLEANUP_SCRIPT, detached)
        detached.chmod(0o755)
        res = subprocess.run(
            ["bash", str(detached), "--bogus"],
            env={
                "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
                "TMPDIR": str(self.archive_tmp),
                "YGGDRASIL_OS_ARCHIVE_URL": archive_path.as_uri(),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("Unknown argument", res.stderr)
        self.assertEqual(list(self.archive_tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

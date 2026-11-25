#!/usr/bin/env python3
"""Unit tests for post-deploy convergence verification (seam + shim smoke)."""

from __future__ import annotations

import contextlib
import io
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from yggdrasil_os import (  # noqa: E402
    CommandResult,
    ConvergenceConfigError,
    ConvergenceFailureError,
    cli,
    parse_convergence_args,
    verify_convergence,
)

VERIFY_SCRIPT = REPO_ROOT / "deploy" / "verify_convergence.sh"


def _ok(stdout: str = "") -> CommandResult:
    return CommandResult(0, stdout, "")


def _err(stderr: str, rc: int = 1) -> CommandResult:
    return CommandResult(rc, "", stderr)


class FakeClock:
    """Deterministic monotonic clock recording sleep durations."""

    def __init__(self, *, start: float = 1000.0, tick_on_monotonic: float = 0.0) -> None:
        self.now = start
        self.tick_on_monotonic = tick_on_monotonic
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        current = self.now
        self.now += self.tick_on_monotonic
        return current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ScenarioDockerPort:
    """Fake DockerPort replaying queued responses for service ls/inspect/ps/logs."""

    def __init__(self, scenario: dict[str, object]) -> None:
        self.scenario = scenario
        self.calls: list[tuple[str, ...]] = []
        self._counters: dict[str, int] = {}

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
        self.calls.append(cmd)
        if len(cmd) >= 2 and cmd[0] == "service":
            sub = cmd[1]
            if sub == "ls":
                if "--format" in cmd:
                    return self._next("ls", self.scenario["ls"])  # type: ignore[arg-type]
                return _ok(str(self.scenario.get("diag_ls", "mystack_app 1/1\n")))
            if sub == "inspect":
                svc = cmd[2]
                inspect_map: dict[str, list[CommandResult]] = self.scenario["inspect"]  # type: ignore[assignment]
                return self._next(f"inspect_{svc}", inspect_map[svc])
            if sub == "ps":
                svc = cmd[2]
                if "--format" in cmd:
                    ps_map: dict[str, list[CommandResult]] = self.scenario["ps"]  # type: ignore[assignment]
                    return self._next(f"ps_{svc}", ps_map[svc])
                diag_ps: dict[str, str] = self.scenario.get("diag_ps", {})  # type: ignore[assignment]
                return _ok(diag_ps.get(svc, f"{svc} ps diag line\n"))
            if sub == "logs":
                svc = cmd[-1]
                diag_logs: dict[str, str] = self.scenario.get("diag_logs", {})  # type: ignore[assignment]
                return _ok(diag_logs.get(svc, f"crash log line for {svc}\n"))
        return _err(f"Unexpected docker args: {cmd}", rc=99)

    def _next(self, key: str, entries: list[CommandResult]) -> CommandResult:
        idx = self._counters.get(key, 0)
        self._counters[key] = idx + 1
        return entries[min(idx, len(entries) - 1)]


class TestVerifyConvergenceBehavior(unittest.TestCase):
    def test_bash_syntax_and_executable_mode(self) -> None:
        mode = stat.S_IMODE(VERIFY_SCRIPT.stat().st_mode)
        self.assertEqual(mode & 0o111, 0o111, f"{VERIFY_SCRIPT} must be executable")
        self.assertEqual(mode & 0o002, 0, f"{VERIFY_SCRIPT} must not be world-writable")
        res = subprocess.run(
            ["bash", "-n", str(VERIFY_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_stable_convergence_single_and_multi_replica_with_transient_poll(self) -> None:
        scenario = {
            "ls": [
                _err("Error response from daemon: transient RPC error"),
                _ok("mystack_api|0/0\nmystack_worker|0/2\n"),
                _ok("mystack_api|1/1\nmystack_worker|1/2\n"),
                _ok("mystack_api|1/1\nmystack_worker|2/2\n"),
                _ok("mystack_api|1/1\nmystack_worker|2/2\n"),
            ],
            "inspect": {
                "mystack_api": [
                    _err("transient inspect error"),
                    _ok("updating\n"),
                    _ok("completed\n"),
                    _ok("\n"),
                ],
                "mystack_worker": [_ok("completed\n")],
            },
            "ps": {
                "mystack_api": [
                    _ok("api_t1|Starting 1 second ago|\n"),
                    _err("transient ps error"),
                    _ok("api_t1|Running 3 seconds ago|\n"),
                ],
                "mystack_worker": [
                    _ok("w_t2|Running 4 seconds ago|\nw_t1|Running 5 seconds ago|\n"),
                    _ok("w_t1|Running 14 seconds ago|\nw_t2|Running 15 seconds ago|\n"),
                    _ok("w_t2|Running 24 seconds ago|\nw_t1|Running 25 seconds ago|\n"),
                ],
            },
        }
        config = parse_convergence_args(
            [
                "--timeout=60",
                "--poll",
                "2",
                "--dwell-checks",
                "2",
                "--dwell-interval=4",
                "--log-tail",
                "30",
                "mystack",
                "mystack_api",
                "worker",
            ],
            env={},
        )
        assert config is not None
        docker = ScenarioDockerPort(scenario)
        clock = FakeClock()
        out, err = io.StringIO(), io.StringIO()

        result = verify_convergence(config, docker, clock, out=out, err=err)

        self.assertEqual(result.stack_name, "mystack")
        self.assertIn("Warning: docker service ls failed", err.getvalue())
        self.assertIn("Warning: docker service inspect failed", err.getvalue())
        self.assertIn("Warning: Service mystack_api update state is 'updating'", err.getvalue())
        self.assertIn("Warning: docker service ps failed for mystack_api", err.getvalue())
        self.assertNotIn("::error::", err.getvalue() + out.getvalue())
        self.assertIn(
            "Post-deploy Swarm convergence and stability verified for stack 'mystack'.",
            out.getvalue(),
        )
        self.assertEqual(clock.sleeps, [2, 2, 2, 2, 2, 4, 4])
        self.assertFalse(any(c[:2] == ("service", "logs") for c in docker.calls))

    def test_single_service_1_of_1_converges_cleanly_with_env_overrides(self) -> None:
        scenario = {
            "ls": [_ok("solo_web|1/1\n")],
            "inspect": {"solo_web": [_ok("\n")]},
            "ps": {"solo_web": [_ok("web_t1|Running 1 second ago|\n")]},
        }
        config = parse_convergence_args(
            ["solo", "solo_web"],
            env={
                "CONVERGENCE_TIMEOUT": "30",
                "POLL_INTERVAL": "1",
                "DWELL_CHECKS": "1",
                "DWELL_INTERVAL": "2",
                "LOG_TAIL": "25",
            },
        )
        assert config is not None
        docker = ScenarioDockerPort(scenario)
        clock = FakeClock()
        out, err = io.StringIO(), io.StringIO()

        verify_convergence(config, docker, clock, out=out, err=err)

        self.assertNotIn("::error::", err.getvalue() + out.getvalue())
        self.assertEqual(clock.sleeps, [2])
        self.assertIn("Dwell check 1/1 passed", out.getvalue())

    def test_task_id_churn_during_dwell_fails_and_dumps_logs(self) -> None:
        scenario = {
            "ls": [_ok("mystack_worker|2/2\n")],
            "inspect": {"mystack_worker": [_ok("completed\n")]},
            "ps": {
                "mystack_worker": [
                    _ok("w_t2|Running 4s|\nw_t1|Running 5s|\n"),
                    _ok("w_t1|Running 14s|\nw_t2|Running 15s|\n"),
                    _ok("w_t1|Running 24s|\nw_t3|Running 1s|\n"),
                ]
            },
            "diag_logs": {"mystack_worker": "worker OOMKilled backtrace\n"},
        }
        config = parse_convergence_args(
            ["--dwell-checks", "2", "--dwell-interval", "3", "--log-tail", "45", "mystack", "worker"],
            env={},
        )
        assert config is not None
        docker = ScenarioDockerPort(scenario)
        clock = FakeClock()
        out, err = io.StringIO(), io.StringIO()

        with self.assertRaises(ConvergenceFailureError):
            verify_convergence(config, docker, clock, out=out, err=err)

        self.assertIn("::error::Task ID churn detected for mystack_worker during dwell check 2/2", err.getvalue())
        self.assertIn("worker OOMKilled backtrace", err.getvalue())
        self.assertIn(("service", "ls", "--filter", "name=mystack_"), docker.calls)
        self.assertIn(("service", "ps", "mystack_worker", "--no-trunc"), docker.calls)
        self.assertIn(("service", "logs", "--tail", "45", "mystack_worker"), docker.calls)

    def test_non_running_transition_and_task_error_during_dwell_fail_with_diagnostics(self) -> None:
        cases = [
            ("non_running_state", _ok("t1|Failed 1 second ago|exit 137\n"), "is not Running"),
            ("non_empty_task_error", _ok("t1|Running 8 seconds ago|healthcheck failed\n"), "reported error: healthcheck failed"),
            ("replica_count_drop", _ok(""), "has 0 running task(s) (expected 1)"),
        ]
        for label, dwell_ps, expected_msg in cases:
            with self.subTest(case=label):
                scenario = {
                    "ls": [_ok("mystack_api|1/1\n")],
                    "inspect": {"mystack_api": [_ok("\n")]},
                    "ps": {"mystack_api": [_ok("t1|Running 2s|\n"), dwell_ps]},
                    "diag_logs": {"mystack_api": f"diag log for {label}\n"},
                }
                config = parse_convergence_args(
                    ["--dwell-checks", "1", "--dwell-interval", "2", "--log-tail", "20", "mystack", "api"],
                    env={},
                )
                assert config is not None
                docker = ScenarioDockerPort(scenario)
                clock = FakeClock()
                out, err = io.StringIO(), io.StringIO()

                with self.assertRaises(ConvergenceFailureError):
                    verify_convergence(config, docker, clock, out=out, err=err)

                self.assertIn("::error::Stability dwell check 1/1 failed for mystack_api", err.getvalue())
                self.assertIn(expected_msg, err.getvalue())
                self.assertIn(f"diag log for {label}", err.getvalue())
                self.assertIn(("service", "logs", "--tail", "20", "mystack_api"), docker.calls)

    def test_abnormal_update_state_fails_immediately_before_timeout(self) -> None:
        for bad_state in ("paused", "rollback_started", "rollback_completed"):
            with self.subTest(state=bad_state):
                scenario = {
                    "ls": [_ok("mystack_api|0/1\n")],
                    "inspect": {"mystack_api": [_ok(f"{bad_state}\n")]},
                    "ps": {"mystack_api": [_ok("")]},
                    "diag_logs": {"mystack_api": f"rollback log {bad_state}\n"},
                }
                config = parse_convergence_args(["--timeout", "120", "--poll", "5", "mystack", "api"], env={})
                assert config is not None
                docker = ScenarioDockerPort(scenario)
                clock = FakeClock()
                err = io.StringIO()

                with self.assertRaises(ConvergenceFailureError):
                    verify_convergence(config, docker, clock, out=io.StringIO(), err=err)

                self.assertIn(
                    f"::error::Service mystack_api entered abnormal Swarm update state '{bad_state}'.",
                    err.getvalue(),
                )
                self.assertIn(f"rollback log {bad_state}", err.getvalue())
                self.assertEqual(clock.sleeps, [])

    def test_convergence_timeout_fails_loudly_with_diagnostics(self) -> None:
        scenario = {
            "ls": [_ok("mystack_api|0/1\n")],
            "inspect": {"mystack_api": [_ok("updating\n")]},
            "ps": {"mystack_api": [_ok("")]},
            "diag_ls": "mystack_api 0/1\n",
            "diag_ps": {"mystack_api": "t_bad Pending No suitable node\n"},
            "diag_logs": {"mystack_api": "startup timeout log line\n"},
        }
        config = parse_convergence_args(
            ["mystack", "api"],
            env={"CONVERGENCE_TIMEOUT": "3", "POLL_INTERVAL": "1", "LOG_TAIL": "15"},
        )
        assert config is not None
        docker = ScenarioDockerPort(scenario)
        clock = FakeClock(start=1000.0, tick_on_monotonic=2.0)
        err = io.StringIO()

        with self.assertRaises(ConvergenceFailureError):
            verify_convergence(config, docker, clock, out=io.StringIO(), err=err)

        self.assertIn(
            "::error::Post-deploy convergence timed out after 3s for stack 'mystack' (mystack_api).",
            err.getvalue(),
        )
        self.assertIn("t_bad Pending No suitable node", err.getvalue())
        self.assertIn("startup timeout log line", err.getvalue())
        self.assertIn(("service", "ls", "--filter", "name=mystack_"), docker.calls)
        self.assertIn(("service", "ps", "mystack_api", "--no-trunc"), docker.calls)
        self.assertIn(("service", "logs", "--tail", "15", "mystack_api"), docker.calls)

    def test_cli_validation_fails_loudly_with_exit_1_on_invalid_flags_values_and_positionals(self) -> None:
        invalid_cases = [
            ("no_args", [], None),
            ("missing_service", ["mystack"], None),
            ("unknown_long_flag", ["--unknown-flag", "mystack", "api"], None),
            ("unknown_short_flag", ["-x", "mystack", "api"], None),
            ("missing_opt_value_at_end", ["mystack", "api", "--timeout"], None),
            ("missing_opt_value_before_flag", ["--timeout", "--poll", "5", "mystack", "api"], None),
            ("empty_equals_opt_value", ["--timeout=", "mystack", "api"], None),
            ("zero_timeout", ["--timeout", "0", "mystack", "api"], None),
            ("negative_poll", ["--poll", "-2", "mystack", "api"], None),
            ("non_numeric_dwell_checks", ["--dwell-checks", "abc", "mystack", "api"], None),
            ("float_dwell_interval", ["--dwell-interval", "1.5", "mystack", "api"], None),
            ("zero_log_tail", ["--log-tail", "0", "mystack", "api"], None),
            ("empty_service_arg", ["mystack", ""], None),
            ("invalid_env_timeout", ["mystack", "api"], {"CONVERGENCE_TIMEOUT": "0"}),
        ]

        for label, cli_args, env_overrides in invalid_cases:
            with self.subTest(case=label):
                with self.assertRaises(ConvergenceConfigError):
                    parse_convergence_args(cli_args, env=env_overrides or {})

                old_env = os.environ.copy()
                try:
                    if env_overrides:
                        os.environ.update(env_overrides)
                    with contextlib.redirect_stderr(io.StringIO()):
                        rc = cli.main(["verify-convergence", *cli_args])
                    self.assertEqual(rc, 1)
                finally:
                    os.environ.clear()
                    os.environ.update(old_env)

    def test_shim_help_exits_zero_via_subprocess(self) -> None:
        res = subprocess.run(
            ["bash", str(VERIFY_SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Usage: verify_convergence.sh", res.stdout)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Unit tests for deploy/verify_convergence.sh."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = REPO_ROOT / "deploy" / "verify_convergence.sh"

MOCK_DOCKER_PY = f"""#!{sys.executable}
import json
import os
import sys
from pathlib import Path

state_dir = Path(os.environ["MOCK_STATE_DIR"])
scenario = json.loads(Path(os.environ["MOCK_SCENARIO_FILE"]).read_text(encoding="utf-8"))
calls_file = state_dir / "docker_calls.jsonl"
args = sys.argv[1:]

with calls_file.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")

def next_entry(key: str, entries: list[dict]) -> dict:
    counter_file = state_dir / ("count_" + key.replace("/", "_") + ".txt")
    idx = int(counter_file.read_text(encoding="utf-8")) if counter_file.exists() else 0
    counter_file.write_text(str(idx + 1), encoding="utf-8")
    return entries[min(idx, len(entries) - 1)]

if len(args) >= 2 and args[0] == "service":
    sub = args[1]
    if sub == "ls":
        if "--format" in args:
            resp = next_entry("ls", scenario["ls"])
            if resp.get("stdout"):
                sys.stdout.write(resp["stdout"])
            if resp.get("stderr"):
                sys.stderr.write(resp["stderr"])
            sys.exit(resp.get("rc", 0))
        diag = scenario.get("diag_ls", "mystack_app 1/1\\n")
        sys.stdout.write(diag)
        sys.exit(0)

    if sub == "inspect":
        svc = args[2]
        entries = scenario["inspect"][svc]
        resp = next_entry(f"inspect_{{svc}}", entries)
        if resp.get("stdout"):
            sys.stdout.write(resp["stdout"])
        if resp.get("stderr"):
            sys.stderr.write(resp["stderr"])
        sys.exit(resp.get("rc", 0))

    if sub == "ps":
        svc = args[2]
        if "--format" in args:
            entries = scenario["ps"][svc]
            resp = next_entry(f"ps_{{svc}}", entries)
            if resp.get("stdout"):
                sys.stdout.write(resp["stdout"])
            if resp.get("stderr"):
                sys.stderr.write(resp["stderr"])
            sys.exit(resp.get("rc", 0))
        diag_ps = scenario.get("diag_ps", {{}}).get(svc, f"{{svc}} ps diag line\\n")
        sys.stdout.write(diag_ps)
        sys.exit(0)

    if sub == "logs":
        svc = args[-1]
        diag_logs = scenario.get("diag_logs", {{}}).get(svc, f"crash log line for {{svc}}\\n")
        sys.stdout.write(diag_logs)
        sys.exit(0)

sys.stderr.write(f"Unexpected docker args: {{args}}\\n")
sys.exit(99)
"""

MOCK_DATE_PY = f"""#!{sys.executable}
import os
import sys
from pathlib import Path

state_dir = Path(os.environ["MOCK_STATE_DIR"])
step = int(os.environ.get("MOCK_DATE_STEP", "0"))
epoch_file = state_dir / "date_epoch.txt"
if epoch_file.exists():
    val = int(epoch_file.read_text(encoding="utf-8")) + step
else:
    val = int(os.environ.get("MOCK_DATE_START", "1000"))
epoch_file.write_text(str(val), encoding="utf-8")
print(val)
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _ok(stdout: str = "") -> dict:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


def _err(stderr: str, rc: int = 1) -> dict:
    return {"rc": rc, "stdout": "", "stderr": stderr}


class _VerifyConvergenceBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-verify-convergence-"))
        self.mock_bin = self.tmpdir / "bin"
        self.state_dir = self.tmpdir / "state"
        self.mock_bin.mkdir()
        self.state_dir.mkdir()
        self.scenario_file = self.tmpdir / "scenario.json"
        self.sleep_log = self.state_dir / "sleep.log"
        self.calls_file = self.state_dir / "docker_calls.jsonl"

        _write_executable(self.mock_bin / "docker", MOCK_DOCKER_PY)
        _write_executable(
            self.mock_bin / "sleep",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.sleep_log}"\n',
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install_mock_date(self, step: int = 1, start: int = 1000) -> dict[str, str]:
        _write_executable(self.mock_bin / "date", MOCK_DATE_PY)
        return {"MOCK_DATE_STEP": str(step), "MOCK_DATE_START": str(start)}

    def _run_verify(
        self,
        args: list[str],
        scenario: dict | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if scenario is not None:
            self.scenario_file.write_text(json.dumps(scenario), encoding="utf-8")
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "MOCK_STATE_DIR": str(self.state_dir),
            "MOCK_SCENARIO_FILE": str(self.scenario_file),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(VERIFY_SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _docker_calls(self) -> list[list[str]]:
        if not self.calls_file.exists():
            return []
        lines = self.calls_file.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def _sleep_calls(self) -> list[str]:
        if not self.sleep_log.exists():
            return []
        return self.sleep_log.read_text(encoding="utf-8").splitlines()


class TestVerifyConvergenceBehavior(_VerifyConvergenceBase):
    def test_bash_syntax_and_executable_mode(self) -> None:
        mode = stat.S_IMODE(VERIFY_SCRIPT.stat().st_mode)
        self.assertEqual(mode & 0o111, 0o111, f"{VERIFY_SCRIPT} must be executable")
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
                "mystack_worker": [
                    _ok("completed\n"),
                ],
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

        res = self._run_verify(
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
            scenario=scenario,
        )

        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("Warning: docker service ls failed", res.stderr)
        self.assertIn("Warning: docker service inspect failed", res.stderr)
        self.assertIn("Warning: Service mystack_api update state is 'updating'", res.stderr)
        self.assertIn("Warning: docker service ps failed for mystack_api", res.stderr)
        self.assertNotIn("::error::", res.stderr + res.stdout)
        self.assertIn(
            "Post-deploy Swarm convergence and stability verified for stack 'mystack'.",
            res.stdout,
        )
        self.assertEqual(self._sleep_calls(), ["2", "2", "2", "2", "2", "4", "4"])
        calls = self._docker_calls()
        self.assertFalse(any(c[:2] == ["service", "logs"] for c in calls))

    def test_single_service_1_of_1_converges_cleanly_with_env_overrides(self) -> None:
        scenario = {
            "ls": [_ok("solo_web|1/1\n")],
            "inspect": {"solo_web": [_ok("\n")]},
            "ps": {"solo_web": [_ok("web_t1|Running 1 second ago|\n")]},
        }

        res = self._run_verify(
            ["solo", "solo_web"],
            scenario=scenario,
            extra_env={
                "CONVERGENCE_TIMEOUT": "30",
                "POLL_INTERVAL": "1",
                "DWELL_CHECKS": "1",
                "DWELL_INTERVAL": "2",
                "LOG_TAIL": "25",
            },
        )

        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertNotIn("::error::", res.stderr + res.stdout)
        self.assertEqual(self._sleep_calls(), ["2"])
        self.assertIn("Dwell check 1/1 passed", res.stdout)

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

        res = self._run_verify(
            ["--dwell-checks", "2", "--dwell-interval", "3", "--log-tail", "45", "mystack", "worker"],
            scenario=scenario,
        )

        self.assertEqual(res.returncode, 1)
        self.assertIn("::error::Task ID churn detected for mystack_worker during dwell check 2/2", res.stderr)
        self.assertIn("worker OOMKilled backtrace", res.stderr)
        calls = self._docker_calls()
        self.assertIn(["service", "ls", "--filter", "name=mystack_"], calls)
        self.assertIn(["service", "ps", "mystack_worker", "--no-trunc"], calls)
        self.assertIn(["service", "logs", "--tail", "45", "mystack_worker"], calls)

    def test_non_running_transition_and_task_error_during_dwell_fail_with_diagnostics(self) -> None:
        cases = [
            (
                "non_running_state",
                _ok("t1|Failed 1 second ago|exit 137\n"),
                "is not Running",
            ),
            (
                "non_empty_task_error",
                _ok("t1|Running 8 seconds ago|healthcheck failed\n"),
                "reported error: healthcheck failed",
            ),
            (
                "replica_count_drop",
                _ok(""),
                "has 0 running task(s) (expected 1)",
            ),
        ]
        for label, dwell_ps, expected_msg in cases:
            with self.subTest(case=label):
                for p in self.state_dir.glob("*"):
                    p.unlink()
                scenario = {
                    "ls": [_ok("mystack_api|1/1\n")],
                    "inspect": {"mystack_api": [_ok("\n")]},
                    "ps": {"mystack_api": [_ok("t1|Running 2s|\n"), dwell_ps]},
                    "diag_logs": {"mystack_api": f"diag log for {label}\n"},
                }

                res = self._run_verify(
                    ["--dwell-checks", "1", "--dwell-interval", "2", "--log-tail", "20", "mystack", "api"],
                    scenario=scenario,
                )

                self.assertEqual(res.returncode, 1)
                self.assertIn("::error::Stability dwell check 1/1 failed for mystack_api", res.stderr)
                self.assertIn(expected_msg, res.stderr)
                self.assertIn(f"diag log for {label}", res.stderr)
                self.assertIn(["service", "logs", "--tail", "20", "mystack_api"], self._docker_calls())

    def test_abnormal_update_state_fails_immediately_before_timeout(self) -> None:
        for bad_state in ("paused", "rollback_started", "rollback_completed"):
            with self.subTest(state=bad_state):
                for p in self.state_dir.glob("*"):
                    p.unlink()
                scenario = {
                    "ls": [_ok("mystack_api|0/1\n")],
                    "inspect": {"mystack_api": [_ok(f"{bad_state}\n")]},
                    "ps": {"mystack_api": [_ok("")]},
                    "diag_logs": {"mystack_api": f"rollback log {bad_state}\n"},
                }

                res = self._run_verify(
                    ["--timeout", "120", "--poll", "5", "mystack", "api"],
                    scenario=scenario,
                )

                self.assertEqual(res.returncode, 1)
                self.assertIn(
                    f"::error::Service mystack_api entered abnormal Swarm update state '{bad_state}'.",
                    res.stderr,
                )
                self.assertIn(f"rollback log {bad_state}", res.stderr)
                self.assertEqual(self._sleep_calls(), [])

    def test_convergence_timeout_fails_loudly_with_diagnostics(self) -> None:
        date_env = self._install_mock_date(step=2, start=1000)
        scenario = {
            "ls": [_ok("mystack_api|0/1\n")],
            "inspect": {"mystack_api": [_ok("updating\n")]},
            "ps": {"mystack_api": [_ok("")]},
            "diag_ls": "mystack_api 0/1\n",
            "diag_ps": {"mystack_api": "t_bad Pending No suitable node\n"},
            "diag_logs": {"mystack_api": "startup timeout log line\n"},
        }

        res = self._run_verify(
            ["mystack", "api"],
            scenario=scenario,
            extra_env={
                **date_env,
                "CONVERGENCE_TIMEOUT": "3",
                "POLL_INTERVAL": "1",
                "LOG_TAIL": "15",
            },
        )

        self.assertEqual(res.returncode, 1)
        self.assertIn(
            "::error::Post-deploy convergence timed out after 3s for stack 'mystack' (mystack_api).",
            res.stderr,
        )
        self.assertIn("t_bad Pending No suitable node", res.stderr)
        self.assertIn("startup timeout log line", res.stderr)
        calls = self._docker_calls()
        self.assertIn(["service", "ls", "--filter", "name=mystack_"], calls)
        self.assertIn(["service", "ps", "mystack_api", "--no-trunc"], calls)
        self.assertIn(["service", "logs", "--tail", "15", "mystack_api"], calls)

    def test_cli_validation_fails_loudly_on_invalid_flags_values_and_positionals(self) -> None:
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
                self.calls_file.unlink(missing_ok=True)
                res = self._run_verify(cli_args, extra_env=env_overrides)
                self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
                self.assertEqual(self._docker_calls(), [])


if __name__ == "__main__":
    unittest.main()

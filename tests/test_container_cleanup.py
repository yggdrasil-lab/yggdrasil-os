#!/usr/bin/env python3
"""Unit tests for orphaned container and anonymous volume cleanup scripts."""

import configparser
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SCRIPT = REPO_ROOT / "maintenance" / "cleanup_storage.sh"
INSTALL_TIMER_SCRIPT = REPO_ROOT / "maintenance" / "install_cleanup_timer.sh"
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"
SYSTEMD_DIR = REPO_ROOT / "maintenance" / "systemd"
MONTHLY_CLEANUP_SERVICE = SYSTEMD_DIR / "gaia-cleanup.service"
MONTHLY_CLEANUP_TIMER = SYSTEMD_DIR / "gaia-cleanup.timer"
DAILY_PRUNE_SERVICE = SYSTEMD_DIR / "gaia-container-prune.service"
DAILY_PRUNE_TIMER = SYSTEMD_DIR / "gaia-container-prune.timer"
STARTUP_SCRIPT = REPO_ROOT / "swarm" / "startup_orchestrator.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _parse_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    return parser


def _extract_setup_gaia_cleanup_step() -> str:
    text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'(echo "=== \[15/15\] Setting up Gaia Storage Cleanup & Daily Container Prune Timers ==="\n'
        r'if \[ -n "\$\{SCRIPT_DIR\}" \].*?\nfi)',
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("Could not locate [15/15] cleanup timer block in hosts/setup_gaia.sh")
    return match.group(1)


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
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.docker_log = self.tmpdir / "docker.log"
        self.df_log = self.tmpdir / "df.log"
        self.journal_log = self.tmpdir / "journal.log"
        self.net_prune_log = self.tmpdir / "net_prune.log"

        _write_executable(
            self.mock_bin / "docker",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.docker_log}\n"
            "if [ \"${1:-}\" = \"ps\" ] && [ -n \"${MOCK_REGISTRY_CID:-}\" ]; then\n"
            "  printf '%s\\n' \"$MOCK_REGISTRY_CID\"\n"
            "fi\n"
            "exit 0\n",
        )
        _write_executable(
            self.mock_bin / "df",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.df_log}\n"
            "echo 'Filesystem      Size  Used Avail Use% Mounted on'\n"
            "echo '/dev/nvme0n1p2  900G  200G  700G  23% /'\n",
        )
        _write_executable(
            self.mock_bin / "journalctl",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.journal_log}\n",
        )
        _write_executable(
            self.mock_bin / "prune-network-endpoints",
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {self.net_prune_log}\n",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_prunes_containers_and_anonymous_volumes_before_builder_and_image_prune(
        self,
    ) -> None:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
        }
        res = subprocess.run(
            ["bash", str(CLEANUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        container_prune_cmd = "container prune -f --filter until=24h"
        volume_prune_cmd = "volume prune -f"
        builder_prune_cmd = "builder prune -a -f --filter until=168h"
        image_prune_cmd = "image prune -a -f"

        self.assertIn(container_prune_cmd, docker_calls)
        self.assertIn(volume_prune_cmd, docker_calls)
        self.assertIn(builder_prune_cmd, docker_calls)
        self.assertIn(image_prune_cmd, docker_calls)

        volume_calls = [c for c in docker_calls if c.startswith("volume prune")]
        for call in volume_calls:
            tokens = call.split()
            self.assertNotIn("-a", tokens)
            self.assertNotIn("--all", tokens)

        c_idx = docker_calls.index(container_prune_cmd)
        v_idx = docker_calls.index(volume_prune_cmd)
        b_idx = docker_calls.index(builder_prune_cmd)
        i_idx = docker_calls.index(image_prune_cmd)

        self.assertLess(c_idx, v_idx)
        self.assertLess(c_idx, b_idx)
        self.assertLess(c_idx, i_idx)
        self.assertLess(v_idx, b_idx)
        self.assertLess(v_idx, i_idx)

        self.assertEqual(
            self.journal_log.read_text(encoding="utf-8").splitlines(),
            ["--vacuum-size=500M"],
        )
        self.assertEqual(
            self.df_log.read_text(encoding="utf-8").splitlines(),
            ["-h /", "-h /"],
        )
        self.assertEqual(
            self.net_prune_log.read_text(encoding="utf-8").splitlines(),
            ["--all --force"],
        )

    def _run_cleanup(
        self,
        args: list[str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
        }
        if extra_env:
            env.update(extra_env)
        cmd = ["bash", str(CLEANUP_SCRIPT)]
        if args:
            cmd.extend(args)
        return subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_containers_only_prunes_containers_and_anonymous_volumes_only(self) -> None:
        res = self._run_cleanup(
            args=["--containers-only"],
            extra_env={"MOCK_REGISTRY_CID": "reg-123"},
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            docker_calls,
            [
                "container prune -f --filter until=24h",
                "volume prune -f",
            ],
        )

        volume_calls = [c for c in docker_calls if c.startswith("volume prune")]
        for call in volume_calls:
            tokens = call.split()
            self.assertNotIn("-a", tokens)
            self.assertNotIn("--all", tokens)

        self.assertFalse(self.journal_log.exists())
        self.assertFalse(self.net_prune_log.exists())
        self.assertEqual(
            self.df_log.read_text(encoding="utf-8").splitlines(),
            ["-h /", "-h /"],
        )
        self.assertIn("Gaia Containers-Only Cleanup Finished", res.stdout)

    def test_unknown_flag_fails_loudly_without_running_cleanup(self) -> None:
        for bad_args in (["--bogus"], ["--containers-only", "--bogus"], ["unexpected"]):
            with self.subTest(args=bad_args):
                for log_file in (
                    self.docker_log,
                    self.df_log,
                    self.journal_log,
                    self.net_prune_log,
                ):
                    log_file.unlink(missing_ok=True)

                res = self._run_cleanup(args=bad_args)
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("Unknown argument", res.stderr)
                self.assertIn(bad_args[-1], res.stderr)
                self.assertFalse(self.docker_log.exists())
                self.assertFalse(self.df_log.exists())
                self.assertFalse(self.journal_log.exists())
                self.assertFalse(self.net_prune_log.exists())


class TestContainerPruneUnitsAndInstaller(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-install-cleanup-timer-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.calls_log = self.tmpdir / "calls.log"
        self.curl_log = self.tmpdir / "curl.log"
        self.dest_bin = self.tmpdir / "usr_local_bin" / "gaia-cleanup.sh"
        self.dest_systemd = self.tmpdir / "etc_systemd_system"
        self._install_mock_tools()

    def _install_mock_tools(self) -> None:
        _write_executable(
            self.mock_bin / "id",
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = \"-u\" ]; then\n"
            "  printf '%s\\n' \"${MOCK_UID:-0}\"\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/id \"$@\"\n",
        )
        _write_executable(
            self.mock_bin / "systemctl",
            "#!/usr/bin/env bash\n"
            f"printf 'systemctl %s\\n' \"$*\" >> {self.calls_log}\n"
            "if [ \"${1:-}\" = \"list-timers\" ]; then\n"
            "  if [ \"${MOCK_EMPTY_TIMERS:-0}\" != \"1\" ]; then\n"
            "    echo 'NEXT LEFT LAST PASSED UNIT ACTIVATES'\n"
            "    echo 'Mon 03:00 - - - gaia-cleanup.timer gaia-cleanup.service'\n"
            "    echo 'Tue 04:30 - - - gaia-container-prune.timer gaia-container-prune.service'\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n",
        )
        self._install_mock_curl()

    def _install_mock_curl(self) -> None:
        _write_executable(
            self.mock_bin / "curl",
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "url=\"\"\n"
            "out=\"\"\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o) out=\"$2\"; shift 2 ;;\n"
            "    -*) shift ;;\n"
            "    *) url=\"$1\"; shift ;;\n"
            "  esac\n"
            "done\n"
            f"printf '%s\\n' \"$url\" >> {self.curl_log}\n"
            "if [ -n \"$out\" ]; then\n"
            "  printf '# downloaded from %s\\n' \"$url\" > \"$out\"\n"
            "elif [ -n \"${MOCK_INSTALL_SCRIPT_PATH:-}\" ] && [[ \"$url\" == */maintenance/install_cleanup_timer.sh ]]; then\n"
            "  cat \"$MOCK_INSTALL_SCRIPT_PATH\"\n"
            "fi\n",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_installer(
        self,
        *,
        stdin_mode: bool = False,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "GAIA_CLEANUP_BIN_PATH": str(self.dest_bin),
            "GAIA_SYSTEMD_DIR": str(self.dest_systemd),
        }
        if extra_env:
            env.update(extra_env)
        if stdin_mode:
            return subprocess.run(
                ["bash"],
                input=INSTALL_TIMER_SCRIPT.read_text(encoding="utf-8"),
                cwd=str(self.tmpdir),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        return subprocess.run(
            ["bash", str(INSTALL_TIMER_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

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

    def test_install_cleanup_timer_non_root_fails_loudly(self) -> None:
        res = self._run_installer(extra_env={"MOCK_UID": "1000"})
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("must be run as root", res.stderr)
        self.assertFalse(self.calls_log.exists())
        self.assertFalse(self.dest_bin.exists())

    def test_install_cleanup_timer_local_idempotent_installs_both_timers(self) -> None:
        expected_calls = [
            "systemctl daemon-reload",
            "systemctl enable --now gaia-cleanup.timer",
            "systemctl enable --now gaia-container-prune.timer",
            "systemctl list-timers --all",
        ]
        for run_idx in range(2):
            with self.subTest(run=run_idx):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_installer()
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                self.assertEqual(
                    self.calls_log.read_text(encoding="utf-8").splitlines(),
                    expected_calls,
                )

        self.assertEqual(stat.S_IMODE(self.dest_bin.stat().st_mode), 0o755)
        self.assertEqual(
            self.dest_bin.read_text(encoding="utf-8"),
            CLEANUP_SCRIPT.read_text(encoding="utf-8"),
        )
        for unit in (
            "gaia-cleanup.service",
            "gaia-cleanup.timer",
            "gaia-container-prune.service",
            "gaia-container-prune.timer",
        ):
            installed = self.dest_systemd / unit
            self.assertTrue(installed.is_file(), f"Missing installed {unit}")
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o644)
            self.assertEqual(
                installed.read_text(encoding="utf-8"),
                (SYSTEMD_DIR / unit).read_text(encoding="utf-8"),
            )
        self.assertFalse(self.curl_log.exists())

    def test_install_cleanup_timer_zero_clone_stdin_curl_fallback(self) -> None:
        res = self._run_installer(
            stdin_mode=True,
            extra_env={"MOCK_EMPTY_TIMERS": "1"},
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        raw_base = "https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/maintenance"
        self.assertEqual(
            self.curl_log.read_text(encoding="utf-8").splitlines(),
            [
                f"{raw_base}/cleanup_storage.sh",
                f"{raw_base}/systemd/gaia-cleanup.service",
                f"{raw_base}/systemd/gaia-cleanup.timer",
                f"{raw_base}/systemd/gaia-container-prune.service",
                f"{raw_base}/systemd/gaia-container-prune.timer",
            ],
        )
        self.assertEqual(stat.S_IMODE(self.dest_bin.stat().st_mode), 0o755)
        for unit in (
            "gaia-cleanup.service",
            "gaia-cleanup.timer",
            "gaia-container-prune.service",
            "gaia-container-prune.timer",
        ):
            self.assertEqual(stat.S_IMODE((self.dest_systemd / unit).stat().st_mode), 0o644)
        self.assertEqual(
            self.calls_log.read_text(encoding="utf-8").splitlines(),
            [
                "systemctl daemon-reload",
                "systemctl enable --now gaia-cleanup.timer",
                "systemctl enable --now gaia-container-prune.timer",
                "systemctl list-timers --all",
            ],
        )

    def test_install_cleanup_timer_honors_raw_base_override(self) -> None:
        override = "https://mirror.example.test/yggdrasil-os/feat"
        res = self._run_installer(
            stdin_mode=True,
            extra_env={"MOCK_EMPTY_TIMERS": "1", "RAW_BASE": override},
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(
            self.curl_log.read_text(encoding="utf-8").splitlines(),
            [
                f"{override}/maintenance/cleanup_storage.sh",
                f"{override}/maintenance/systemd/gaia-cleanup.service",
                f"{override}/maintenance/systemd/gaia-cleanup.timer",
                f"{override}/maintenance/systemd/gaia-container-prune.service",
                f"{override}/maintenance/systemd/gaia-container-prune.timer",
            ],
        )

    def test_setup_gaia_step_numbering_is_consistent(self) -> None:
        setup_text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
        headers = re.findall(r"=== \[(\d+)/(\d+)\]", setup_text)
        self.assertEqual(
            headers,
            [(str(i), "15") for i in range(1, 16)],
            f"Unexpected step numbering in setup_gaia.sh: {headers}",
        )

    def test_setup_gaia_cleanup_step_runs_local_mode(self) -> None:
        step_block = _extract_setup_gaia_cleanup_step()
        override = "https://mirror.example.test/yggdrasil-os/feat"
        local_runner = (
            "set -euo pipefail\n"
            f'SCRIPT_DIR="{REPO_ROOT / "hosts"}"\n'
            f'RAW_BASE="{override}"\n'
            f"{step_block}\n"
        )
        res = self._run_bash_snippet(local_runner)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertFalse(self.curl_log.exists())
        self.assertEqual(
            self.calls_log.read_text(encoding="utf-8").splitlines(),
            [
                "systemctl daemon-reload",
                "systemctl enable --now gaia-cleanup.timer",
                "systemctl enable --now gaia-container-prune.timer",
                "systemctl list-timers --all",
            ],
        )

    def test_setup_gaia_cleanup_step_runs_curl_mode(self) -> None:
        step_block = _extract_setup_gaia_cleanup_step()
        override = "https://mirror.example.test/yggdrasil-os/feat"
        curl_runner = (
            "set -euo pipefail\n"
            'SCRIPT_DIR=""\n'
            f'RAW_BASE="{override}"\n'
            f"{step_block}\n"
        )
        res = self._run_bash_snippet(
            curl_runner,
            extra_env={"MOCK_INSTALL_SCRIPT_PATH": str(INSTALL_TIMER_SCRIPT)},
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(
            self.curl_log.read_text(encoding="utf-8").splitlines(),
            [
                f"{override}/maintenance/install_cleanup_timer.sh",
                f"{override}/maintenance/cleanup_storage.sh",
                f"{override}/maintenance/systemd/gaia-cleanup.service",
                f"{override}/maintenance/systemd/gaia-cleanup.timer",
                f"{override}/maintenance/systemd/gaia-container-prune.service",
                f"{override}/maintenance/systemd/gaia-container-prune.timer",
            ],
        )
        self.assertEqual(
            self.calls_log.read_text(encoding="utf-8").splitlines(),
            [
                "systemctl daemon-reload",
                "systemctl enable --now gaia-cleanup.timer",
                "systemctl enable --now gaia-container-prune.timer",
                "systemctl list-timers --all",
            ],
        )

    def _run_bash_snippet(
        self,
        snippet: str,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "GAIA_CLEANUP_BIN_PATH": str(self.dest_bin),
            "GAIA_SYSTEMD_DIR": str(self.dest_systemd),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash"],
            input=snippet,
            cwd=str(self.tmpdir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


class TestStartupOrchestrator(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-startup-orchestrator-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.state_dir = self.tmpdir / "state"
        self.state_dir.mkdir()
        self.docker_log = self.tmpdir / "docker.log"

        for name in ("created", "exited", "dead"):
            (self.state_dir / name).write_text("", encoding="utf-8")
        (self.state_dir / "has_runner").write_text("0", encoding="utf-8")
        (self.state_dir / "runner_running").write_text("false", encoding="utf-8")

        mock_docker = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"STATE_DIR={self.state_dir}\n"
            f"printf '%s\\n' \"$*\" >> {self.docker_log}\n"
            "cmd=\"${1:-}\"\n"
            "case \"$cmd\" in\n"
            "  info)\n"
            "    exit 0\n"
            "    ;;\n"
            "  swarm)\n"
            "    exit 0\n"
            "    ;;\n"
            "  service)\n"
            "    if [ \"${2:-}\" = \"inspect\" ]; then\n"
            "      exit 1\n"
            "    fi\n"
            "    exit 0\n"
            "    ;;\n"
            "  inspect)\n"
            "    cat \"$STATE_DIR/runner_running\"\n"
            "    exit 0\n"
            "    ;;\n"
            "  start)\n"
            "    if [ \"${2:-}\" = \"gaia-runner\" ]; then\n"
            "      echo 'true' > \"$STATE_DIR/runner_running\"\n"
            "    fi\n"
            "    exit 0\n"
            "    ;;\n"
            "  ps)\n"
            "    case \"$*\" in\n"
            "      *\"--format {{.Names}}\"*)\n"
            "        if [ \"$(cat \"$STATE_DIR/has_runner\")\" = \"1\" ]; then\n"
            "          echo 'gaia-runner'\n"
            "        fi\n"
            "        ;;\n"
            "      *\"--filter status=running\"*)\n"
            "        if [ \"$(cat \"$STATE_DIR/has_runner\")\" = \"1\" ] && [ \"$(cat \"$STATE_DIR/runner_running\")\" = \"true\" ]; then\n"
            "          echo 'cid-gaia-runner'\n"
            "        fi\n"
            "        ;;\n"
            "      *\"--filter status=created --filter status=exited --filter status=dead\"*)\n"
            "        cat \"$STATE_DIR/created\" \"$STATE_DIR/exited\" \"$STATE_DIR/dead\"\n"
            "        ;;\n"
            "      *\"--filter status=dead\"*)\n"
            "        cat \"$STATE_DIR/dead\"\n"
            "        ;;\n"
            "    esac\n"
            "    exit 0\n"
            "    ;;\n"
            "  container)\n"
            "    if [ \"${2:-}\" = \"prune\" ]; then\n"
            "      : > \"$STATE_DIR/created\"\n"
            "      : > \"$STATE_DIR/exited\"\n"
            "    fi\n"
            "    exit 0\n"
            "    ;;\n"
            "  rm)\n"
            "    shift\n"
            "    for arg in \"$@\"; do\n"
            "      case \"$arg\" in -*) continue ;; esac\n"
            "      grep -vxF -- \"$arg\" \"$STATE_DIR/dead\" > \"$STATE_DIR/dead.tmp\" || true\n"
            "      mv \"$STATE_DIR/dead.tmp\" \"$STATE_DIR/dead\"\n"
            "    done\n"
            "    exit 0\n"
            "    ;;\n"
            "  volume)\n"
            "    exit 0\n"
            "    ;;\n"
            "  node)\n"
            "    echo 'ID HOSTNAME STATUS AVAILABILITY MANAGER STATUS'\n"
            "    echo 'node1 * gaia Ready Active Leader'\n"
            "    exit 0\n"
            "    ;;\n"
            "esac\n"
            "exit 0\n"
        )
        _write_executable(self.mock_bin / "docker", mock_docker)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_startup(self) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
        }
        return subprocess.run(
            ["bash", str(STARTUP_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_created_only_containers_trigger_prune_after_gaia_runner_guard(self) -> None:
        (self.state_dir / "has_runner").write_text("1", encoding="utf-8")
        (self.state_dir / "runner_running").write_text("false", encoding="utf-8")
        (self.state_dir / "created").write_text("cid-created-1\n", encoding="utf-8")

        res = self._run_startup()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertIn("start gaia-runner", docker_calls)
        self.assertIn("container prune -f", docker_calls)
        self.assertIn("volume prune -f", docker_calls)

        volume_calls = [c for c in docker_calls if c.startswith("volume prune")]
        for call in volume_calls:
            tokens = call.split()
            self.assertNotIn("-a", tokens)
            self.assertNotIn("--all", tokens)

        runner_idx = docker_calls.index("start gaia-runner")
        c_prune_idx = docker_calls.index("container prune -f")
        v_prune_idx = docker_calls.index("volume prune -f")
        self.assertLess(runner_idx, c_prune_idx)
        self.assertLess(c_prune_idx, v_prune_idx)

        ps_created_calls = [
            c
            for c in docker_calls
            if c == "ps -a --filter status=created --filter status=exited --filter status=dead -q"
        ]
        self.assertEqual(len(ps_created_calls), 2)
        self.assertIn("Stopped/Dead Containers:   0", res.stdout)

    def test_dead_containers_invoke_rm_f_v_and_volume_prune(self) -> None:
        (self.state_dir / "has_runner").write_text("1", encoding="utf-8")
        (self.state_dir / "runner_running").write_text("true", encoding="utf-8")
        (self.state_dir / "dead").write_text("cid-dead-1\ncid-dead-2\n", encoding="utf-8")

        res = self._run_startup()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("start gaia-runner", docker_calls)
        self.assertIn("container prune -f", docker_calls)
        self.assertIn("rm -f -v cid-dead-1 cid-dead-2", docker_calls)
        self.assertIn("volume prune -f", docker_calls)

        c_prune_idx = docker_calls.index("container prune -f")
        rm_idx = docker_calls.index("rm -f -v cid-dead-1 cid-dead-2")
        v_prune_idx = docker_calls.index("volume prune -f")
        self.assertLess(c_prune_idx, rm_idx)
        self.assertLess(rm_idx, v_prune_idx)
        self.assertIn("Stopped/Dead Containers:   0", res.stdout)

    def test_no_stopped_containers_skips_pruning(self) -> None:
        res = self._run_startup()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        docker_calls = self.docker_log.read_text(encoding="utf-8").splitlines()
        self.assertNotIn("container prune -f", docker_calls)
        self.assertNotIn("volume prune -f", docker_calls)
        self.assertFalse(any(c.startswith("rm ") for c in docker_calls))
        self.assertIn("No dead/stopped containers found.", res.stdout)


if __name__ == "__main__":
    unittest.main()

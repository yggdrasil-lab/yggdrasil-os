#!/usr/bin/env python3
"""Unit tests for orphaned container and anonymous volume cleanup scripts."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SCRIPT = REPO_ROOT / "maintenance" / "cleanup_storage.sh"
STARTUP_SCRIPT = REPO_ROOT / "swarm" / "startup_orchestrator.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class TestBashSyntax(unittest.TestCase):
    def test_bash_syntax_valid(self) -> None:
        for script in (CLEANUP_SCRIPT, STARTUP_SCRIPT):
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

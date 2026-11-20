#!/usr/bin/env python3
"""Unit tests for stale down vx-* VXLAN cleanup in Docker restart scripts."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GAIA_RESTART_SCRIPT = REPO_ROOT / "docker" / "restart_docker.sh"
MUSPELHEIM_RESTART_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "muspelheim-restart-docker.sh"

SAMPLE_VXLAN_LINKS = "\n".join(
    [
        "19428: vx-001036-y2zly: <BROADCAST,MULTICAST> mtu 1450 qdisc noop state DOWN mode DEFAULT group default \\    link/ether d6:75:8e:f2:5e:1e brd ff:ff:ff:ff:ff:ff",
        "19429: vx-001037-abcde@NONE: <BROADCAST,MULTICAST> mtu 1450 qdisc noop state DOWN mode DEFAULT group default \\    link/ether d6:75:8e:f2:5e:1f brd ff:ff:ff:ff:ff:ff",
        "12: vx-001001-live1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UP mode DEFAULT group default \\    link/ether 02:42:0a:00:01:02 brd ff:ff:ff:ff:ff:ff",
        "13: vx-001002-live2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1450 qdisc noqueue state UNKNOWN mode DEFAULT group default \\    link/ether 02:42:0a:00:02:02 brd ff:ff:ff:ff:ff:ff",
        "20: vxlan0: <BROADCAST,MULTICAST> mtu 1450 qdisc noop state DOWN mode DEFAULT group default \\    link/ether 02:42:00:00:00:01 brd ff:ff:ff:ff:ff:ff",
        "21: flannel.1: <BROADCAST,MULTICAST> mtu 1450 qdisc noop state DOWN mode DEFAULT group default \\    link/ether 02:42:00:00:00:02 brd ff:ff:ff:ff:ff:ff",
    ]
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class TestRestartDockerVxlanCleanup(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-restart-vxlan-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.calls_log = self.tmpdir / "calls.log"
        self.ip_links_file = self.tmpdir / "ip_links.txt"
        self.ip_links_file.write_text(SAMPLE_VXLAN_LINKS + "\n", encoding="utf-8")
        self.startup_hook = self.tmpdir / "mock_startup.sh"
        self.restore_hook = self.tmpdir / "mock_restore.sh"

        self._install_mocks()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install_mocks(self) -> None:
        _write_executable(
            self.mock_bin / "id",
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = \"-u\" ]; then\n"
            "  echo \"${MOCK_UID:-0}\"\n"
            "  exit 0\n"
            "fi\n"
            "exec /usr/bin/id \"$@\"\n",
        )
        _write_executable(
            self.mock_bin / "sync",
            "#!/usr/bin/env bash\n"
            f"echo \"sync\" >> {self.calls_log}\n",
        )
        _write_executable(
            self.mock_bin / "sleep",
            "#!/usr/bin/env bash\n"
            f"echo \"sleep $*\" >> {self.calls_log}\n",
        )
        _write_executable(
            self.mock_bin / "systemctl",
            "#!/usr/bin/env bash\n"
            f"echo \"systemctl $*\" >> {self.calls_log}\n"
            "if [ \"${MOCK_SYSTEMCTL_FAIL_SOCKET:-0}\" = \"1\" ] && [ \"$*\" = \"stop docker.socket docker.service\" ]; then\n"
            "  exit 1\n"
            "fi\n"
            "exit 0\n",
        )
        _write_executable(
            self.mock_bin / "ip",
            "#!/usr/bin/env bash\n"
            f"echo \"ip $*\" >> {self.calls_log}\n"
            "if [ \"$*\" = \"-o link show type vxlan\" ]; then\n"
            f"  cat {self.ip_links_file}\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"${1:-}\" = \"link\" ] && [ \"${2:-}\" = \"delete\" ]; then\n"
            "  if [ \"${MOCK_IP_DELETE_FAIL:-0}\" = \"1\" ]; then\n"
            "    exit 1\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
        )
        _write_executable(
            self.mock_bin / "docker",
            "#!/usr/bin/env bash\n"
            f"echo \"docker $*\" >> {self.calls_log}\n"
            "exit 0\n",
        )
        _write_executable(
            self.startup_hook,
            "#!/usr/bin/env bash\n"
            f"echo \"startup\" >> {self.calls_log}\n",
        )
        _write_executable(
            self.restore_hook,
            "#!/usr/bin/env bash\n"
            f"echo \"restore\" >> {self.calls_log}\n",
        )

    def _run_script(
        self,
        script: Path,
        extra_env: dict[str, str] | None = None,
        path_override: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": path_override or f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "SWARM_STARTUP_SCRIPT": str(self.startup_hook),
            "APOLLO_RESTORE_SCRIPT": str(self.restore_hook),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_gaia_restart_deletes_only_down_vx_links_between_stop_and_start(self) -> None:
        res = self._run_script(GAIA_RESTART_SCRIPT)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        calls = self.calls_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            calls,
            [
                "sync",
                "systemctl stop docker.socket docker.service",
                "ip -o link show type vxlan",
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
                "systemctl start docker",
                "startup",
            ],
        )
        deleted = [c for c in calls if c.startswith("ip link delete ")]
        self.assertEqual(
            deleted,
            [
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
            ],
        )
        self.assertIn("vx-001036-y2zly", res.stdout)
        self.assertIn("vx-001037-abcde", res.stdout)

    def test_muspelheim_restart_deletes_only_down_vx_links_between_stop_and_start(self) -> None:
        res = self._run_script(MUSPELHEIM_RESTART_SCRIPT)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        calls = self.calls_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            calls,
            [
                "sync",
                "docker info",
                "docker stop slskd sabnzbd qbittorrent gluetun",
                "systemctl stop docker.socket docker.service",
                "ip -o link show type vxlan",
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
                "systemctl start docker",
                "docker info",
                "docker info",
                "restore",
                "docker container prune -f",
            ],
        )
        deleted = [c for c in calls if c.startswith("ip link delete ")]
        self.assertEqual(
            deleted,
            [
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
            ],
        )

    def test_systemctl_socket_stop_failure_falls_back_to_stop_docker(self) -> None:
        for script in (GAIA_RESTART_SCRIPT, MUSPELHEIM_RESTART_SCRIPT):
            with self.subTest(script=script.name):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_script(
                    script,
                    extra_env={"MOCK_SYSTEMCTL_FAIL_SOCKET": "1"},
                )
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                calls = self.calls_log.read_text(encoding="utf-8").splitlines()
                stop_socket_idx = calls.index("systemctl stop docker.socket docker.service")
                stop_docker_idx = calls.index("systemctl stop docker")
                show_idx = calls.index("ip -o link show type vxlan")
                start_idx = calls.index("systemctl start docker")
                self.assertLess(stop_socket_idx, stop_docker_idx)
                self.assertLess(stop_docker_idx, show_idx)
                self.assertLess(show_idx, start_idx)
                self.assertIn("ip link delete vx-001036-y2zly", calls)
                self.assertIn("ip link delete vx-001037-abcde", calls)

    def test_empty_vxlan_output_and_delete_failure_do_not_abort_restart(self) -> None:
        self.ip_links_file.write_text("", encoding="utf-8")
        for script in (GAIA_RESTART_SCRIPT, MUSPELHEIM_RESTART_SCRIPT):
            with self.subTest(case="empty", script=script.name):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_script(script)
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                calls = self.calls_log.read_text(encoding="utf-8").splitlines()
                self.assertFalse(any(c.startswith("ip link delete ") for c in calls))
                self.assertIn("systemctl start docker", calls)

        self.ip_links_file.write_text(SAMPLE_VXLAN_LINKS + "\n", encoding="utf-8")
        for script in (GAIA_RESTART_SCRIPT, MUSPELHEIM_RESTART_SCRIPT):
            with self.subTest(case="delete-fail", script=script.name):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_script(
                    script,
                    extra_env={"MOCK_IP_DELETE_FAIL": "1"},
                )
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                calls = self.calls_log.read_text(encoding="utf-8").splitlines()
                self.assertIn("ip link delete vx-001036-y2zly", calls)
                self.assertIn("systemctl start docker", calls)

    def test_missing_ip_command_skips_vxlan_sweep_cleanly(self) -> None:
        no_ip_bin = self.tmpdir / "no_ip_bin"
        no_ip_bin.mkdir()
        for name in ("id", "sync", "sleep", "systemctl", "docker"):
            os.symlink(self.mock_bin / name, no_ip_bin / name)
        for util in ("bash", "env", "dirname", "cat"):
            resolved = shutil.which(util)
            self.assertIsNotNone(resolved, f"Required utility {util} not found on PATH")
            os.symlink(resolved, no_ip_bin / util)

        for script in (GAIA_RESTART_SCRIPT, MUSPELHEIM_RESTART_SCRIPT):
            with self.subTest(script=script.name):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_script(script, path_override=str(no_ip_bin))
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                calls = self.calls_log.read_text(encoding="utf-8").splitlines()
                self.assertFalse(any(c.startswith("ip ") for c in calls))
                self.assertIn("systemctl stop docker.socket docker.service", calls)
                self.assertIn("systemctl start docker", calls)

    def test_non_root_user_exits_nonzero_before_touching_docker(self) -> None:
        for script in (GAIA_RESTART_SCRIPT, MUSPELHEIM_RESTART_SCRIPT):
            with self.subTest(script=script.name):
                self.calls_log.unlink(missing_ok=True)
                res = self._run_script(script, extra_env={"MOCK_UID": "1000"})
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("must be run as root", res.stdout + res.stderr)
                self.assertFalse(self.calls_log.exists())


if __name__ == "__main__":
    unittest.main()

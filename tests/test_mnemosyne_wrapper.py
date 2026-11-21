#!/usr/bin/env python3
"""Unit tests for the hardened host mnemosyne CLI wrapper."""

import json
import os
import pty
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MNEMOSYNE_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "mnemosyne.sh"
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"


class TestMnemosyneWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-mnemosyne-wrapper-"))
        self.fake_docker = self.tmpdir / "fake-docker"
        self.state_file = self.tmpdir / "state.json"
        self.log_file = self.tmpdir / "calls.jsonl"
        self._write_fake_docker()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_fake_docker(self) -> None:
        self.fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "state_file = Path(os.environ['FAKE_DOCKER_STATE'])\n"
            "log_file = Path(os.environ['FAKE_DOCKER_LOG'])\n"
            "with log_file.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "\n"
            "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if cmd == 'ps':\n"
            "    state = json.loads(state_file.read_text(encoding='utf-8'))\n"
            "    responses = state.get('ps_responses', [])\n"
            "    idx = state.get('ps_calls', 0)\n"
            "    state['ps_calls'] = idx + 1\n"
            "    state_file.write_text(json.dumps(state), encoding='utf-8')\n"
            "    entry = responses[idx] if idx < len(responses) else (responses[-1] if responses else {'rc': 0, 'stdout': ''})\n"
            "    if entry.get('stdout'):\n"
            "        sys.stdout.write(entry['stdout'])\n"
            "    if entry.get('stderr'):\n"
            "        sys.stderr.write(entry['stderr'])\n"
            "    sys.exit(entry.get('rc', 0))\n"
            "if cmd == 'exec':\n"
            "    sys.stdout.write('fake-mnemosyne-ok\\n')\n"
            "    sys.exit(0)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        self.fake_docker.chmod(0o755)

    def _set_ps_responses(self, responses: list[dict]) -> None:
        self.state_file.write_text(
            json.dumps({"ps_calls": 0, "ps_responses": responses}),
            encoding="utf-8",
        )

    def _read_calls(self) -> list[list[str]]:
        if not self.log_file.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _base_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(self.tmpdir),
            "DOCKER_BIN": str(self.fake_docker),
            "FAKE_DOCKER_STATE": str(self.state_file),
            "FAKE_DOCKER_LOG": str(self.log_file),
            "MNEMOSYNE_WAIT_RETRIES": "3",
            "MNEMOSYNE_WAIT_SLEEP": "0",
        }
        if extra_env:
            env.update(extra_env)
        return env

    def test_scripts_syntax_and_setup_gaia_wiring(self) -> None:
        mode = stat.S_IMODE(MNEMOSYNE_SCRIPT.stat().st_mode)
        self.assertEqual(mode & 0o111, 0o111, "mnemosyne.sh must be executable")
        for script in (MNEMOSYNE_SCRIPT, SETUP_GAIA_SCRIPT):
            with self.subTest(script=script.name):
                res = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)

        setup_text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"mnemosyne.sh:mnemosyne"', setup_text)
        self.assertIn("install_central_cli", setup_text)
        self.assertIn("${CENTRAL_BIN_DIR:-/usr/local/bin}", setup_text)
        self.assertIn('ln -sf "${target_bin}" "${USER_LOCAL_BIN}/${bin_name}"', setup_text)

    def test_running_container_immediately_found_forwards_exact_args(self) -> None:
        self._set_ps_responses([{"rc": 0, "stdout": "cid-running-123\ncid-older-456\n"}])
        res = subprocess.run(
            [str(MNEMOSYNE_SCRIPT), "recall", "hello world", "5", "--json"],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "fake-mnemosyne-ok")

        calls = self._read_calls()
        self.assertEqual(
            calls,
            [
                [
                    "ps",
                    "-q",
                    "-f",
                    "name=odin_huginn-gateway",
                    "-f",
                    "status=running",
                ],
                [
                    "exec",
                    "-i",
                    "cid-running-123",
                    "/opt/hermes/.venv/bin/mnemosyne",
                    "recall",
                    "hello world",
                    "5",
                    "--json",
                ],
            ],
        )

    def test_zero_args_forwarded_cleanly(self) -> None:
        self._set_ps_responses([{"rc": 0, "stdout": "cid-zero-args\n"}])
        res = subprocess.run(
            [str(MNEMOSYNE_SCRIPT)],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self._read_calls()
        self.assertEqual(
            calls[1],
            ["exec", "-i", "cid-zero-args", "/opt/hermes/.venv/bin/mnemosyne"],
        )

    def test_interactive_tty_uses_it_flags(self) -> None:
        self._set_ps_responses([{"rc": 0, "stdout": "cid-tty-999\n"}])
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                [str(MNEMOSYNE_SCRIPT), "stats"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=subprocess.PIPE,
                env=self._base_env(),
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1
            output = bytearray()
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            _, stderr_bytes = proc.communicate(timeout=10)
        finally:
            if slave_fd != -1:
                os.close(slave_fd)
            os.close(master_fd)

        self.assertEqual(proc.returncode, 0, stderr_bytes.decode("utf-8", errors="replace"))
        self.assertIn("fake-mnemosyne-ok", output.decode("utf-8", errors="replace"))
        calls = self._read_calls()
        self.assertEqual(
            calls[1],
            ["exec", "-it", "cid-tty-999", "/opt/hermes/.venv/bin/mnemosyne", "stats"],
        )

    def test_transient_empty_or_stopped_container_retries_and_succeeds(self) -> None:
        self._set_ps_responses(
            [
                {"rc": 0, "stdout": ""},
                {"rc": 1, "stdout": ""},
                {"rc": 0, "stdout": "cid-recovered-789\n"},
            ]
        )
        res = subprocess.run(
            [str(MNEMOSYNE_SCRIPT), "recall", "rolling update"],
            env=self._base_env({"MNEMOSYNE_WAIT_RETRIES": "3", "MNEMOSYNE_WAIT_SLEEP": "0"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        calls = self._read_calls()
        ps_calls = [c for c in calls if c[0] == "ps"]
        exec_calls = [c for c in calls if c[0] == "exec"]
        self.assertEqual(len(ps_calls), 3)
        self.assertEqual(
            exec_calls,
            [
                [
                    "exec",
                    "-i",
                    "cid-recovered-789",
                    "/opt/hermes/.venv/bin/mnemosyne",
                    "recall",
                    "rolling update",
                ]
            ],
        )

    def test_unavailable_after_retries_exits_nonzero_with_diagnostic(self) -> None:
        self._set_ps_responses([{"rc": 0, "stdout": ""}])
        res = subprocess.run(
            [str(MNEMOSYNE_SCRIPT), "stats"],
            env=self._base_env({"MNEMOSYNE_WAIT_RETRIES": "2", "MNEMOSYNE_WAIT_SLEEP": "0"}),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("odin_huginn-gateway", res.stderr)
        self.assertIn("timeout 5 docker service ps odin_huginn-gateway", res.stderr)
        calls = self._read_calls()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c[0] == "ps" for c in calls))


if __name__ == "__main__":
    unittest.main()

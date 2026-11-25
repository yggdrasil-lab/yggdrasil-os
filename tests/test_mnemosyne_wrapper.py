#!/usr/bin/env python3
"""Unit tests for the hardened host mnemosyne CLI wrapper."""

import contextlib
import json
import os
import pty
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
MNEMOSYNE_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "mnemosyne.sh"
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"


class TestMnemosyneWrapper(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-mnemosyne-wrapper-"))
        self.fake_docker = self.tmpdir / "fake-docker"
        self.state_file = self.tmpdir / "state.json"
        self.log_file = self.tmpdir / "calls.jsonl"
        self.db_path = self.tmpdir / "mnemosyne.db"
        self._write_fake_docker()
        self._init_synthetic_db()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _init_synthetic_db(self) -> None:
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE working_memory (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    source TEXT,
                    timestamp TEXT,
                    session_id TEXT,
                    importance REAL,
                    metadata_json TEXT,
                    veracity TEXT,
                    created_at TEXT,
                    author_id TEXT,
                    author_type TEXT,
                    scope TEXT,
                    channel_id TEXT,
                    valid_until TEXT,
                    superseded_by TEXT,
                    recall_count INTEGER,
                    last_recalled TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE episodic_memory (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    source TEXT,
                    timestamp TEXT,
                    session_id TEXT,
                    importance REAL,
                    metadata_json TEXT,
                    veracity TEXT,
                    created_at TEXT,
                    author_id TEXT,
                    author_type TEXT,
                    scope TEXT,
                    channel_id TEXT,
                    valid_until TEXT,
                    superseded_by TEXT,
                    recall_count INTEGER,
                    last_recalled TEXT,
                    event_date TEXT,
                    event_date_precision TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE memories (
                    id TEXT PRIMARY KEY,
                    content TEXT,
                    source TEXT,
                    timestamp TEXT,
                    session_id TEXT,
                    importance REAL,
                    metadata_json TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO working_memory (
                    id, content, source, timestamp, session_id, importance,
                    metadata_json, veracity, created_at, author_id, author_type,
                    scope, channel_id, valid_until, superseded_by, recall_count,
                    last_recalled
                ) VALUES (
                    'syn-work-001', 'Synthetic working memory note', 'fact',
                    '2026-01-01T00:00:00', 'sess-1', 0.8, '{}', 'stated',
                    '2026-01-01 00:00:00', 'alex', 'user', 'global',
                    'default', NULL, NULL, 1, NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO episodic_memory (
                    id, content, source, timestamp, session_id, importance,
                    metadata_json, veracity, created_at, author_id, author_type,
                    scope, channel_id, valid_until, superseded_by, recall_count,
                    last_recalled, event_date, event_date_precision
                ) VALUES (
                    'syn-ep-002', 'Synthetic episodic memory note', 'insight',
                    '2026-01-02T00:00:00', 'sess-2', 0.7, '{}', 'tool',
                    '2026-01-02 00:00:00', NULL, NULL, 'global',
                    'default', NULL, NULL, 2, NULL, '2026-01-02', 'day'
                )
                """
            )
            conn.commit()

    def _write_fake_docker(self) -> None:
        self.fake_docker.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import os\n"
            "import subprocess\n"
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
            "    if '/opt/hermes/.venv/bin/python' in sys.argv:\n"
            "        py_idx = sys.argv.index('/opt/hermes/.venv/bin/python')\n"
            "        res = subprocess.run([sys.executable] + sys.argv[py_idx + 1:], check=False)\n"
            "        sys.exit(res.returncode)\n"
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
            "MNEMOSYNE_DB_PATH": str(self.db_path),
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

        from yggdrasil_os import (
            CommandResult,
            LocalOrRemoteAssetFetcher,
            ProvisionContext,
            load_manifest,
            provision_host,
        )

        manifest = load_manifest("gaia")
        self.assertTrue(
            any(c.src == "hosts/scripts/mnemosyne.sh" and c.bin_name == "mnemosyne" for c in manifest.central_clis)
        )

        class _NoopRunner:
            def run(self, argv, *, env=None, check=True, input_text=None):
                return CommandResult(0, "", "")

            def has_command(self, name):
                return False

        central_bin = self.tmpdir / "usr_local_bin"
        user_bin = self.tmpdir / "user_local_bin"
        ctx = ProvisionContext(
            repo_root=REPO_ROOT,
            root_dir=self.tmpdir / "root",
            central_bin_dir=central_bin,
            user_local_bin=user_bin,
            vault_path=self.tmpdir / "vault",
            cleanup_bin_path=self.tmpdir / "cleanup" / "gaia-cleanup.sh",
            cleanup_systemd_dir=self.tmpdir / "systemd",
            euid=0,
        )
        provision_host(
            "gaia",
            ctx,
            _NoopRunner(),
            LocalOrRemoteAssetFetcher(repo_root=REPO_ROOT, raw_base="https://example.invalid"),
        )
        installed_bin = central_bin / "mnemosyne"
        linked_bin = user_bin / "mnemosyne"
        self.assertTrue(installed_bin.is_file())
        self.assertEqual(stat.S_IMODE(installed_bin.stat().st_mode), 0o755)
        self.assertTrue(linked_bin.is_symlink())
        self.assertEqual(Path(os.readlink(linked_bin)), installed_bin)

    def test_help_and_no_args_fast_path_skips_docker(self) -> None:
        self._set_ps_responses([{"rc": 1, "stdout": ""}])
        for args in ([], ["-h"], ["--help"], ["help"]):
            with self.subTest(args=args):
                res = subprocess.run(
                    [str(MNEMOSYNE_SCRIPT), *args],
                    env=self._base_env(),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                for expected in (
                    "recall",
                    "remember",
                    "get <id>",
                    "status",
                    "stats",
                    "export",
                    "CONTAINER_NAME",
                    "mnemosyne_get",
                    "mnemosyne_recall",
                ):
                    self.assertIn(expected, res.stdout)
                self.assertEqual(self._read_calls(), [])

    def test_get_missing_or_empty_id_fails_loudly_without_docker(self) -> None:
        self._set_ps_responses([{"rc": 1, "stdout": ""}])
        for args in (["get"], ["get", ""], ["get", "id-1", "extra"]):
            with self.subTest(args=args):
                res = subprocess.run(
                    [str(MNEMOSYNE_SCRIPT), *args],
                    env=self._base_env(),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 2)
                self.assertIn("Usage: mnemosyne get <memory_id>", res.stderr)
                self.assertEqual(self._read_calls(), [])

    def test_get_queries_python_without_onnx_and_returns_json(self) -> None:
        self._set_ps_responses(
            [
                {"rc": 0, "stdout": "cid-get-001\n"},
                {"rc": 0, "stdout": "cid-get-002\n"},
            ]
        )
        for memory_id, expected_tier in (
            ("syn-work-001", "working"),
            ("syn-ep-002", "episodic"),
        ):
            with self.subTest(memory_id=memory_id):
                res = subprocess.run(
                    [str(MNEMOSYNE_SCRIPT), "get", memory_id],
                    env=self._base_env(),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                payload = json.loads(res.stdout)
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["memory"]["id"], memory_id)
                self.assertEqual(payload["memory"]["tier"], expected_tier)

        exec_calls = [c for c in self._read_calls() if c[0] == "exec"]
        self.assertEqual(len(exec_calls), 2)
        for call in exec_calls:
            self.assertEqual(call[0:5], ["exec", "-i", call[2], "/opt/hermes/.venv/bin/python", "-c"])
            self.assertNotIn("/opt/hermes/.venv/bin/mnemosyne", call)

    def test_get_not_found_exits_one_with_clear_error(self) -> None:
        self._set_ps_responses([{"rc": 0, "stdout": "cid-get-missing\n"}])
        res = subprocess.run(
            [str(MNEMOSYNE_SCRIPT), "get", "missing-id-999"],
            env=self._base_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 1)
        self.assertIn("not found", res.stderr)

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

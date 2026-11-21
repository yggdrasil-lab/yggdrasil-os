#!/usr/bin/env python3
"""Unit tests for hosts/scripts/swarm-exec.sh TTY detection and delimiter handling."""

import errno
import os
import pty
import select
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SWARM_EXEC_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "swarm-exec.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class _SwarmExecTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-swarm-exec-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.exec_args_log = self.tmpdir / "exec_args.log"
        self.stdin_log = self.tmpdir / "stdin.log"

        mock_docker = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cmd=\"${1:-}\"\n"
            "if [ \"$cmd\" = \"ps\" ]; then\n"
            "  echo 'abc123def456 test_service.1.xyz'\n"
            "  exit 0\n"
            "elif [ \"$cmd\" = \"exec\" ]; then\n"
            f"  printf '%s\\n' \"$@\" > {self.exec_args_log}\n"
            "  if [ \"${MOCK_CAPTURE_STDIN:-0}\" = \"1\" ]; then\n"
            f"    cat > {self.stdin_log}\n"
            f"    cat {self.stdin_log}\n"
            "  elif [ -n \"${MOCK_STDOUT_TEXT:-}\" ]; then\n"
            "    printf '%s\\n' \"$MOCK_STDOUT_TEXT\"\n"
            "  fi\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n"
        )
        _write_executable(self.mock_bin / "docker", mock_docker)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _base_env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
        }
        if extra:
            env.update(extra)
        return env

    def _read_pty_to_eof(self, master_fd: int, timeout: float = 10.0) -> bytes:
        chunks: list[bytes] = []
        while True:
            ready, _, _ = select.select([master_fd], [], [], timeout)
            if not ready:
                self.fail("Timed out waiting for PTY output from swarm-exec.sh")
            try:
                data = os.read(master_fd, 4096)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            chunks.append(data)
        return b"".join(chunks)

    def _run_piped(
        self,
        args: list[str],
        stdin_data: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        proc = subprocess.Popen(
            ["bash", str(SWARM_EXEC_SCRIPT), *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._base_env(extra_env),
            text=True,
        )
        stdout, stderr = proc.communicate(input=stdin_data, timeout=10)
        return proc.returncode, stdout, stderr

    def _run_pty_both(
        self,
        args: list[str],
        extra_env: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["bash", str(SWARM_EXEC_SCRIPT), *args],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=self._base_env(extra_env),
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        try:
            output = self._read_pty_to_eof(master_fd)
            rc = proc.wait(timeout=10)
        finally:
            os.close(master_fd)
        return rc, output


class TestSwarmExecTtyDetection(_SwarmExecTestBase):

    def test_pty_stdin_and_pty_stdout_uses_it(self) -> None:
        master_fd, slave_fd = pty.openpty()
        env = self._base_env({"MOCK_STDOUT_TEXT": "pty-both-ok"})
        try:
            proc = subprocess.Popen(
                ["bash", str(SWARM_EXEC_SCRIPT), "test_service", "sh", "-c", "echo ok"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        try:
            output = self._read_pty_to_eof(master_fd)
            rc = proc.wait(timeout=10)
        finally:
            os.close(master_fd)

        self.assertEqual(rc, 0, output.decode("utf-8", errors="replace"))
        self.assertIn(b"pty-both-ok", output)
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            args,
            ["exec", "-it", "abc123def456", "sh", "-c", "echo ok"],
        )

    def test_piped_stdin_and_piped_stdout_uses_i_only_and_forwards_stdin(self) -> None:
        payload = "line one\nline two\n"
        env = self._base_env({"MOCK_CAPTURE_STDIN": "1"})
        proc = subprocess.Popen(
            ["bash", str(SWARM_EXEC_SCRIPT), "test_service", "cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        stdout, stderr = proc.communicate(input=payload, timeout=10)

        self.assertEqual(proc.returncode, 0, stderr)
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["exec", "-i", "abc123def456", "cat"])
        self.assertEqual(self.stdin_log.read_text(encoding="utf-8"), payload)
        self.assertEqual(stdout, payload)

    def test_pty_stdin_and_piped_stdout_omits_i_and_t(self) -> None:
        master_fd, slave_fd = pty.openpty()
        env = self._base_env({"MOCK_STDOUT_TEXT": "pty-in-pipe-out-ok"})
        try:
            proc = subprocess.Popen(
                ["bash", str(SWARM_EXEC_SCRIPT), "test_service", "ls", "-la"],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                close_fds=True,
                start_new_session=True,
                text=True,
            )
        finally:
            os.close(slave_fd)

        try:
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            os.close(master_fd)

        self.assertEqual(proc.returncode, 0, stderr)
        self.assertEqual(stdout, "pty-in-pipe-out-ok\n")
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["exec", "abc123def456", "ls", "-la"])

    def test_piped_stdin_and_pty_stdout_uses_i_only(self) -> None:
        payload = "piped-to-pty\n"
        master_fd, slave_fd = pty.openpty()
        env = self._base_env({"MOCK_CAPTURE_STDIN": "1"})
        try:
            proc = subprocess.Popen(
                ["bash", str(SWARM_EXEC_SCRIPT), "test_service", "cat"],
                stdin=subprocess.PIPE,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(slave_fd)

        try:
            assert proc.stdin is not None
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.close()
            output = self._read_pty_to_eof(master_fd)
            rc = proc.wait(timeout=10)
        finally:
            os.close(master_fd)

        self.assertEqual(rc, 0, output.decode("utf-8", errors="replace"))
        self.assertIn(b"piped-to-pty", output)
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["exec", "-i", "abc123def456", "cat"])
        self.assertEqual(self.stdin_log.read_text(encoding="utf-8"), payload)


class TestSwarmExecDelimiter(_SwarmExecTestBase):
    def test_leading_delimiter_stripped_on_piped_path(self) -> None:
        payload = "SELECT 1;\n"
        rc, stdout, stderr = self._run_piped(
            ["svc", "--", "psql", "-U", "iris"],
            stdin_data=payload,
            extra_env={"MOCK_CAPTURE_STDIN": "1"},
        )

        self.assertEqual(rc, 0, stderr)
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["exec", "-i", "abc123def456", "psql", "-U", "iris"])
        self.assertEqual(self.stdin_log.read_text(encoding="utf-8"), payload)
        self.assertEqual(stdout, payload)

    def test_leading_delimiter_stripped_on_tty_path(self) -> None:
        rc, output = self._run_pty_both(
            ["svc", "--", "psql", "-U", "iris"],
            extra_env={"MOCK_STDOUT_TEXT": "tty-delim-ok"},
        )

        self.assertEqual(rc, 0, output.decode("utf-8", errors="replace"))
        self.assertIn(b"tty-delim-ok", output)
        args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(args, ["exec", "-it", "abc123def456", "psql", "-U", "iris"])

    def test_inner_delimiter_preserved(self) -> None:
        cases = [
            ("with_leading_delimiter", ["svc", "--", "grep", "--", "-foo"]),
            ("without_leading_delimiter", ["svc", "grep", "--", "-foo"]),
        ]
        for label, cli_args in cases:
            with self.subTest(case=label):
                self.exec_args_log.unlink(missing_ok=True)
                rc, stdout, stderr = self._run_piped(
                    cli_args,
                    extra_env={"MOCK_STDOUT_TEXT": "grep-ok"},
                )
                self.assertEqual(rc, 0, stderr)
                self.assertEqual(stdout, "grep-ok\n")
                args = self.exec_args_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(args, ["exec", "-i", "abc123def456", "grep", "--", "-foo"])

    def test_missing_command_after_delimiter_prints_usage_and_exits_one(self) -> None:
        rc, stdout, stderr = self._run_piped(["svc", "--"])

        self.assertEqual(rc, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Usage: swarm-exec <service_pattern> [--] <command...>", stderr)
        self.assertFalse(self.exec_args_log.exists())

    def test_missing_command_without_delimiter_prints_usage_and_exits_one(self) -> None:
        rc, stdout, stderr = self._run_piped(["svc"])

        self.assertEqual(rc, 1)
        self.assertIn("Usage: swarm-exec <service_pattern> [--] <command...>", stdout + stderr)
        self.assertFalse(self.exec_args_log.exists())


if __name__ == "__main__":
    unittest.main()

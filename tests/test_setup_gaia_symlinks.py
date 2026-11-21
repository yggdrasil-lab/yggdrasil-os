#!/usr/bin/env python3
"""Unit tests for uniform /usr/local/bin install and ~/.local/bin symlinks in hosts/setup_gaia.sh."""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"
CENTRAL_CLIS = (
    ("charon-restore.sh", "charon-restore"),
    ("swarm-exec.sh", "swarm-exec"),
    ("mnemosyne.sh", "mnemosyne"),
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _extract_central_cli_block() -> str:
    """Extract install_central_cli and CENTRAL_CLI_SPECS loop from setup_gaia.sh."""
    text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"(install_central_cli\(\) \{.*?\nfor cli_spec in \"\$\{CENTRAL_CLI_SPECS\[@\]\}\"; do\n.*?\ndone)",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("Could not locate install_central_cli block in hosts/setup_gaia.sh")
    return match.group(1)


class TestSetupGaiaSymlinks(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-setup-gaia-symlinks-"))
        self.mock_tools = self.tmpdir / "tools"
        self.mock_tools.mkdir()
        self.central_bin = self.tmpdir / "usr_local_bin"
        self.user_local_bin = self.tmpdir / "home" / "tester" / ".local" / "bin"
        self.chown_log = self.tmpdir / "chown.log"
        self.curl_log = self.tmpdir / "curl.log"
        self.target_user = subprocess.check_output(["id", "-un"], text=True).strip()
        self.target_group = subprocess.check_output(["id", "-gn"], text=True).strip()
        self._install_mock_chown()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install_mock_chown(self) -> None:
        real_chown = shutil.which("chown")
        self.assertIsNotNone(real_chown, "System chown binary must exist on PATH")
        _write_executable(
            self.mock_tools / "chown",
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{self.chown_log}"\n'
            f'exec "{real_chown}" "$@"\n',
        )

    def _install_mock_curl(self, fail_if_called: bool = False) -> None:
        if fail_if_called:
            _write_executable(
                self.mock_tools / "curl",
                "#!/usr/bin/env bash\n"
                'echo "Unexpected curl invocation: $*" >&2\n'
                "exit 99\n",
            )
            return

        _write_executable(
            self.mock_tools / "curl",
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            f"log_path = Path({str(self.curl_log)!r})\n"
            "args = sys.argv[1:]\n"
            "dest = None\n"
            "url = None\n"
            "idx = 0\n"
            "while idx < len(args):\n"
            "    if args[idx] == '-o' and idx + 1 < len(args):\n"
            "        dest = Path(args[idx + 1])\n"
            "        idx += 2\n"
            "        continue\n"
            "    if not args[idx].startswith('-'):\n"
            "        url = args[idx]\n"
            "    idx += 1\n"
            "assert dest is not None and url is not None, f'Unexpected curl args: {args}'\n"
            "with log_path.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(f'{url} -> {dest}\\n')\n"
            "script_name = url.rsplit('/', 1)[-1]\n"
            "dest.write_text(f'#!/usr/bin/env bash\\necho curled:{script_name}\\n', encoding='utf-8')\n"
            "os.chmod(dest, 0o644)\n",
        )

    def _run_extracted_block(
        self,
        script_dir: str,
        raw_base: str = "https://example.invalid/yggdrasil-os",
    ) -> subprocess.CompletedProcess[str]:
        block = _extract_central_cli_block()
        runner_script = "\n".join(
            [
                "set -e",
                f'SCRIPT_DIR="{script_dir}"',
                f'RAW_BASE="{raw_base}"',
                f'CENTRAL_BIN_DIR="{self.central_bin}"',
                f'USER_LOCAL_BIN="{self.user_local_bin}"',
                f'TARGET_USER="{self.target_user}"',
                f'TARGET_GROUP="{self.target_group}"',
                block,
            ]
        )
        env = {
            "PATH": f"{self.mock_tools}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
        }
        return subprocess.run(
            ["bash"],
            input=runner_script,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _assert_symlinks_point_to_central_bin(self, expected_prefix: str) -> None:
        chown_entries = (
            self.chown_log.read_text(encoding="utf-8").splitlines()
            if self.chown_log.exists()
            else []
        )
        for script_name, bin_name in CENTRAL_CLIS:
            with self.subTest(cli=bin_name):
                central_path = self.central_bin / bin_name
                user_path = self.user_local_bin / bin_name

                self.assertTrue(central_path.exists(), f"{central_path} missing")
                self.assertFalse(central_path.is_symlink(), f"{central_path} must be a file")
                self.assertEqual(stat.S_IMODE(central_path.stat().st_mode), 0o755)

                self.assertTrue(user_path.is_symlink(), f"{user_path} must be a symlink")
                self.assertEqual(Path(os.readlink(user_path)), central_path)
                self.assertIn(
                    f"-h {self.target_user}:{self.target_group} {user_path}",
                    chown_entries,
                )

                out = subprocess.check_output([str(user_path)], text=True).strip()
                expected_arg = bin_name if expected_prefix == "fresh" else script_name
                self.assertEqual(out, f"{expected_prefix}:{expected_arg}")

    def test_setup_gaia_uses_uniform_helper_and_preserves_defaults(self) -> None:
        res = subprocess.run(
            ["bash", "-n", str(SETUP_GAIA_SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

        text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("install_central_cli()", text)
        self.assertIn('local central_bin_dir="${CENTRAL_BIN_DIR:-/usr/local/bin}"', text)
        self.assertIn('ln -sf "${target_bin}" "${USER_LOCAL_BIN}/${bin_name}"', text)
        self.assertIn(
            'chown -h "${TARGET_USER}:${TARGET_GROUP}" "${USER_LOCAL_BIN}/${bin_name}" 2>/dev/null || true',
            text,
        )
        for script_name, bin_name in CENTRAL_CLIS:
            self.assertIn(f'"{script_name}:{bin_name}"', text)
            self.assertNotIn(f"scripts/{script_name}", text)
            self.assertNotIn(f"/usr/local/bin/{bin_name}", text)

        default_dir = subprocess.check_output(
            ["bash", "-c", 'CENTRAL_BIN_DIR=""; echo "${CENTRAL_BIN_DIR:-/usr/local/bin}"'],
            text=True,
        ).strip()
        self.assertEqual(default_dir, "/usr/local/bin")

    def test_local_scripts_replace_stale_standalone_files_with_symlinks(self) -> None:
        self._install_mock_curl(fail_if_called=True)
        mock_script_dir = self.tmpdir / "repo" / "hosts"
        self.central_bin.mkdir(parents=True)
        self.user_local_bin.mkdir(parents=True)

        for script_name, bin_name in CENTRAL_CLIS:
            _write_executable(
                mock_script_dir / "scripts" / script_name,
                f"#!/usr/bin/env bash\necho fresh:{bin_name}\n",
            )
            _write_executable(
                self.central_bin / bin_name,
                f"#!/usr/bin/env bash\necho old-central:{bin_name}\n",
            )
            _write_executable(
                self.user_local_bin / bin_name,
                f"#!/usr/bin/env bash\necho stale-standalone:{bin_name}\n",
            )

        res = self._run_extracted_block(script_dir=str(mock_script_dir))
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self._assert_symlinks_point_to_central_bin("fresh")

    def test_zero_clone_curl_fallback_creates_missing_dir_and_symlinks(self) -> None:
        self._install_mock_curl(fail_if_called=False)
        self.assertFalse(self.user_local_bin.exists())

        res = self._run_extracted_block(
            script_dir="",
            raw_base="https://example.invalid/yggdrasil-os",
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertTrue(self.user_local_bin.is_dir())

        chown_entries = self.chown_log.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            f"{self.target_user}:{self.target_group} {self.user_local_bin}",
            chown_entries,
        )

        curl_entries = self.curl_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            curl_entries,
            [
                f"https://example.invalid/yggdrasil-os/hosts/scripts/{script_name} -> {self.central_bin / bin_name}"
                for script_name, bin_name in CENTRAL_CLIS
            ],
        )
        self._assert_symlinks_point_to_central_bin("curled")

    def test_idempotent_rerun_keeps_symlinks_pointing_to_central_bin(self) -> None:
        self._install_mock_curl(fail_if_called=True)
        mock_script_dir = self.tmpdir / "repo" / "hosts"
        for script_name, bin_name in CENTRAL_CLIS:
            _write_executable(
                mock_script_dir / "scripts" / script_name,
                f"#!/usr/bin/env bash\necho fresh:{bin_name}\n",
            )

        res1 = self._run_extracted_block(script_dir=str(mock_script_dir))
        self.assertEqual(res1.returncode, 0, res1.stderr + res1.stdout)
        self.chown_log.unlink(missing_ok=True)

        res2 = self._run_extracted_block(script_dir=str(mock_script_dir))
        self.assertEqual(res2.returncode, 0, res2.stderr + res2.stdout)
        self._assert_symlinks_point_to_central_bin("fresh")


if __name__ == "__main__":
    unittest.main()

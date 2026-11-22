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
FORGE_PR_SCRIPTS_REL = ".agents/plugins/court-of-odin/domain-skills/forge-pr-workflow/scripts"
LEGACY_FORGE_PR_PATH = "/opt/odin/hermes/skills/devops/forge-pr-workflow"
FORGE_PR_SYMLINKS = (
    ("publish_pwa_visual_review.py", "publish-pwa-visual-review"),
    ("create_forge_pr.py", "create-forge-pr"),
    ("verify_forge_pr.py", "verify-forge-pr"),
)
UNCHANGED_SKILL_SYMLINKS = (
    ("/opt/odin/hermes/skills/health/workout/scripts/kratos_workout_cli.py", "kratos-workout"),
    ("${VAULT_ROOT}/.agents/skills/romm-ops/scripts/romm_manage.py", "romm-manage"),
)


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _install_mock_chown(mock_tools: Path, chown_log: Path) -> None:
    real_chown = shutil.which("chown")
    if real_chown is None:
        raise AssertionError("System chown binary must exist on PATH")
    _write_executable(
        mock_tools / "chown",
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{chown_log}"\n'
        f'exec "{real_chown}" "$@"\n',
    )


def _run_bash_with_tools(
    runner_script: str,
    mock_tools: Path,
    home_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{mock_tools}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home_dir),
    }
    return subprocess.run(
        ["bash"],
        input=runner_script,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


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


def _extract_turnkey_symlink_block() -> str:
    """Extract the Odin turnkey USER_LOCAL_BIN symlink block from setup_gaia.sh."""
    text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r'(VAULT_ROOT="\$\{OBSIDIAN_VAULT_PATH:-/opt/atlas/vault\}"\n.*?\n'
        r'chown -h "\$\{TARGET_USER\}:\$\{TARGET_GROUP\}" "\$\{USER_LOCAL_BIN\}"/\* 2>/dev/null \|\| true)',
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("Could not locate turnkey USER_LOCAL_BIN symlink block in hosts/setup_gaia.sh")
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
        _install_mock_chown(self.mock_tools, self.chown_log)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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
        return _run_bash_with_tools(runner_script, self.mock_tools, self.tmpdir)

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


class TestSetupGaiaForgePrSymlinks(unittest.TestCase):
    """Regression tests for Forge PR workflow turnkey symlinks in hosts/setup_gaia.sh."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-setup-gaia-forge-pr-"))
        self.mock_tools = self.tmpdir / "tools"
        self.mock_tools.mkdir()
        self.vault_root = self.tmpdir / "vault"
        self.user_local_bin = self.tmpdir / "home" / "tester" / ".local" / "bin"
        self.user_local_bin.mkdir(parents=True)
        self.chown_log = self.tmpdir / "chown.log"
        self.target_user = subprocess.check_output(["id", "-un"], text=True).strip()
        self.target_group = subprocess.check_output(["id", "-gn"], text=True).strip()
        _install_mock_chown(self.mock_tools, self.chown_log)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_turnkey_block(self) -> subprocess.CompletedProcess[str]:
        block = _extract_turnkey_symlink_block()
        runner_script = "\n".join(
            [
                "set -e",
                f'OBSIDIAN_VAULT_PATH="{self.vault_root}"',
                f'USER_LOCAL_BIN="{self.user_local_bin}"',
                f'TARGET_USER="{self.target_user}"',
                f'TARGET_GROUP="{self.target_group}"',
                block,
            ]
        )
        return _run_bash_with_tools(runner_script, self.mock_tools, self.tmpdir)

    def test_setup_gaia_declares_domain_skill_forge_pr_symlinks_only(self) -> None:
        text = SETUP_GAIA_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(LEGACY_FORGE_PR_PATH, text)
        for script_name, bin_name in FORGE_PR_SYMLINKS:
            expected = (
                f'ln -sf "${{VAULT_ROOT}}/{FORGE_PR_SCRIPTS_REL}/{script_name}" '
                f'"${{USER_LOCAL_BIN}}/{bin_name}"'
            )
            with self.subTest(cli=bin_name):
                self.assertIn(expected, text)
        for target_expr, bin_name in UNCHANGED_SKILL_SYMLINKS:
            expected = f'ln -sf "{target_expr}" "${{USER_LOCAL_BIN}}/{bin_name}"'
            with self.subTest(unchanged=bin_name):
                self.assertIn(expected, text)

        default_vault = subprocess.check_output(
            ["bash", "-c", 'OBSIDIAN_VAULT_PATH=""; echo "${OBSIDIAN_VAULT_PATH:-/opt/atlas/vault}"'],
            text=True,
        ).strip()
        self.assertEqual(default_vault, "/opt/atlas/vault")

    def test_turnkey_block_links_forge_pr_clis_and_replaces_stale_targets(self) -> None:
        scripts_dir = self.vault_root / FORGE_PR_SCRIPTS_REL
        for script_name, bin_name in FORGE_PR_SYMLINKS:
            _write_executable(
                scripts_dir / script_name,
                f"#!/usr/bin/env bash\necho forge-pr:{bin_name}\n",
            )
        romm_script = self.vault_root / ".agents" / "skills" / "romm-ops" / "scripts" / "romm_manage.py"
        _write_executable(romm_script, "#!/usr/bin/env bash\necho romm-manage\n")

        stale_link = self.user_local_bin / "publish-pwa-visual-review"
        stale_link.symlink_to(f"{LEGACY_FORGE_PR_PATH}/scripts/publish_pwa_visual_review.py")
        _write_executable(
            self.user_local_bin / "create-forge-pr",
            "#!/usr/bin/env bash\necho stale-standalone\n",
        )

        res = self._run_turnkey_block()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)

        for script_name, bin_name in FORGE_PR_SYMLINKS:
            with self.subTest(cli=bin_name):
                link_path = self.user_local_bin / bin_name
                self.assertTrue(link_path.is_symlink(), f"{link_path} must be a symlink")
                self.assertEqual(Path(os.readlink(link_path)), scripts_dir / script_name)
                out = subprocess.check_output([str(link_path)], text=True).strip()
                self.assertEqual(out, f"forge-pr:{bin_name}")

        self.assertEqual(
            os.readlink(self.user_local_bin / "kratos-workout"),
            "/opt/odin/hermes/skills/health/workout/scripts/kratos_workout_cli.py",
        )
        self.assertEqual(Path(os.readlink(self.user_local_bin / "romm-manage")), romm_script)
        chown_entries = self.chown_log.read_text(encoding="utf-8")
        self.assertIn(f"-h {self.target_user}:{self.target_group}", chown_entries)


if __name__ == "__main__":
    unittest.main()

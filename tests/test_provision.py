#!/usr/bin/env python3
"""Unit and smoke tests for yggdrasil_os provisioning and zero-clone shims."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from yggdrasil_os import (  # noqa: E402
    AssetFetchError,
    CommandResult,
    LocalOrRemoteAssetFetcher,
    ProvisionContext,
    WAIT_FOR_NETWORK_CONF,
    fix_docker_dns_boot,
    install_cleanup_timer,
    install_startup_service,
    load_manifest,
    provision_host,
)

SHIM_SCRIPTS = (
    REPO_ROOT / "hosts" / "setup_gaia.sh",
    REPO_ROOT / "hosts" / "setup_muspelheim.sh",
    REPO_ROOT / "maintenance" / "install_cleanup_timer.sh",
    REPO_ROOT / "swarm" / "install_startup_service.sh",
    REPO_ROOT / "network" / "fix_docker_dns_boot.sh",
)

EXPECTED_TURNKEY_SYMLINKS = {
    "morning-copilot": "Forge/odin/hermes-agent/scripts/morning_copilot_aggregator.py",
    "mnemosyne-dreaming": "Forge/odin/hermes-agent/scripts/mnemosyne_consolidate_daemon.py",
    "mnemosyne-associative": "Forge/odin/hermes-agent/scripts/mnemosyne_associative.py",
    "mnemosyne-hygiene-audit": "Forge/odin/hermes-agent/scripts/mnemosyne_temporal_hygiene.py",
    "mimir-code": "Forge/odin/hermes-agent/scripts/mimir_code_graph.py",
    "forseti-visual-gatekeeper": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/forseti_visual_gatekeeper.py",
    "publish-pwa-visual-review": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/publish_pwa_visual_review.py",
    "create-forge-pr": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/create_forge_pr.py",
    "verify-forge-pr": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/verify_forge_pr.py",
    "forge-ship": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/forge_ship.py",
    "forge-merge-verify": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/forge_merge_verify.py",
    "romm-manage": ".agents/plugins/court-of-odin/skills/romm-ops/scripts/romm_manage.py",
    "safe-docker-run": ".agents/plugins/court-of-odin/skills/gaia-host-ops/scripts/safe_docker_run.py",
    "huginn-recon": ".agents/plugins/court-of-odin/skills/fleet/scripts/huginn_recon.py",
    "rollout-convergence-gate": ".agents/plugins/court-of-odin/skills/forge-pr-workflow/scripts/rollout_convergence_gate.py",
    "forseti-diff-bundle": ".agents/plugins/court-of-odin/skills/code-review/scripts/forseti_diff_bundle.py",
}


class FakeCommandRunner:
    """In-memory CommandRunner recording every invocation."""

    def __init__(
        self,
        *,
        available_commands: set[str] | None = None,
        docker_info_rc: int = 0,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str] | None, str | None]] = []
        self.available_commands = (
            {"systemctl", "git", "sudo", "docker", "bash"}
            if available_commands is None
            else available_commands
        )
        self.docker_info_rc = docker_info_rc

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        cmd = tuple(argv)
        env_copy = dict(env) if env is not None else None
        self.calls.append((cmd, env_copy, input_text))
        if cmd[:2] == ("docker", "info"):
            return CommandResult(self.docker_info_rc, "", "")
        if cmd[:2] == ("systemctl", "list-timers"):
            return CommandResult(
                0,
                "NEXT LEFT LAST PASSED UNIT ACTIVATES\n"
                "Mon 03:00 - - - gaia-cleanup.timer gaia-cleanup.service\n",
                "",
            )
        return CommandResult(0, "", "")

    def has_command(self, name: str) -> bool:
        return name in self.available_commands


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class TestHostProvisioning(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-yggdrasil-provision-"))
        self.root_dir = self.tmpdir / "root"
        self.root_dir.mkdir()
        self.fetcher = LocalOrRemoteAssetFetcher(
            repo_root=REPO_ROOT,
            raw_base="https://example.invalid/yggdrasil-os",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _gaia_ctx(self, *, sudo_user: str = "tester") -> ProvisionContext:
        return ProvisionContext(
            repo_root=REPO_ROOT,
            root_dir=self.root_dir,
            raw_base="https://example.invalid/yggdrasil-os",
            sudo_user=sudo_user,
            target_user=sudo_user or "tester",
            target_group="testgrp",
            user_local_bin=self.root_dir / "home" / "tester" / ".local" / "bin",
            central_bin_dir=self.root_dir / "usr" / "local" / "bin",
            vault_path=self.root_dir / "opt" / "atlas" / "vault",
            cleanup_bin_path=self.root_dir / "usr" / "local" / "bin" / "gaia-cleanup.sh",
            cleanup_systemd_dir=self.root_dir / "etc" / "systemd" / "system",
            euid=0,
        )

    def test_gaia_provisioning_enforces_all_invariants(self) -> None:
        ctx = self._gaia_ctx(sudo_user="tester")
        runner = FakeCommandRunner()

        # Pre-seed deprecated files, Hermes sockets, agy, vault .git, Forge artifacts, stale CLI
        deprecated = self.root_dir / "usr" / "local" / "bin" / "notify-discord.sh"
        deprecated.parent.mkdir(parents=True)
        deprecated.write_text("old", encoding="utf-8")
        stale_pruner = self.root_dir / "usr" / "local" / "bin" / "prune-network-endpoints"
        stale_pruner.write_text("old-pruner", encoding="utf-8")

        hermes_dir = self.root_dir / "opt" / "odin" / "hermes"
        hermes_dir.mkdir(parents=True)
        hermes_dir.chmod(0o775)
        sock = hermes_dir / "gateway.sock"
        lock = hermes_dir / "gateway.lock"
        sock.write_text("", encoding="utf-8")
        lock.write_text("", encoding="utf-8")
        sock.chmod(0o644)
        lock.chmod(0o644)

        agy = ctx.effective_user_local_bin / "agy"
        _write_executable(agy, "#!/usr/bin/env bash\n")
        _write_executable(ctx.effective_user_local_bin / "forge-ship", "#!/usr/bin/env bash\n")

        vault_git = ctx.effective_vault_path / ".git"
        vault_git.mkdir(parents=True)
        forge_dist = ctx.effective_vault_path / "Forge" / "plutus" / "web" / "dist"
        forge_dist.mkdir(parents=True)
        vite_stale = ctx.effective_vault_path / "Forge" / "kratos" / "web" / "node_modules" / ".vite_root_owned"
        vite_stale.mkdir(parents=True)
        (vite_stale / "marker").write_text("x", encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()):
            provision_host("gaia", ctx, runner, self.fetcher)

        self.assertFalse(deprecated.exists())
        self.assertFalse(stale_pruner.exists())
        gaia_manifest = load_manifest("gaia")
        self.assertIn("/usr/local/bin/prune-network-endpoints", gaia_manifest.remove_paths)
        self.assertNotIn("/usr/local/bin/prune-network-endpoints", [a.dest for a in gaia_manifest.assets])
        self.assertFalse((REPO_ROOT / "hosts" / "scripts" / "prune-network-endpoints.sh").exists())
        self.assertFalse((REPO_ROOT / "network" / "prune_network_endpoints.py").exists())
        self.assertTrue((self.root_dir / "opt" / "kratos" / "data").is_dir())
        self.assertTrue((self.root_dir / "opt" / "eir" / "data").is_dir())
        self.assertTrue((self.root_dir / "opt" / "iris" / "data").is_dir())
        self.assertTrue((self.root_dir / "opt" / "odysseus" / "data").is_dir())
        self.assertTrue((self.root_dir / "opt" / "odysseus" / "mappacks").is_dir())
        self.assertTrue((self.root_dir / "opt" / "apollo-core" / "jellystat-db").is_dir())
        self.assertTrue((self.root_dir / "opt" / "cerberus" / "postgres").is_dir())

        self.assertEqual(stat.S_IMODE(hermes_dir.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(sock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root_dir / "opt" / "odin" / "mnemosyne").stat().st_mode), 0o775)
        self.assertTrue(bool(stat.S_IMODE((ctx.effective_vault_path / "second-brain").stat().st_mode) & stat.S_IWOTH))

        chown_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] == "chown"]
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "opt" / "kratos")), chown_calls)
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "opt" / "eir")), chown_calls)
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "opt" / "iris")), chown_calls)
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "opt" / "odysseus")), chown_calls)
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "opt" / "odysseus" / "mappacks")), chown_calls)
        self.assertIn(("chown", "1000:1000", str(self.root_dir / "mnt" / "storage" / "registry")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "kratos")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "odysseus")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "apollo-core" / "jellystat-db")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "cerberus" / "postgres")), chown_calls)
        self.assertIn(("chown", "-R", "tester:testgrp", str(vault_git)), chown_calls)
        self.assertIn(("chown", "-R", "tester:testgrp", str(forge_dist)), chown_calls)
        self.assertFalse(vite_stale.exists())

        for script_name in ("deploy.sh", "ensure_secret.sh", "prune_secrets.sh", "verify_convergence.sh"):
            installed = ctx.effective_central_bin_dir / script_name
            self.assertTrue(installed.is_file(), f"Missing {script_name}")
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o755)

        for cli_name in ("charon-restore", "swarm-exec", "mnemosyne"):
            central = ctx.effective_central_bin_dir / cli_name
            link = ctx.effective_user_local_bin / cli_name
            self.assertTrue(central.is_file())
            self.assertEqual(stat.S_IMODE(central.stat().st_mode), 0o755)
            self.assertTrue(link.is_symlink())
            self.assertEqual(Path(os.readlink(link)), central)

        for link_name, rel_target in EXPECTED_TURNKEY_SYMLINKS.items():
            link = ctx.effective_user_local_bin / link_name
            self.assertTrue(link.is_symlink())
            self.assertEqual(Path(os.readlink(link)), ctx.effective_vault_path / rel_target)

        self.assertEqual(
            os.readlink(ctx.effective_user_local_bin / "kratos-workout"),
            "/opt/odin/hermes/skills/health/workout/scripts/kratos_workout_cli.py",
        )
        agy_link = self.root_dir / "usr" / "local" / "bin" / "agy"
        self.assertTrue(agy_link.is_symlink())
        self.assertEqual(Path(os.readlink(agy_link)), agy)

        prereq_call = ("bash", str(REPO_ROOT / "hosts" / "install_prerequisites.sh"))
        self.assertIn(prereq_call, [cmd for cmd, _, _ in runner.calls])

        pin_calls = [
            (cmd, env)
            for cmd, env, _ in runner.calls
            if cmd == (str(self.root_dir / "usr" / "local" / "bin" / "pin-registry-host"),)
        ]
        self.assertEqual(pin_calls, [((str(self.root_dir / "usr" / "local" / "bin" / "pin-registry-host"),), {"NODE_ROLE": "manager"})])

        systemctl_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] == "systemctl"]
        self.assertIn(("systemctl", "enable", "--now", "update-github-runner.timer"), systemctl_calls)
        self.assertIn(("systemctl", "enable", "--now", "gaia-cleanup.timer"), systemctl_calls)
        self.assertIn(("systemctl", "enable", "--now", "gaia-container-prune.timer"), systemctl_calls)

        git_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] in ("git", "sudo")]
        self.assertIn(("git", "config", "--global", "status.submoduleSummary", "false"), git_calls)
        self.assertIn(("sudo", "-u", "tester", "git", "config", "--global", "submodule.recurse", "false"), git_calls)

    def test_gaia_omits_agy_symlink_when_missing_and_ignores_missing_hermes_sockets(self) -> None:
        ctx = self._gaia_ctx(sudo_user="root")
        runner = FakeCommandRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            provision_host("gaia", ctx, runner, self.fetcher)

        self.assertFalse((self.root_dir / "usr" / "local" / "bin" / "agy").exists())
        self.assertFalse(any(cmd[0] == "sudo" for cmd, _, _ in runner.calls))

    def test_muspelheim_provisioning_enforces_all_invariants(self) -> None:
        ctx = ProvisionContext(
            repo_root=REPO_ROOT,
            root_dir=self.root_dir,
            raw_base="https://example.invalid/yggdrasil-os",
            euid=0,
        )
        runner = FakeCommandRunner()

        deprecated = self.root_dir / "usr" / "local" / "bin" / "notify-discord.sh"
        deprecated.parent.mkdir(parents=True)
        deprecated.write_text("old", encoding="utf-8")

        provision_host("muspelheim", ctx, runner, self.fetcher)

        self.assertFalse(deprecated.exists())
        self.assertTrue((self.root_dir / "opt" / "immich" / "postgres").is_dir())
        self.assertTrue((self.root_dir / "mnt" / "storage" / "backups").is_dir())
        self.assertTrue((self.root_dir / "mnt" / "storage" / "downloads" / "slskd" / ".incomplete").is_dir())

        romm_db = self.root_dir / "opt" / "romm" / "db"
        self.assertTrue(romm_db.is_dir())
        self.assertEqual(stat.S_IMODE(romm_db.stat().st_mode), 0o750)

        chown_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] == "chown"]
        self.assertIn(("chown", "-R", "999:999", str(romm_db)), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "romm")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "opt" / "immich" / "postgres")), chown_calls)
        self.assertNotIn(("chown", "-R", "1000:1000", str(self.root_dir / "mnt" / "storage" / "backups")), chown_calls)

        for rel in (
            "usr/local/bin/apollo-supply-restore.sh",
            "usr/local/bin/muspelheim-restart-docker.sh",
            "usr/local/bin/charon-restore",
            "usr/local/bin/swarm-exec",
            "usr/local/bin/pin-registry-host",
            "usr/local/bin/update-github-runner.sh",
        ):
            target = self.root_dir / rel
            self.assertTrue(target.is_file(), f"Missing {rel}")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

        self.assertEqual(
            stat.S_IMODE((self.root_dir / "etc" / "systemd" / "system" / "apollo-supply.service").stat().st_mode),
            0o644,
        )

        pin_calls = [
            (cmd, env)
            for cmd, env, _ in runner.calls
            if cmd == (str(self.root_dir / "usr" / "local" / "bin" / "pin-registry-host"),)
        ]
        self.assertEqual(pin_calls, [((str(self.root_dir / "usr" / "local" / "bin" / "pin-registry-host"),), {"NODE_ROLE": "worker"})])

        systemctl_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] == "systemctl"]
        self.assertIn(("systemctl", "enable", "apollo-supply.service"), systemctl_calls)
        self.assertNotIn(("systemctl", "enable", "--now", "apollo-supply.service"), systemctl_calls)
        self.assertIn(("systemctl", "enable", "--now", "update-github-runner.timer"), systemctl_calls)
        self.assertFalse(any(cmd[0] == "git" for cmd, _, _ in runner.calls))

    def test_standalone_installers_enforce_root_check_and_install_expected_files(self) -> None:
        non_root = ProvisionContext(repo_root=REPO_ROOT, root_dir=self.root_dir, euid=1000)
        runner = FakeCommandRunner()
        with self.assertRaises(PermissionError):
            install_cleanup_timer(non_root, runner, self.fetcher)
        with self.assertRaises(PermissionError):
            install_startup_service(non_root, runner, self.fetcher)
        with self.assertRaises(PermissionError):
            fix_docker_dns_boot(non_root, runner)
        self.assertEqual(runner.calls, [])

        root_ctx = ProvisionContext(
            repo_root=REPO_ROOT,
            root_dir=self.root_dir,
            cleanup_bin_path=self.tmpdir / "custom" / "cleanup.sh",
            cleanup_systemd_dir=self.tmpdir / "custom_systemd",
            euid=0,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            install_cleanup_timer(root_ctx, runner, self.fetcher)
        self.assertEqual(stat.S_IMODE(root_ctx.effective_cleanup_bin_path.stat().st_mode), 0o755)
        for unit in (
            "gaia-cleanup.service",
            "gaia-cleanup.timer",
            "gaia-container-prune.service",
            "gaia-container-prune.timer",
        ):
            self.assertEqual(stat.S_IMODE((root_ctx.effective_cleanup_systemd_dir / unit).stat().st_mode), 0o644)

        runner.calls.clear()
        install_startup_service(root_ctx, runner, self.fetcher)
        dropin = self.root_dir / "etc" / "systemd" / "system" / "docker.service.d" / "wait-for-network.conf"
        self.assertEqual(dropin.read_text(encoding="utf-8"), WAIT_FOR_NETWORK_CONF)
        self.assertEqual(stat.S_IMODE(dropin.stat().st_mode), 0o644)
        self.assertEqual(
            stat.S_IMODE((self.root_dir / "usr" / "local" / "bin" / "gaia-swarm-startup.sh").stat().st_mode),
            0o755,
        )
        self.assertEqual(
            stat.S_IMODE((self.root_dir / "usr" / "local" / "bin" / "gaia-restart-docker.sh").stat().st_mode),
            0o755,
        )
        systemctl_calls = [cmd for cmd, _, _ in runner.calls if cmd[0] == "systemctl"]
        self.assertIn(("systemctl", "enable", "gaia-swarm-startup.service"), systemctl_calls)
        self.assertNotIn(("systemctl", "enable", "--now", "gaia-swarm-startup.service"), systemctl_calls)
        self.assertIn(("docker", "swarm", "update", "--task-history-limit", "1"), [c for c, _, _ in runner.calls])

        dropin.unlink()
        runner_no_swarm = FakeCommandRunner(docker_info_rc=1)
        install_startup_service(root_ctx, runner_no_swarm, self.fetcher)
        self.assertNotIn(("docker", "swarm", "update", "--task-history-limit", "1"), [c for c, _, _ in runner_no_swarm.calls])

        dropin.unlink()
        runner.calls.clear()
        fix_docker_dns_boot(root_ctx, runner)
        self.assertEqual(dropin.read_text(encoding="utf-8"), WAIT_FOR_NETWORK_CONF)
        self.assertEqual([cmd for cmd, _, _ in runner.calls], [("systemctl", "daemon-reload")])

    def test_asset_fetcher_local_first_remote_http_and_fail_loud(self) -> None:
        self.assertEqual(
            self.fetcher.fetch_bytes("maintenance/cleanup_storage.sh"),
            (REPO_ROOT / "maintenance" / "cleanup_storage.sh").read_bytes(),
        )

        serve_dir = self.tmpdir / "http_root"
        (serve_dir / "hosts").mkdir(parents=True)
        (serve_dir / "hosts" / "hello.txt").write_bytes(b"remote-ok\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietHandler, directory=str(serve_dir)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            remote_fetcher = LocalOrRemoteAssetFetcher(
                repo_root=None,
                raw_base=f"http://127.0.0.1:{server.server_address[1]}",
            )
            self.assertEqual(remote_fetcher.fetch_bytes("hosts/hello.txt"), b"remote-ok\n")
            with self.assertRaises(AssetFetchError):
                remote_fetcher.fetch_bytes("hosts/missing.txt")
            with self.assertRaises(AssetFetchError):
                remote_fetcher.fetch_bytes("../etc/passwd")
        finally:
            server.shutdown()
            server.server_close()

    def test_manifests_parse_cleanly(self) -> None:
        self.assertEqual(load_manifest("gaia").host, "gaia")
        self.assertEqual(load_manifest("muspelheim").host, "muspelheim")


class TestZeroCloneShims(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-yggdrasil-shims-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.calls_log = self.tmpdir / "calls.log"
        self.archive_tmp_parent = self.tmpdir / "shim_tmp"
        self.archive_tmp_parent.mkdir()

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
            "exit 0\n",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_shims_are_executable_and_valid_bash(self) -> None:
        for script in SHIM_SCRIPTS:
            with self.subTest(script=script.name):
                mode = stat.S_IMODE(script.stat().st_mode)
                self.assertEqual(mode & 0o111, 0o111)
                self.assertEqual(mode & 0o002, 0)
                res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_shim_in_tree_and_root_override_and_tarball_cleanup(self) -> None:
        # 1. Non-root check in standalone shim fails loudly
        res_non_root = subprocess.run(
            ["bash", str(REPO_ROOT / "maintenance" / "install_cleanup_timer.sh")],
            env={"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin", "MOCK_UID": "1000"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_non_root.returncode, 0)
        self.assertIn("must be run as root", res_non_root.stderr)

        # 2. Real CLI fails loudly as non-root and exposes expected subcommands
        res_cli_help = subprocess.run(
            [sys.executable, "-m", "yggdrasil_os", "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_cli_help.returncode, 0, res_cli_help.stderr)
        for subcmd in ("provision", "install-cleanup-timer", "install-startup-service", "fix-docker-dns-boot"):
            self.assertIn(subcmd, res_cli_help.stdout)

        res_cli_non_root = subprocess.run(
            [sys.executable, "-m", "yggdrasil_os", "fix-docker-dns-boot"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_cli_non_root.returncode, 0)
        self.assertIn("must be run as root", res_cli_non_root.stderr)

        # Install mock python3 in mock_bin to inspect shim resolution & TMP_DIR lifecycle
        py_log = self.tmpdir / "python_calls.jsonl"
        _write_executable(
            self.mock_bin / "python3",
            f"#!{sys.executable}\n"
            "import importlib, json, os, sys\n"
            f"log_path = {str(py_log)!r}\n"
            "mod = importlib.import_module('yggdrasil_os')\n"
            "entry = {'argv': sys.argv[1:], 'module_file': mod.__file__, 'pythonpath': os.environ.get('PYTHONPATH', '')}\n"
            "with open(log_path, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(entry) + '\\n')\n"
            "sys.exit(int(os.environ.get('MOCK_PY_EXIT', '0')))\n",
        )

        # 3. In-tree resolution across all 5 shims
        expected_cmds = (
            ("hosts/setup_gaia.sh", ["provision", "gaia"]),
            ("hosts/setup_muspelheim.sh", ["provision", "muspelheim"]),
            ("maintenance/install_cleanup_timer.sh", ["install-cleanup-timer"]),
            ("swarm/install_startup_service.sh", ["install-startup-service"]),
            ("network/fix_docker_dns_boot.sh", ["fix-docker-dns-boot"]),
        )
        for rel_script, expected_args in expected_cmds:
            py_log.unlink(missing_ok=True)
            res = subprocess.run(
                ["bash", str(REPO_ROOT / rel_script)],
                env={"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin", "MOCK_UID": "0"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
            entry = json.loads(py_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", *expected_args])
            self.assertEqual(Path(entry["module_file"]).resolve(), (REPO_ROOT / "yggdrasil_os" / "__init__.py").resolve())

        # 4. Piped stdin with YGGDRASIL_OS_ROOT (invalid fails loud, valid succeeds)
        bad_root = self.tmpdir / "bad_root"
        bad_root.mkdir()
        res_bad = subprocess.run(
            ["bash"],
            input=(REPO_ROOT / "maintenance" / "install_cleanup_timer.sh").read_text(encoding="utf-8"),
            env={
                "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                "MOCK_UID": "0",
                "YGGDRASIL_OS_ROOT": str(bad_root),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_bad.returncode, 0)
        self.assertIn("does not contain yggdrasil_os/__init__.py", res_bad.stderr)

        py_log.unlink(missing_ok=True)
        res_root_override = subprocess.run(
            ["bash"],
            input=(REPO_ROOT / "maintenance" / "install_cleanup_timer.sh").read_text(encoding="utf-8"),
            env={
                "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                "MOCK_UID": "0",
                "YGGDRASIL_OS_ROOT": str(REPO_ROOT),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_root_override.returncode, 0, res_root_override.stderr + res_root_override.stdout)
        entry = json.loads(py_log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", "install-cleanup-timer"])

        # 5. Piped stdin with archive tarball via file:// URL + TMP_DIR cleanup on success and failure
        archive_path = self.tmpdir / "yggdrasil-os-main.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(REPO_ROOT / "yggdrasil_os", arcname="yggdrasil-os-main/yggdrasil_os")

        for mock_exit in ("0", "42"):
            py_log.unlink(missing_ok=True)
            res_tar = subprocess.run(
                ["bash"],
                input=(REPO_ROOT / "hosts" / "setup_gaia.sh").read_text(encoding="utf-8"),
                env={
                    "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                    "TMPDIR": str(self.archive_tmp_parent),
                    "YGGDRASIL_OS_ARCHIVE_URL": archive_path.as_uri(),
                    "MOCK_PY_EXIT": mock_exit,
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_tar.returncode, int(mock_exit), res_tar.stderr + res_tar.stdout)
            entry = json.loads(py_log.read_text(encoding="utf-8").splitlines()[-1])
            self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", "provision", "gaia"])
            self.assertTrue(entry["pythonpath"].startswith(str(self.archive_tmp_parent)))
            self.assertEqual(list(self.archive_tmp_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

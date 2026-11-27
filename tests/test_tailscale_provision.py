#!/usr/bin/env python3
"""Unit tests for host-native Tailscale provisioning and helper scripts."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from yggdrasil_os import (  # noqa: E402
    CommandResult,
    LocalOrRemoteAssetFetcher,
    ProvisionContext,
    load_manifest,
    provision_host,
)

INSTALL_SCRIPT = REPO_ROOT / "hosts" / "install_tailscale.sh"
UDP_GRO_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "tailscale-udp-gro.sh"
SYSCTL_CONF = REPO_ROOT / "hosts" / "sysctl" / "99-tailscale.conf"
UDP_GRO_SERVICE = REPO_ROOT / "hosts" / "systemd" / "tailscale-udp-gro.service"


class _RecordingRunner:
    """In-memory CommandRunner capturing all executed commands."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        del env, check, input_text
        self.calls.append(tuple(argv))
        return CommandResult(0, "", "")

    def has_command(self, name: str) -> bool:
        return name == "systemctl"


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _write_mock_id(bin_dir: Path) -> None:
    _write_exec(
        bin_dir / "id",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-u" ]; then\n'
        '  printf \'%s\\n\' "${MOCK_UID:-0}"\n'
        "  exit 0\n"
        "fi\n"
        'exec /usr/bin/id "$@"\n',
    )


def _isolated_tool_path(bin_dir: Path) -> str:
    """Create an isolated PATH containing bin_dir plus bash/env/awk."""
    core_dir = bin_dir.parent / f"{bin_dir.name}_core"
    core_dir.mkdir(exist_ok=True)
    for tool in ("bash", "env", "awk"):
        found = shutil.which(tool)
        if found and not (core_dir / tool).exists():
            os.symlink(found, core_dir / tool)
    return f"{bin_dir}:{core_dir}"


class TestTailscaleManifestAndProvision(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-tailscale-prov-"))
        self.root_dir = self.tmpdir / "root"
        self.root_dir.mkdir()
        self.fetcher = LocalOrRemoteAssetFetcher(
            repo_root=REPO_ROOT,
            raw_base="https://example.invalid/yggdrasil-os",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_manifests_declare_tailscale_assets_sysctl_and_units(self) -> None:
        expected_assets = {
            ("hosts/sysctl/99-tailscale.conf", "/etc/sysctl.d/99-tailscale.conf", 0o644),
            ("hosts/scripts/tailscale-udp-gro.sh", "/usr/local/bin/tailscale-udp-gro.sh", 0o755),
            ("hosts/systemd/tailscale-udp-gro.service", "/etc/systemd/system/tailscale-udp-gro.service", 0o644),
        }
        expected_pre = {
            "gaia": ("hosts/install_prerequisites.sh", "hosts/install_tailscale.sh"),
            "muspelheim": ("hosts/install_tailscale.sh",),
        }
        for host, pre_scripts in expected_pre.items():
            manifest = load_manifest(host)
            self.assertEqual(manifest.pre_scripts, pre_scripts)
            assets = {(a.src, a.dest, a.mode) for a in manifest.assets}
            self.assertTrue(expected_assets.issubset(assets), f"Missing Tailscale assets in {host}")
            post_argvs = [cmd.argv for cmd in manifest.post_commands]
            self.assertIn(("sysctl", "-p", "/etc/sysctl.d/99-tailscale.conf"), post_argvs)
            enable_now = list(manifest.systemd.enable_now)
            gro_idx = enable_now.index("tailscale-udp-gro.service")
            ts_idx = enable_now.index("tailscaled.service")
            self.assertEqual(ts_idx, gro_idx + 1)

    def test_provision_host_installs_tailscale_files_and_enables_units(self) -> None:
        for host in ("gaia", "muspelheim"):
            root = self.tmpdir / f"root-{host}"
            root.mkdir()
            ctx = ProvisionContext(repo_root=REPO_ROOT, root_dir=root, euid=0, sudo_user="root")
            runner = _RecordingRunner()
            with contextlib.redirect_stdout(io.StringIO()):
                provision_host(host, ctx, runner, self.fetcher)

            sysctl_dest = root / "etc" / "sysctl.d" / "99-tailscale.conf"
            gro_dest = root / "usr" / "local" / "bin" / "tailscale-udp-gro.sh"
            unit_dest = root / "etc" / "systemd" / "system" / "tailscale-udp-gro.service"
            self.assertEqual(stat.S_IMODE(sysctl_dest.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(gro_dest.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(unit_dest.stat().st_mode), 0o644)
            self.assertIn("net.ipv4.ip_forward = 1", sysctl_dest.read_text(encoding="utf-8"))
            self.assertIn("net.ipv6.conf.all.forwarding = 1", sysctl_dest.read_text(encoding="utf-8"))
            self.assertIn("Before=tailscaled.service", unit_dest.read_text(encoding="utf-8"))

            self.assertIn(("bash", str(INSTALL_SCRIPT)), runner.calls)
            self.assertIn(("sysctl", "-p", "/etc/sysctl.d/99-tailscale.conf"), runner.calls)
            gro_call = ("systemctl", "enable", "--now", "tailscale-udp-gro.service")
            ts_call = ("systemctl", "enable", "--now", "tailscaled.service")
            self.assertLess(runner.calls.index(gro_call), runner.calls.index(ts_call))


class TestTailscaleScripts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-tailscale-scripts-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        _write_mock_id(self.mock_bin)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scripts_are_executable_and_pass_bash_syntax(self) -> None:
        for script in (INSTALL_SCRIPT, UDP_GRO_SCRIPT):
            self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)
            res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(res.returncode, 0, res.stderr)

    def test_install_tailscale_fails_loudly_when_non_root(self) -> None:
        res = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            env={"PATH": _isolated_tool_path(self.mock_bin), "MOCK_UID": "1000"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Error: install_tailscale.sh must be run as root (sudo).", res.stderr)

    def test_install_tailscale_is_idempotent_when_binaries_exist(self) -> None:
        iso_bin = self.tmpdir / "iso_bin"
        _write_mock_id(iso_bin)
        _write_exec(iso_bin / "tailscale", "#!/usr/bin/env bash\nexit 0\n")
        _write_exec(iso_bin / "ethtool", "#!/usr/bin/env bash\nexit 0\n")
        res = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            env={"PATH": _isolated_tool_path(iso_bin), "MOCK_UID": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("already installed", res.stdout)

    def test_install_tailscale_fails_loudly_on_missing_or_incomplete_os_release(self) -> None:
        iso_bin = self.tmpdir / "empty_tools"
        _write_mock_id(iso_bin)
        env_path = _isolated_tool_path(iso_bin)

        missing_os = self.tmpdir / "missing-os-release"
        res_missing = subprocess.run(
            ["bash", str(INSTALL_SCRIPT)],
            env={"PATH": env_path, "MOCK_UID": "0", "OS_RELEASE_PATH": str(missing_os)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_missing.returncode, 0)
        self.assertIn("Cannot read OS release file", res_missing.stderr)

        for label, body in (
            ("missing-codename", "ID=ubuntu\n"),
            ("missing-id", "VERSION_CODENAME=noble\n"),
        ):
            os_file = self.tmpdir / f"os-release-{label}"
            os_file.write_text(body, encoding="utf-8")
            res = subprocess.run(
                ["bash", str(INSTALL_SCRIPT)],
                env={"PATH": env_path, "MOCK_UID": "0", "OS_RELEASE_PATH": str(os_file)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("Missing ID or VERSION_CODENAME", res.stderr)

    def test_udp_gro_detects_default_interface_dynamically(self) -> None:
        gro_bin = self.tmpdir / "gro_bin"
        gro_bin.mkdir()
        ethtool_log = self.tmpdir / "ethtool.log"
        _write_exec(
            gro_bin / "ip",
            "#!/usr/bin/env bash\n"
            'printf "default via 10.0.0.1 dev wlp3s0 proto dhcp metric 100\\n"\n',
        )
        _write_exec(
            gro_bin / "ethtool",
            f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > {str(ethtool_log)!r}\n",
        )
        res = subprocess.run(
            ["bash", str(UDP_GRO_SCRIPT)],
            env={"PATH": _isolated_tool_path(gro_bin)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(
            ethtool_log.read_text(encoding="utf-8").strip(),
            "-K wlp3s0 rx-udp-gro-forwarding on rx-gro-list off",
        )

    def test_udp_gro_fails_loudly_without_default_route_or_ethtool(self) -> None:
        no_route_bin = self.tmpdir / "no_route"
        _write_exec(no_route_bin / "ip", "#!/usr/bin/env bash\nexit 0\n")
        _write_exec(no_route_bin / "ethtool", "#!/usr/bin/env bash\nexit 0\n")
        res_route = subprocess.run(
            ["bash", str(UDP_GRO_SCRIPT)],
            env={"PATH": _isolated_tool_path(no_route_bin)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_route.returncode, 0)
        self.assertIn("Could not determine default network interface", res_route.stderr)

        no_ethtool_bin = self.tmpdir / "no_ethtool"
        _write_exec(
            no_ethtool_bin / "ip",
            "#!/usr/bin/env bash\nprintf 'default via 10.0.0.1 dev wlp3s0\\n'\n",
        )
        res_ethtool = subprocess.run(
            ["bash", str(UDP_GRO_SCRIPT)],
            env={"PATH": _isolated_tool_path(no_ethtool_bin)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(res_ethtool.returncode, 0)
        self.assertIn("ethtool command not found", res_ethtool.stderr)

    def test_tailscale_files_contain_no_auth_keys_or_hardcoded_nics(self) -> None:
        targets = (
            INSTALL_SCRIPT,
            UDP_GRO_SCRIPT,
            SYSCTL_CONF,
            UDP_GRO_SERVICE,
            REPO_ROOT / "yggdrasil_os" / "manifests" / "gaia.json",
            REPO_ROOT / "yggdrasil_os" / "manifests" / "muspelheim.json",
        )
        forbidden_tokens = ("tskey-", "enp2s0", "eth0", "\u2013", "\u2014")
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_tokens:
                self.assertNotIn(token, text, f"Forbidden token {token!r} found in {path}")
        self.assertNotIn("tailscale up", INSTALL_SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

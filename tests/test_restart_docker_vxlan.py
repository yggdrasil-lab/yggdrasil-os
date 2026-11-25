#!/usr/bin/env python3
"""Direct seam and zero-clone shim tests for Docker restart and VXLAN cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from yggdrasil_os import (  # noqa: E402
    APOLLO_RESTORE_REL_PATH,
    AssetFetchError,
    CommandExecutionError,
    CommandResult,
    IpNetlinkPort,
    RestartContext,
    SubprocessSystemdPort,
    VxlanLink,
    restart_docker,
    sweep_stale_vxlan_links,
)
from yggdrasil_os.recovery_ports import parse_vxlan_links  # noqa: E402

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


class FakeClock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCommandRunner:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        available_commands: set[str] | None = None,
        handler: Callable[[tuple[str, ...]], CommandResult] | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.available_commands = available_commands if available_commands is not None else {"ip"}
        self._handler = handler

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        cmd = tuple(argv)
        self.calls.append(" ".join(cmd))
        res = self._handler(cmd) if self._handler else CommandResult(0, "", "")
        if check and res.returncode != 0:
            raise CommandExecutionError(cmd, res.returncode, res.stdout, res.stderr)
        return res

    def has_command(self, name: str) -> bool:
        return name in self.available_commands


class FakeDockerPort:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        handler: Callable[[tuple[str, ...]], CommandResult] | None = None,
    ) -> None:
        self.calls = calls if calls is not None else []
        self._handler = handler

    def run(
        self,
        args: Sequence[str],
        *,
        stdin_text: str | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        check: bool = False,
    ) -> CommandResult:
        cmd = tuple(args)
        self.calls.append("docker " + " ".join(cmd))
        res = self._handler(cmd) if self._handler else CommandResult(0, "", "")
        if check and res.returncode != 0:
            raise CommandExecutionError(("docker", *cmd), res.returncode, res.stdout, res.stderr)
        return res


class FakeSystemdPort:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        fail_stop_socket: bool = False,
        fail_stop_docker: bool = False,
        fail_start_docker: bool = False,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.fail_stop_socket = fail_stop_socket
        self.fail_stop_docker = fail_stop_docker
        self.fail_start_docker = fail_start_docker

    def stop_units(self, units: Sequence[str]) -> CommandResult:
        self.calls.append("systemctl stop " + " ".join(units))
        if tuple(units) == ("docker.socket", "docker.service") and self.fail_stop_socket:
            return CommandResult(1, "", "socket stop failed")
        if tuple(units) == ("docker",) and self.fail_stop_docker:
            return CommandResult(1, "", "docker stop failed")
        return CommandResult(0, "", "")

    def start_unit(self, unit: str) -> CommandResult:
        self.calls.append(f"systemctl start {unit}")
        if self.fail_start_docker:
            return CommandResult(1, "", "start failed")
        return CommandResult(0, "", "")

    def vacuum_journal(self, max_size: str) -> CommandResult:
        self.calls.append(f"journalctl --vacuum-size={max_size}")
        return CommandResult(0, "", "")


class FakeNetlinkPort:
    def __init__(
        self,
        *,
        calls: list[str] | None = None,
        available: bool = True,
        links: tuple[VxlanLink, ...] = (),
        fail_delete: bool = False,
    ) -> None:
        self.calls = calls if calls is not None else []
        self.available = available
        self.links = links
        self.fail_delete = fail_delete

    def is_available(self) -> bool:
        return self.available

    def list_vxlan_links(self) -> tuple[VxlanLink, ...]:
        self.calls.append("ip -o link show type vxlan")
        return self.links

    def delete_link(self, name: str) -> CommandResult:
        self.calls.append(f"ip link delete {name}")
        if self.fail_delete:
            return CommandResult(1, "", "delete error")
        return CommandResult(0, "", "")


class FakeAssetFetcher:
    def __init__(
        self,
        *,
        local_files: dict[str, Path] | None = None,
        remote_bytes: dict[str, bytes] | None = None,
    ) -> None:
        self.local_files = local_files or {}
        self.remote_bytes = remote_bytes or {}

    def local_path(self, rel_path: str) -> Path | None:
        return self.local_files.get(rel_path)

    def fetch_bytes(self, rel_path: str) -> bytes:
        if rel_path in self.remote_bytes:
            return self.remote_bytes[rel_path]
        raise AssetFetchError(f"Missing asset {rel_path}")


class TestRestartDockerSeam(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-restart-seam-"))
        self.calls: list[str] = []
        self.sample_links = parse_vxlan_links(SAMPLE_VXLAN_LINKS)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_parse_and_sweep_stale_vxlan_links_filters_and_strips_peer(self) -> None:
        self.assertEqual(
            self.sample_links,
            (
                VxlanLink("vx-001036-y2zly", "DOWN"),
                VxlanLink("vx-001037-abcde", "DOWN"),
                VxlanLink("vx-001001-live1", "UP"),
                VxlanLink("vx-001002-live2", "UNKNOWN"),
                VxlanLink("vxlan0", "DOWN"),
                VxlanLink("flannel.1", "DOWN"),
            ),
        )
        netlink = FakeNetlinkPort(calls=self.calls, links=self.sample_links)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            removed = sweep_stale_vxlan_links(netlink)
        self.assertEqual(removed, ("vx-001036-y2zly", "vx-001037-abcde"))
        self.assertEqual(
            self.calls,
            [
                "ip -o link show type vxlan",
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
            ],
        )
        self.assertIn("vx-001036-y2zly", buf.getvalue())
        self.assertIn("vx-001037-abcde", buf.getvalue())

    def test_gaia_restart_with_override_and_in_process_startup(self) -> None:
        runner = FakeCommandRunner(calls=self.calls)
        docker = FakeDockerPort(calls=self.calls)
        systemd = FakeSystemdPort(calls=self.calls)
        netlink = FakeNetlinkPort(calls=self.calls, links=self.sample_links)
        fetcher = FakeAssetFetcher()
        clock = FakeClock()

        ctx_override = RestartContext(euid=0, swarm_startup_script="/custom/startup.sh")
        with contextlib.redirect_stdout(io.StringIO()):
            res_override = restart_docker(
                "gaia",
                ctx_override,
                docker=docker,
                systemd=systemd,
                netlink=netlink,
                runner=runner,
                fetcher=fetcher,
                clock=clock,
            )
        self.assertEqual(res_override.removed_vxlan_links, ("vx-001036-y2zly", "vx-001037-abcde"))
        self.assertIsNone(res_override.startup_result)
        self.assertEqual(
            self.calls,
            [
                "sync",
                "systemctl stop docker.socket docker.service",
                "ip -o link show type vxlan",
                "ip link delete vx-001036-y2zly",
                "ip link delete vx-001037-abcde",
                "systemctl start docker",
                "/custom/startup.sh",
            ],
        )

        self.calls.clear()

        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(1, "", "")
            if cmd[:2] == ("node", "ls"):
                return CommandResult(0, "node1 * gaia Ready Active Leader\n", "")
            return CommandResult(0, "", "")

        docker_inproc = FakeDockerPort(calls=self.calls, handler=docker_handler)
        ctx_inproc = RestartContext(euid=0, swarm_startup_script=None)
        with contextlib.redirect_stdout(io.StringIO()):
            res_inproc = restart_docker(
                "gaia",
                ctx_inproc,
                docker=docker_inproc,
                systemd=systemd,
                netlink=netlink,
                runner=runner,
                fetcher=fetcher,
                clock=clock,
            )
        self.assertIsNotNone(res_inproc.startup_result)
        self.assertTrue(res_inproc.startup_result.converged)
        self.assertIn("docker info", self.calls)
        self.assertIn("docker node ls", self.calls)
        self.assertNotIn("/custom/startup.sh", self.calls)

    def test_muspelheim_restart_stops_apollo_waits_and_prunes(self) -> None:
        restore_script = self.tmpdir / "apollo-restore.sh"
        restore_script.write_text("#!/bin/sh\n", encoding="utf-8")
        restore_script.chmod(0o755)

        runner = FakeCommandRunner(calls=self.calls)
        docker = FakeDockerPort(calls=self.calls)
        systemd = FakeSystemdPort(calls=self.calls)
        netlink = FakeNetlinkPort(calls=self.calls, links=self.sample_links)
        fetcher = FakeAssetFetcher()
        clock = FakeClock()

        ctx = RestartContext(euid=0, apollo_restore_script=str(restore_script))
        with contextlib.redirect_stdout(io.StringIO()):
            res = restart_docker(
                "muspelheim",
                ctx,
                docker=docker,
                systemd=systemd,
                netlink=netlink,
                runner=runner,
                fetcher=fetcher,
                clock=clock,
            )
        self.assertEqual(res.role, "muspelheim")
        self.assertEqual(
            self.calls,
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
                str(restore_script),
                "docker container prune -f",
            ],
        )

    def test_muspelheim_skips_stop_when_down_retries_readiness_and_fails_on_timeout(self) -> None:
        restore_script = self.tmpdir / "apollo-restore.sh"
        restore_script.write_text("#!/bin/sh\n", encoding="utf-8")
        restore_script.chmod(0o755)

        info_results = [
            CommandResult(1, "", "down"),  # pre-restart check: down -> skip docker stop
            CommandResult(1, "", "booting"),
            CommandResult(1, "", "booting"),
            CommandResult(0, "", ""),  # online on 3rd readiness poll
        ]

        def docker_handler(cmd: tuple[str, ...]) -> CommandResult:
            if cmd == ("info",):
                return info_results.pop(0) if info_results else CommandResult(0, "", "")
            return CommandResult(0, "", "")

        runner = FakeCommandRunner(calls=self.calls)
        docker = FakeDockerPort(calls=self.calls, handler=docker_handler)
        systemd = FakeSystemdPort(calls=self.calls)
        netlink = FakeNetlinkPort(calls=self.calls)
        clock = FakeClock()

        with contextlib.redirect_stdout(io.StringIO()):
            restart_docker(
                "muspelheim",
                RestartContext(euid=0, apollo_restore_script=str(restore_script)),
                docker=docker,
                systemd=systemd,
                netlink=netlink,
                runner=runner,
                fetcher=FakeAssetFetcher(),
                clock=clock,
            )
        self.assertNotIn("docker stop slskd sabnzbd qbittorrent gluetun", self.calls)
        self.assertEqual(clock.sleeps, [2, 2])

        docker_always_down = FakeDockerPort(
            calls=[],
            handler=lambda _cmd: CommandResult(1, "", "unreachable"),
        )
        timeout_clock = FakeClock()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "30s"):
                restart_docker(
                    "muspelheim",
                    RestartContext(euid=0, apollo_restore_script=str(restore_script)),
                    docker=docker_always_down,
                    systemd=FakeSystemdPort(),
                    netlink=FakeNetlinkPort(),
                    runner=FakeCommandRunner(),
                    fetcher=FakeAssetFetcher(),
                    clock=timeout_clock,
                )
        self.assertEqual(sum(timeout_clock.sleeps), 30)

    def test_muspelheim_apollo_restore_in_tree_and_remote_temp_cleanup(self) -> None:
        in_tree_restore = self.tmpdir / "in_tree_apollo.sh"
        in_tree_restore.write_text("#!/bin/sh\n", encoding="utf-8")

        runner = FakeCommandRunner(calls=self.calls)
        with contextlib.redirect_stdout(io.StringIO()):
            restart_docker(
                "muspelheim",
                RestartContext(euid=0, apollo_restore_script="/nonexistent/apollo.sh"),
                docker=FakeDockerPort(),
                systemd=FakeSystemdPort(),
                netlink=FakeNetlinkPort(),
                runner=runner,
                fetcher=FakeAssetFetcher(local_files={APOLLO_RESTORE_REL_PATH: in_tree_restore}),
                clock=FakeClock(),
            )
        self.assertIn(f"bash {in_tree_restore}", self.calls)

        executed_tmp_paths: list[Path] = []

        def capture_runner(cmd: tuple[str, ...]) -> CommandResult:
            if cmd[0] == "bash":
                p = Path(cmd[1])
                self.assertTrue(p.is_file())
                self.assertEqual(p.read_bytes(), b"#!/bin/sh\necho remote-apollo\n")
                executed_tmp_paths.append(p)
            return CommandResult(0, "", "")

        with contextlib.redirect_stdout(io.StringIO()):
            restart_docker(
                "muspelheim",
                RestartContext(euid=0, apollo_restore_script="/nonexistent/apollo.sh"),
                docker=FakeDockerPort(),
                systemd=FakeSystemdPort(),
                netlink=FakeNetlinkPort(),
                runner=FakeCommandRunner(handler=capture_runner),
                fetcher=FakeAssetFetcher(
                    remote_bytes={APOLLO_RESTORE_REL_PATH: b"#!/bin/sh\necho remote-apollo\n"}
                ),
                clock=FakeClock(),
            )
        self.assertEqual(len(executed_tmp_paths), 1)
        self.assertFalse(executed_tmp_paths[0].exists())

    def test_systemctl_fallback_missing_ip_delete_failure_and_non_root_guard(self) -> None:
        non_root_calls: list[str] = []
        with self.assertRaisesRegex(PermissionError, "must be run as root"):
            restart_docker(
                "gaia",
                RestartContext(euid=1000, swarm_startup_script="/custom/startup.sh"),
                docker=FakeDockerPort(calls=non_root_calls),
                systemd=FakeSystemdPort(calls=non_root_calls),
                netlink=FakeNetlinkPort(calls=non_root_calls),
                runner=FakeCommandRunner(calls=non_root_calls),
                fetcher=FakeAssetFetcher(),
                clock=FakeClock(),
            )
        self.assertEqual(non_root_calls, [])

        systemd = FakeSystemdPort(calls=self.calls, fail_stop_socket=True)
        netlink_fail_del = FakeNetlinkPort(
            calls=self.calls,
            links=self.sample_links,
            fail_delete=True,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            res = restart_docker(
                "gaia",
                RestartContext(euid=0, swarm_startup_script="/custom/startup.sh"),
                docker=FakeDockerPort(calls=self.calls),
                systemd=systemd,
                netlink=netlink_fail_del,
                runner=FakeCommandRunner(calls=self.calls),
                fetcher=FakeAssetFetcher(),
                clock=FakeClock(),
            )
        self.assertTrue(res.used_systemd_fallback)
        self.assertEqual(res.removed_vxlan_links, ())
        stop_sock_idx = self.calls.index("systemctl stop docker.socket docker.service")
        stop_doc_idx = self.calls.index("systemctl stop docker")
        start_idx = self.calls.index("systemctl start docker")
        self.assertLess(stop_sock_idx, stop_doc_idx)
        self.assertLess(stop_doc_idx, start_idx)

        no_ip_calls: list[str] = []
        with contextlib.redirect_stdout(io.StringIO()):
            restart_docker(
                "gaia",
                RestartContext(euid=0, swarm_startup_script="/custom/startup.sh"),
                docker=FakeDockerPort(calls=no_ip_calls),
                systemd=FakeSystemdPort(calls=no_ip_calls),
                netlink=FakeNetlinkPort(calls=no_ip_calls, available=False),
                runner=FakeCommandRunner(calls=no_ip_calls),
                fetcher=FakeAssetFetcher(),
                clock=FakeClock(),
            )
        self.assertFalse(any(c.startswith("ip ") for c in no_ip_calls))

    def test_adapters_ip_netlink_and_subprocess_systemd_ports(self) -> None:
        runner = FakeCommandRunner(
            calls=self.calls,
            available_commands={"ip"},
            handler=lambda cmd: CommandResult(0, SAMPLE_VXLAN_LINKS + "\n", "")
            if cmd[:2] == ("ip", "-o")
            else CommandResult(0, "", ""),
        )
        netlink = IpNetlinkPort(runner)
        self.assertTrue(netlink.is_available())
        links = netlink.list_vxlan_links()
        self.assertEqual(links[1], VxlanLink("vx-001037-abcde", "DOWN"))
        netlink.delete_link("vx-001036-y2zly")
        systemd = SubprocessSystemdPort(runner)
        systemd.stop_units(("docker.socket", "docker.service"))
        systemd.start_unit("docker")
        systemd.vacuum_journal("500M")
        self.assertEqual(
            self.calls,
            [
                "ip -o link show type vxlan",
                "ip link delete vx-001036-y2zly",
                "systemctl stop docker.socket docker.service",
                "systemctl start docker",
                "journalctl --vacuum-size=500M",
            ],
        )


class TestRestartDockerShims(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-restart-shims-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.archive_tmp = self.tmpdir / "archive_tmp"
        self.archive_tmp.mkdir()
        self.py_log = self.tmpdir / "python.log"

        mock_id = self.mock_bin / "id"
        mock_id.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "${1:-}" = "-u" ]; then echo "${MOCK_UID:-0}"; exit 0; fi\n'
            'exec /usr/bin/id "$@"\n',
            encoding="utf-8",
        )
        mock_id.chmod(0o755)

        mock_py = self.mock_bin / "python3"
        mock_py.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "import yggdrasil_os\n"
            f"with open({str(self.py_log)!r}, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'argv': sys.argv[1:], 'module_file': yggdrasil_os.__file__}) + '\\n')\n",
            encoding="utf-8",
        )
        mock_py.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_in_tree_resolution_for_1_level_and_2_level_shims(self) -> None:
        expected_pairs = (
            (GAIA_RESTART_SCRIPT, ["restart-docker", "gaia"]),
            (MUSPELHEIM_RESTART_SCRIPT, ["restart-docker", "muspelheim"]),
        )
        for script, expected_cmd in expected_pairs:
            with self.subTest(script=script.name):
                mode = stat.S_IMODE(script.stat().st_mode)
                self.assertEqual(mode & 0o111, 0o111)
                self.assertEqual(mode & 0o002, 0)
                syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

                non_root = subprocess.run(
                    ["bash", str(script)],
                    env={"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin", "MOCK_UID": "1000"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(non_root.returncode, 1)
                self.assertIn("must be run as root", non_root.stderr)

                self.py_log.unlink(missing_ok=True)
                ok = subprocess.run(
                    ["bash", str(script)],
                    env={"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin", "MOCK_UID": "0"},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(ok.returncode, 0, ok.stderr + ok.stdout)
                entry = json.loads(self.py_log.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", *expected_cmd])
                self.assertEqual(
                    Path(entry["module_file"]).resolve(),
                    (REPO_ROOT / "yggdrasil_os" / "__init__.py").resolve(),
                )

    def test_detached_shim_tarball_cleans_tmpdir(self) -> None:
        archive_path = self.tmpdir / "yggdrasil-os-main.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(REPO_ROOT / "yggdrasil_os", arcname="yggdrasil-os-main/yggdrasil_os")

        detached = self.tmpdir / "muspelheim-restart-docker.sh"
        shutil.copy2(MUSPELHEIM_RESTART_SCRIPT, detached)
        detached.chmod(0o755)
        res = subprocess.run(
            ["bash", str(detached)],
            env={
                "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                "MOCK_UID": "0",
                "TMPDIR": str(self.archive_tmp),
                "YGGDRASIL_OS_ARCHIVE_URL": archive_path.as_uri(),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertEqual(list(self.archive_tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

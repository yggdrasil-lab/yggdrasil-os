"""Systemd and Netlink port protocols and stdlib adapters for Docker recovery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from yggdrasil_os.ports import CommandResult, CommandRunner


@dataclass(frozen=True)
class VxlanLink:
    """Immutable representation of a kernel VXLAN network interface."""

    name: str
    state: str


class SystemdPort(Protocol):
    """Port for systemd unit control and journal maintenance."""

    def stop_units(self, units: Sequence[str]) -> CommandResult:
        """Stop one or more systemd units."""

    def start_unit(self, unit: str) -> CommandResult:
        """Start a systemd unit."""

    def vacuum_journal(self, max_size: str) -> CommandResult:
        """Vacuum systemd journal logs to at most max_size."""


class NetlinkPort(Protocol):
    """Port for inspecting and deleting kernel VXLAN interfaces."""

    def is_available(self) -> bool:
        """Return True when netlink tooling is available on the host."""

    def list_vxlan_links(self) -> tuple[VxlanLink, ...]:
        """List kernel VXLAN interfaces and their operational states."""

    def delete_link(self, name: str) -> CommandResult:
        """Delete the named kernel network interface."""


class SubprocessSystemdPort:
    """Production SystemdPort backed by a CommandRunner."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def stop_units(self, units: Sequence[str]) -> CommandResult:
        return self._runner.run(["systemctl", "stop", *units], check=False)

    def start_unit(self, unit: str) -> CommandResult:
        return self._runner.run(["systemctl", "start", unit], check=False)

    def vacuum_journal(self, max_size: str) -> CommandResult:
        return self._runner.run(
            ["journalctl", f"--vacuum-size={max_size}"],
            check=False,
        )


class IpNetlinkPort:
    """Production NetlinkPort backed by iproute2 via CommandRunner."""

    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def is_available(self) -> bool:
        return self._runner.has_command("ip")

    def list_vxlan_links(self) -> tuple[VxlanLink, ...]:
        res = self._runner.run(
            ["ip", "-o", "link", "show", "type", "vxlan"],
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return ()
        return parse_vxlan_links(res.stdout)

    def delete_link(self, name: str) -> CommandResult:
        return self._runner.run(["ip", "link", "delete", name], check=False)


def parse_vxlan_links(stdout: str) -> tuple[VxlanLink, ...]:
    """Parse `ip -o link show type vxlan` output, stripping @peer suffixes."""
    links: list[VxlanLink] = []
    for raw_line in stdout.splitlines():
        parsed = _parse_vxlan_line(raw_line)
        if parsed is not None:
            links.append(parsed)
    return tuple(links)


def _parse_vxlan_line(line: str) -> VxlanLink | None:
    stripped = line.strip()
    if not stripped:
        return None
    parts = stripped.split(":", 2)
    if len(parts) < 3:
        return None
    name = parts[1].strip().split("@", 1)[0].strip()
    if not name:
        return None
    tokens = parts[2].split()
    state = ""
    for idx, token in enumerate(tokens):
        if token == "state" and idx + 1 < len(tokens):
            state = tokens[idx + 1]
            break
    return VxlanLink(name=name, state=state)

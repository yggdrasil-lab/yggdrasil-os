"""Command-line entrypoint for python3 -m yggdrasil_os."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from yggdrasil_os.ports import LocalOrRemoteAssetFetcher, SubprocessCommandRunner
from yggdrasil_os.provision import (
    ProvisionContext,
    fix_docker_dns_boot,
    install_cleanup_timer,
    install_startup_service,
    provision_host,
)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for yggdrasil_os."""
    parser = argparse.ArgumentParser(
        prog="yggdrasil_os",
        description="Yggdrasil OS host provisioning and installer runtime.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision_cmd = subparsers.add_parser(
        "provision",
        help="Provision a cluster node from its declarative manifest.",
    )
    provision_cmd.add_argument("host", choices=("gaia", "muspelheim"))

    subparsers.add_parser(
        "install-cleanup-timer",
        help="Install Gaia storage cleanup script and systemd timers.",
    )
    subparsers.add_parser(
        "install-startup-service",
        help="Install Gaia Swarm startup orchestrator and Docker boot drop-in.",
    )
    subparsers.add_parser(
        "fix-docker-dns-boot",
        help="Install Docker wait-for-network systemd drop-in.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to provisioning operations."""
    parser = build_parser()
    parsed = parser.parse_args(list(argv) if argv is not None else None)
    command = str(parsed.command)
    repo_root = Path(__file__).resolve().parents[1]
    ctx = ProvisionContext.from_env(repo_root=repo_root)
    runner = SubprocessCommandRunner()
    fetcher = LocalOrRemoteAssetFetcher(repo_root=ctx.repo_root, raw_base=ctx.raw_base)

    try:
        if command == "provision":
            provision_host(str(parsed.host), ctx, runner, fetcher)
        elif command == "install-cleanup-timer":
            install_cleanup_timer(ctx, runner, fetcher)
        elif command == "install-startup-service":
            install_startup_service(ctx, runner, fetcher)
        elif command == "fix-docker-dns-boot":
            fix_docker_dns_boot(ctx, runner)
        else:
            parser.error(f"Unsupported command: {command}")
    except (PermissionError, RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0

"""Command-line entrypoint for python3 -m yggdrasil_os."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from yggdrasil_os.convergence import (
    ConvergenceFailureError,
    parse_convergence_args,
    verify_convergence,
)
from yggdrasil_os.deploy import deploy_stack, parse_deploy_args
from yggdrasil_os.docker_port import ReportedCommandError, SubprocessDockerPort
from yggdrasil_os.ports import (
    CommandExecutionError,
    LocalOrRemoteAssetFetcher,
    SubprocessCommandRunner,
    SystemClock,
)
from yggdrasil_os.provision import (
    ProvisionContext,
    fix_docker_dns_boot,
    install_cleanup_timer,
    install_startup_service,
    provision_host,
)
from yggdrasil_os.secrets import (
    ensure_secret,
    parse_secret_base_arg,
    prune_secrets,
)

_DEPLOY_COMMANDS = frozenset({"deploy", "ensure-secret", "prune-secrets", "verify-convergence"})
_ENSURE_SECRET_USAGE = 'Usage: echo "content" | ./ensure_secret.sh <SECRET_BASE_NAME>'


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for yggdrasil_os."""
    parser = argparse.ArgumentParser(
        prog="yggdrasil_os",
        description="Yggdrasil OS host provisioning and deployment runtime.",
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
    subparsers.add_parser(
        "deploy",
        add_help=False,
        help="Build/push compose images, deploy Swarm stack, and prune unused secrets.",
    )
    subparsers.add_parser(
        "ensure-secret",
        add_help=False,
        help="Idempotently create a versioned Swarm secret from stdin.",
    )
    subparsers.add_parser(
        "prune-secrets",
        add_help=False,
        help="Prune unused versioned Swarm secrets matching a base prefix.",
    )
    subparsers.add_parser(
        "verify-convergence",
        add_help=False,
        help="Verify Swarm service convergence and task-ID dwell stability.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to provisioning or deployment operations."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if raw_argv and raw_argv[0] in _DEPLOY_COMMANDS:
            return _dispatch_deploy_command(raw_argv[0], raw_argv[1:])
        return _dispatch_provision_command(raw_argv)
    except ReportedCommandError as exc:
        return exc.returncode or 1
    except ConvergenceFailureError:
        return 1
    except CommandExecutionError as exc:
        detail = exc.stderr.strip() or str(exc)
        print(detail, file=sys.stderr)
        return exc.returncode or 1
    except (PermissionError, RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _dispatch_provision_command(argv: list[str]) -> int:
    parser = build_parser()
    parsed = parser.parse_args(argv)
    command = str(parsed.command)
    repo_root = Path(__file__).resolve().parents[1]
    ctx = ProvisionContext.from_env(repo_root=repo_root)
    runner = SubprocessCommandRunner()
    fetcher = LocalOrRemoteAssetFetcher(repo_root=ctx.repo_root, raw_base=ctx.raw_base)

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
    return 0


def _dispatch_deploy_command(command: str, args: list[str]) -> int:
    docker = SubprocessDockerPort()
    clock = SystemClock()

    if command == "deploy":
        config = parse_deploy_args(args)
        if config is not None:
            deploy_stack(config, docker, clock)
        return 0
    if command == "ensure-secret":
        base_name = parse_secret_base_arg(args, usage=_ENSURE_SECRET_USAGE)
        full_name = ensure_secret(base_name, sys.stdin.read(), docker)
        sys.stdout.write(f"{full_name}\n")
        return 0
    if command == "prune-secrets":
        base_name = parse_secret_base_arg(args)
        prune_secrets(docker, base=base_name)
        return 0
    if command == "verify-convergence":
        config = parse_convergence_args(args)
        if config is not None:
            verify_convergence(config, docker, clock)
        return 0
    raise ValueError(f"Unsupported deployment command: {command}")

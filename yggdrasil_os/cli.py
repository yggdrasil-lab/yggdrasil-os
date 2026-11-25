"""Command-line entrypoint for python3 -m yggdrasil_os."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Literal, Sequence, cast

from yggdrasil_os.cleanup import cleanup_storage, parse_cleanup_args
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
from yggdrasil_os.recovery import RestartContext, restart_docker
from yggdrasil_os.recovery_ports import IpNetlinkPort, SubprocessSystemdPort
from yggdrasil_os.secrets import (
    ensure_secret,
    parse_secret_base_arg,
    prune_secrets,
)
from yggdrasil_os.startup import StartupContext, run_startup_orchestrator

_DEPLOY_COMMANDS = frozenset({"deploy", "ensure-secret", "prune-secrets", "verify-convergence"})
_RECOVERY_COMMANDS = frozenset({"restart-docker", "startup-orchestrator"})
_ENSURE_SECRET_USAGE = 'Usage: echo "content" | ./ensure_secret.sh <SECRET_BASE_NAME>'


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for yggdrasil_os."""
    parser = argparse.ArgumentParser(
        prog="yggdrasil_os",
        description="Yggdrasil OS host provisioning, recovery, and deployment runtime.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_provision_subparsers(subparsers)
    _add_deploy_subparsers(subparsers)
    _add_recovery_subparsers(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments and dispatch to provisioning, recovery, or deploy operations."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if raw_argv and raw_argv[0] in _DEPLOY_COMMANDS:
            return _dispatch_deploy_command(raw_argv[0], raw_argv[1:])
        if raw_argv and raw_argv[0] == "cleanup-storage":
            return _dispatch_cleanup_command(raw_argv[1:])
        return _dispatch_cli_command(raw_argv)
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


def _add_provision_subparsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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


def _add_deploy_subparsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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


def _add_recovery_subparsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    restart_cmd = subparsers.add_parser(
        "restart-docker",
        help="Restart Docker daemon with VXLAN cleanup and host-role recovery hooks.",
    )
    restart_cmd.add_argument("role", choices=("gaia", "muspelheim"))
    subparsers.add_parser(
        "startup-orchestrator",
        help="Run Gaia Swarm startup convergence, auto-healing, and safe prune.",
    )
    subparsers.add_parser(
        "cleanup-storage",
        add_help=False,
        help="Prune stale Docker containers, volumes, images, endpoints, and journal logs.",
    )


def _dispatch_cli_command(argv: list[str]) -> int:
    parser = build_parser()
    parsed = parser.parse_args(argv)
    command = str(parsed.command)
    repo_root = Path(__file__).resolve().parents[1]
    if command in _RECOVERY_COMMANDS:
        return _dispatch_recovery_command(command, parsed, repo_root)
    return _dispatch_provision_command(command, parsed, repo_root, parser)


def _dispatch_provision_command(
    command: str,
    parsed: argparse.Namespace,
    repo_root: Path,
    parser: argparse.ArgumentParser,
) -> int:
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


def _dispatch_recovery_command(
    command: str,
    parsed: argparse.Namespace,
    repo_root: Path,
) -> int:
    docker = SubprocessDockerPort()
    clock = SystemClock()
    if command == "startup-orchestrator":
        run_startup_orchestrator(StartupContext.from_env(), docker=docker, clock=clock)
        return 0
    ctx = RestartContext.from_env(repo_root=repo_root)
    runner = SubprocessCommandRunner()
    fetcher = LocalOrRemoteAssetFetcher(repo_root=ctx.repo_root, raw_base=ctx.raw_base)
    role = cast(Literal["gaia", "muspelheim"], str(parsed.role))
    restart_docker(
        role,
        ctx,
        docker=docker,
        systemd=SubprocessSystemdPort(runner),
        netlink=IpNetlinkPort(runner),
        runner=runner,
        fetcher=fetcher,
        clock=clock,
    )
    return 0


def _dispatch_cleanup_command(args: list[str]) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    config = parse_cleanup_args(args, repo_root=repo_root)
    runner = SubprocessCommandRunner()
    cleanup_storage(
        config,
        docker=SubprocessDockerPort(),
        systemd=SubprocessSystemdPort(runner),
        runner=runner,
    )
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

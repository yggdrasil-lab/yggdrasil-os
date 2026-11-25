"""Declarative host provisioning and standalone service installers."""

from __future__ import annotations

import getpass
import grp
import os
import pwd
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from yggdrasil_os.manifest import (
    AssetInstallSpec,
    CentralCliSpec,
    ChownSpec,
    HostManifest,
    PathModeSpec,
    PostCommandSpec,
    RecursiveChmodSpec,
    load_manifest,
)
from yggdrasil_os.ports import AssetFetcher, CommandRunner

DEFAULT_RAW_BASE = "https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main"

WAIT_FOR_NETWORK_CONF = """[Unit]
# Wait until the network is actually "online" (not just the interface being up)
# This usually means having an IP address and connectivity.
After=network-online.target firewalld.service
Wants=network-online.target

[Service]
# Add a conservative 15-second delay to the startup.
# This is crucial after a power outage where the Router might still be booting up
# while the Server is already asking for DHCP/DNS.
ExecStartPre=/bin/sleep 15
"""

_CLEANUP_TIMER_UNITS = (
    "gaia-cleanup.service",
    "gaia-cleanup.timer",
    "gaia-container-prune.service",
    "gaia-container-prune.timer",
)

_FORGE_ARTIFACT_DIRS = (
    "dist",
    "test-results",
    "playwright-report",
    "node_modules/.vite",
)


@dataclass(frozen=True)
class ProvisionContext:
    """Immutable runtime context and path mapping for provisioning."""

    repo_root: Path | None = None
    root_dir: Path = Path("/")
    raw_base: str = DEFAULT_RAW_BASE
    sudo_user: str = ""
    target_user: str = ""
    target_group: str = ""
    user_local_bin: Path | None = None
    central_bin_dir: Path | None = None
    vault_path: Path | None = None
    cleanup_bin_path: Path | None = None
    cleanup_systemd_dir: Path | None = None
    euid: int | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        repo_root: Path | None = None,
        root_dir: Path = Path("/"),
        euid: int | None = None,
    ) -> ProvisionContext:
        """Build ProvisionContext from environment variables and defaults."""
        env = os.environ if environ is None else environ
        sudo_user = env.get("SUDO_USER", "").strip()
        target_user = sudo_user or _current_username()
        target_group = _resolve_primary_group(target_user)
        return cls(
            repo_root=repo_root,
            root_dir=root_dir,
            raw_base=env.get("RAW_BASE") or DEFAULT_RAW_BASE,
            sudo_user=sudo_user,
            target_user=target_user,
            target_group=target_group,
            user_local_bin=_optional_path(env.get("USER_LOCAL_BIN")),
            central_bin_dir=_optional_path(env.get("CENTRAL_BIN_DIR")),
            vault_path=_optional_path(env.get("OBSIDIAN_VAULT_PATH")),
            cleanup_bin_path=_optional_path(env.get("GAIA_CLEANUP_BIN_PATH")),
            cleanup_systemd_dir=_optional_path(env.get("GAIA_SYSTEMD_DIR")),
            euid=euid,
        )

    @property
    def effective_euid(self) -> int:
        return os.geteuid() if self.euid is None else self.euid

    @property
    def effective_target_user(self) -> str:
        return self.target_user or self.sudo_user or _current_username()

    @property
    def effective_target_group(self) -> str:
        if self.target_group:
            return self.target_group
        return _resolve_primary_group(self.effective_target_user)

    @property
    def effective_vault_path(self) -> Path:
        if self.vault_path is not None:
            return self.vault_path
        return self.map_path("/opt/atlas/vault")

    @property
    def effective_central_bin_dir(self) -> Path:
        if self.central_bin_dir is not None:
            return self.central_bin_dir
        return self.map_path("/usr/local/bin")

    @property
    def effective_user_local_bin(self) -> Path:
        if self.user_local_bin is not None:
            return self.user_local_bin
        return self.map_path(f"/home/{self.effective_target_user}/.local/bin")

    @property
    def effective_cleanup_bin_path(self) -> Path:
        if self.cleanup_bin_path is not None:
            return self.cleanup_bin_path
        return self.map_path("/usr/local/bin/gaia-cleanup.sh")

    @property
    def effective_cleanup_systemd_dir(self) -> Path:
        if self.cleanup_systemd_dir is not None:
            return self.cleanup_systemd_dir
        return self.map_path("/etc/systemd/system")

    def map_path(self, raw_path: str | Path) -> Path:
        """Map an absolute canonical host path under root_dir."""
        path = Path(raw_path)
        if self.root_dir == Path("/"):
            return path
        if path.is_absolute():
            if path.is_relative_to(self.root_dir):
                return path
            return self.root_dir / path.relative_to("/")
        return self.root_dir / path

    def format_template(self, template: str) -> str:
        """Expand manifest placeholders without mapping root_dir."""
        target_owner = f"{self.effective_target_user}:{self.effective_target_group}"
        return template.format(
            vault_path=str(self.effective_vault_path),
            user_local_bin=str(self.effective_user_local_bin),
            central_bin_dir=str(self.effective_central_bin_dir),
            target_owner=target_owner,
        )

    def resolve_manifest_path(self, template: str) -> Path:
        """Expand placeholders and map canonical paths under root_dir."""
        if "{vault_path}" in template:
            return Path(self.format_template(template))
        if "{user_local_bin}" in template:
            return Path(self.format_template(template))
        if "{central_bin_dir}" in template:
            return Path(self.format_template(template))
        return self.map_path(template)


def provision_host(
    host: str,
    ctx: ProvisionContext,
    runner: CommandRunner,
    fetcher: AssetFetcher,
    *,
    manifest: HostManifest | None = None,
) -> None:
    """Provision directories, permissions, CLIs, symlinks, and units for host."""
    spec = load_manifest(host) if manifest is None else manifest
    _run_pre_scripts(spec.pre_scripts, runner, fetcher)
    _remove_paths(spec.remove_paths, ctx)
    _ensure_directories(spec.directories, ctx)
    _apply_chowns(spec.recursive_chowns, ctx, runner, recursive=True)
    _apply_chowns(spec.non_recursive_chowns, ctx, runner, recursive=False)
    _apply_recursive_chmods(spec.recursive_chmods, ctx)
    _apply_path_modes(spec.path_modes, ctx)
    _install_turnkey_and_conditional_symlinks(spec, ctx, runner)
    _apply_git_global_config(spec.git_global_config, ctx, runner)
    _install_assets(spec.assets, ctx, fetcher)
    _install_central_clis(spec.central_clis, ctx, runner, fetcher)
    _run_post_commands(spec.post_commands, ctx, runner)
    _enable_manifest_systemd(spec, runner)
    if spec.install_cleanup_timer:
        install_cleanup_timer(ctx, runner, fetcher, require_root=False)
    if spec.vault_sweeps:
        _run_vault_sweeps(ctx, runner)


def install_cleanup_timer(
    ctx: ProvisionContext,
    runner: CommandRunner,
    fetcher: AssetFetcher,
    *,
    require_root: bool = True,
) -> None:
    """Install gaia-cleanup.sh plus monthly cleanup and daily prune timers."""
    _require_root(ctx, require_root)
    _install_asset_file(
        fetcher,
        "maintenance/cleanup_storage.sh",
        ctx.effective_cleanup_bin_path,
        0o755,
    )
    systemd_dir = ctx.effective_cleanup_systemd_dir
    for unit in _CLEANUP_TIMER_UNITS:
        _install_asset_file(
            fetcher,
            f"maintenance/systemd/{unit}",
            systemd_dir / unit,
            0o644,
        )
    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", "--now", "gaia-cleanup.timer"])
    runner.run(["systemctl", "enable", "--now", "gaia-container-prune.timer"])
    timers = runner.run(["systemctl", "list-timers", "--all"], check=False)
    for line in timers.stdout.splitlines():
        if any(tok in line for tok in ("gaia-cleanup", "gaia-container-prune", "NEXT")):
            print(line)


def install_startup_service(
    ctx: ProvisionContext,
    runner: CommandRunner,
    fetcher: AssetFetcher,
    *,
    require_root: bool = True,
) -> None:
    """Install Swarm startup orchestrator, restart helper, and Docker boot drop-in."""
    _require_root(ctx, require_root)
    _install_asset_file(
        fetcher,
        "swarm/startup_orchestrator.sh",
        ctx.map_path("/usr/local/bin/gaia-swarm-startup.sh"),
        0o755,
    )
    _install_asset_file(
        fetcher,
        "docker/restart_docker.sh",
        ctx.map_path("/usr/local/bin/gaia-restart-docker.sh"),
        0o755,
    )
    _write_text_file(
        ctx.map_path("/etc/systemd/system/docker.service.d/wait-for-network.conf"),
        WAIT_FOR_NETWORK_CONF,
        0o644,
    )
    _install_asset_file(
        fetcher,
        "swarm/systemd/gaia-swarm-startup.service",
        ctx.map_path("/etc/systemd/system/gaia-swarm-startup.service"),
        0o644,
    )
    runner.run(["systemctl", "daemon-reload"])
    runner.run(["systemctl", "enable", "gaia-swarm-startup.service"])
    docker_info = runner.run(["docker", "info"], check=False)
    if docker_info.returncode == 0:
        runner.run(
            ["docker", "swarm", "update", "--task-history-limit", "1"],
            check=False,
        )


def fix_docker_dns_boot(
    ctx: ProvisionContext,
    runner: CommandRunner,
    *,
    require_root: bool = True,
) -> None:
    """Write Docker wait-for-network systemd drop-in and reload systemd."""
    _require_root(ctx, require_root)
    _write_text_file(
        ctx.map_path("/etc/systemd/system/docker.service.d/wait-for-network.conf"),
        WAIT_FOR_NETWORK_CONF,
        0o644,
    )
    runner.run(["systemctl", "daemon-reload"])


def _require_root(ctx: ProvisionContext, require_root: bool) -> None:
    if require_root and ctx.effective_euid != 0:
        raise PermissionError("Error: This script must be run as root (sudo).")


def _run_pre_scripts(
    pre_scripts: tuple[str, ...],
    runner: CommandRunner,
    fetcher: AssetFetcher,
) -> None:
    for rel_path in pre_scripts:
        local = fetcher.local_path(rel_path)
        if local is not None:
            runner.run(["bash", str(local)])
            continue
        script_text = fetcher.fetch_bytes(rel_path).decode("utf-8")
        runner.run(["bash", "-s"], input_text=script_text)


def _remove_paths(paths: tuple[str, ...], ctx: ProvisionContext) -> None:
    for raw_path in paths:
        target = ctx.resolve_manifest_path(raw_path)
        if target.is_symlink() or target.is_file():
            target.unlink()


def _ensure_directories(paths: tuple[str, ...], ctx: ProvisionContext) -> None:
    for raw_path in paths:
        ctx.resolve_manifest_path(raw_path).mkdir(parents=True, exist_ok=True)


def _apply_chowns(
    specs: tuple[ChownSpec, ...],
    ctx: ProvisionContext,
    runner: CommandRunner,
    *,
    recursive: bool,
) -> None:
    for spec in specs:
        target = ctx.resolve_manifest_path(spec.path)
        if spec.if_exists and not target.exists():
            continue
        owner = ctx.format_template(spec.owner)
        argv = ["chown", "-R", owner, str(target)] if recursive else ["chown", owner, str(target)]
        runner.run(argv)


def _apply_recursive_chmods(
    specs: tuple[RecursiveChmodSpec, ...],
    ctx: ProvisionContext,
) -> None:
    for spec in specs:
        root = ctx.resolve_manifest_path(spec.path)
        if not root.exists():
            continue
        for current in _walk_non_symlinks(root):
            if spec.mode == "o+w":
                current_mode = stat.S_IMODE(current.stat().st_mode)
                os.chmod(current, current_mode | stat.S_IWOTH)
            else:
                os.chmod(current, int(spec.mode, 8))


def _walk_non_symlinks(root: Path) -> list[Path]:
    collected: list[Path] = []
    if not root.is_symlink():
        collected.append(root)
    if not root.is_dir() or root.is_symlink():
        return collected
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in dirnames:
            candidate = base / name
            if not candidate.is_symlink():
                collected.append(candidate)
        for name in filenames:
            candidate = base / name
            if not candidate.is_symlink():
                collected.append(candidate)
    return collected


def _apply_path_modes(specs: tuple[PathModeSpec, ...], ctx: ProvisionContext) -> None:
    for spec in specs:
        target = ctx.resolve_manifest_path(spec.path)
        if not target.exists():
            if spec.ignore_missing:
                continue
            raise FileNotFoundError(f"Expected path for chmod does not exist: {target}")
        os.chmod(target, spec.mode)


def _install_turnkey_and_conditional_symlinks(
    spec: HostManifest,
    ctx: ProvisionContext,
    runner: CommandRunner,
) -> None:
    if not spec.turnkey_symlinks and not spec.conditional_symlinks:
        return
    user_local_bin = _ensure_user_local_bin(ctx, runner)
    for cond in spec.conditional_symlinks:
        target = Path(ctx.format_template(cond.target))
        if target.is_file():
            _force_symlink(target, ctx.resolve_manifest_path(cond.link))
    owner = f"{ctx.effective_target_user}:{ctx.effective_target_group}"
    for link_spec in spec.turnkey_symlinks:
        target = Path(ctx.format_template(link_spec.target))
        dest = user_local_bin / link_spec.link_name
        _force_symlink(target, dest)
        runner.run(["chown", "-h", owner, str(dest)], check=False)


def _apply_git_global_config(
    settings: tuple[tuple[str, str], ...],
    ctx: ProvisionContext,
    runner: CommandRunner,
) -> None:
    if not settings:
        return
    for key, value in settings:
        runner.run(["git", "config", "--global", key, value])
    if ctx.sudo_user and ctx.sudo_user != "root":
        target_user = ctx.effective_target_user
        for key, value in settings:
            runner.run(["sudo", "-u", target_user, "git", "config", "--global", key, value])


def _install_assets(
    assets: tuple[AssetInstallSpec, ...],
    ctx: ProvisionContext,
    fetcher: AssetFetcher,
) -> None:
    for asset in assets:
        _install_asset_file(
            fetcher,
            asset.src,
            ctx.resolve_manifest_path(asset.dest),
            asset.mode,
        )


def _install_central_clis(
    clis: tuple[CentralCliSpec, ...],
    ctx: ProvisionContext,
    runner: CommandRunner,
    fetcher: AssetFetcher,
) -> None:
    if not clis:
        return
    central_dir = ctx.effective_central_bin_dir
    user_local_bin = _ensure_user_local_bin(ctx, runner)
    owner = f"{ctx.effective_target_user}:{ctx.effective_target_group}"
    for cli in clis:
        target_bin = central_dir / cli.bin_name
        _install_asset_file(fetcher, cli.src, target_bin, 0o755)
        link_path = user_local_bin / cli.bin_name
        _force_symlink(target_bin, link_path)
        runner.run(["chown", "-h", owner, str(link_path)], check=False)


def _ensure_user_local_bin(ctx: ProvisionContext, runner: CommandRunner) -> Path:
    user_local_bin = ctx.effective_user_local_bin
    if not user_local_bin.is_dir():
        user_local_bin.mkdir(parents=True, exist_ok=True)
        owner = f"{ctx.effective_target_user}:{ctx.effective_target_group}"
        runner.run(["chown", owner, str(user_local_bin)], check=False)
    return user_local_bin


def _run_post_commands(
    commands: tuple[PostCommandSpec, ...],
    ctx: ProvisionContext,
    runner: CommandRunner,
) -> None:
    for cmd in commands:
        argv = list(cmd.argv)
        if cmd.map_first_arg and argv:
            argv[0] = str(ctx.resolve_manifest_path(argv[0]))
        env = dict(cmd.env) if cmd.env else None
        runner.run(argv, env=env)


def _enable_manifest_systemd(spec: HostManifest, runner: CommandRunner) -> None:
    if not spec.systemd.enable and not spec.systemd.enable_now:
        return
    if not runner.has_command("systemctl"):
        return
    runner.run(["systemctl", "daemon-reload"])
    for unit in spec.systemd.enable:
        runner.run(["systemctl", "enable", unit])
    for unit in spec.systemd.enable_now:
        runner.run(["systemctl", "enable", "--now", unit])


def _run_vault_sweeps(ctx: ProvisionContext, runner: CommandRunner) -> None:
    vault_dir = ctx.effective_vault_path
    owner = f"{ctx.effective_target_user}:{ctx.effective_target_group}"
    git_dir = vault_dir / ".git"
    if git_dir.is_dir():
        runner.run(["chown", "-R", owner, str(git_dir)])
    for rel_name in _FORGE_ARTIFACT_DIRS:
        for pattern in (f"Forge/*/{rel_name}", f"Forge/*/web/{rel_name}"):
            for artifact_dir in sorted(vault_dir.glob(pattern)):
                if artifact_dir.is_dir():
                    runner.run(["chown", "-R", owner, str(artifact_dir)])
    for pattern in ("Forge/*/node_modules/.vite_root_owned", "Forge/*/web/node_modules/.vite_root_owned"):
        for stale_dir in sorted(vault_dir.glob(pattern)):
            if stale_dir.is_symlink():
                stale_dir.unlink()
            elif stale_dir.is_dir():
                shutil.rmtree(stale_dir, ignore_errors=True)


def _install_asset_file(
    fetcher: AssetFetcher,
    rel_src: str,
    dest: Path,
    mode: int,
) -> None:
    payload = fetcher.fetch_bytes(rel_src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    os.chmod(dest, mode)


def _write_text_file(dest: Path, content: str, mode: int) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    os.chmod(dest, mode)


def _force_symlink(target: Path, link_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.is_symlink() or link_path.exists():
        if link_path.is_dir() and not link_path.is_symlink():
            raise IsADirectoryError(f"Refusing to overwrite directory with symlink: {link_path}")
        link_path.unlink()
    link_path.symlink_to(target)


def _optional_path(raw: str | None) -> Path | None:
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


def _current_username() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return getpass.getuser()


def _resolve_primary_group(username: str) -> str:
    try:
        gid = pwd.getpwnam(username).pw_gid
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return username

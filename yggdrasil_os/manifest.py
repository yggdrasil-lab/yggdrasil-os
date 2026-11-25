"""Declarative host manifest models and JSON loader."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """Raised when a host manifest is missing or malformed."""


@dataclass(frozen=True)
class ChownSpec:
    """Ownership assignment specification."""

    path: str
    owner: str
    if_exists: bool = False


@dataclass(frozen=True)
class RecursiveChmodSpec:
    """Recursive permission change specification."""

    path: str
    mode: str


@dataclass(frozen=True)
class PathModeSpec:
    """Single-path permission change specification."""

    path: str
    mode: int
    ignore_missing: bool = False


@dataclass(frozen=True)
class AssetInstallSpec:
    """File installation from repo asset to host destination."""

    src: str
    dest: str
    mode: int


@dataclass(frozen=True)
class CentralCliSpec:
    """Central CLI binary plus user ~/.local/bin symlink."""

    src: str
    bin_name: str


@dataclass(frozen=True)
class TurnkeySymlinkSpec:
    """Symlink installed inside USER_LOCAL_BIN."""

    target: str
    link_name: str


@dataclass(frozen=True)
class ConditionalSymlinkSpec:
    """Symlink created only when target already exists as a file."""

    target: str
    link: str


@dataclass(frozen=True)
class PostCommandSpec:
    """Command executed after files and directories are provisioned."""

    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...]
    map_first_arg: bool = False


@dataclass(frozen=True)
class SystemdSpec:
    """Systemd units to enable on the target host."""

    enable: tuple[str, ...]
    enable_now: tuple[str, ...]


@dataclass(frozen=True)
class HostManifest:
    """Complete declarative provisioning manifest for a cluster node."""

    host: str
    remove_paths: tuple[str, ...]
    pre_scripts: tuple[str, ...]
    directories: tuple[str, ...]
    recursive_chowns: tuple[ChownSpec, ...]
    non_recursive_chowns: tuple[ChownSpec, ...]
    recursive_chmods: tuple[RecursiveChmodSpec, ...]
    path_modes: tuple[PathModeSpec, ...]
    assets: tuple[AssetInstallSpec, ...]
    central_clis: tuple[CentralCliSpec, ...]
    turnkey_symlinks: tuple[TurnkeySymlinkSpec, ...]
    conditional_symlinks: tuple[ConditionalSymlinkSpec, ...]
    git_global_config: tuple[tuple[str, str], ...]
    post_commands: tuple[PostCommandSpec, ...]
    systemd: SystemdSpec
    install_cleanup_timer: bool
    vault_sweeps: bool


def load_manifest(host: str) -> HostManifest:
    """Load and validate the bundled JSON manifest for host."""
    if not host or "/" in host or "\\" in host or ".." in host:
        raise ManifestError(f"Invalid host manifest name: {host!r}")
    manifest_file = resources.files("yggdrasil_os.manifests").joinpath(f"{host}.json")
    if not manifest_file.is_file():
        raise ManifestError(f"Unknown host manifest: {host!r}")
    raw = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("host") != host:
        raise ManifestError(f"Malformed manifest payload for {host!r}")
    return _parse_manifest(raw)


def load_manifest_file(path: Path) -> HostManifest:
    """Load and validate a manifest directly from a filesystem path."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("host"), str):
        raise ManifestError(f"Malformed manifest file: {path}")
    return _parse_manifest(raw)


def _parse_manifest(raw: dict[str, Any]) -> HostManifest:
    systemd_raw = raw.get("systemd", {})
    git_raw = raw.get("git_global_config", {})
    return HostManifest(
        host=str(raw["host"]),
        remove_paths=tuple(raw.get("remove_paths", ())),
        pre_scripts=tuple(raw.get("pre_scripts", ())),
        directories=tuple(raw.get("directories", ())),
        recursive_chowns=_parse_chowns(raw.get("recursive_chowns", ())),
        non_recursive_chowns=_parse_chowns(raw.get("non_recursive_chowns", ())),
        recursive_chmods=_parse_recursive_chmods(raw.get("recursive_chmods", ())),
        path_modes=_parse_path_modes(raw.get("path_modes", ())),
        assets=_parse_assets(raw.get("assets", ())),
        central_clis=_parse_central_clis(raw.get("central_clis", ())),
        turnkey_symlinks=_parse_turnkey_symlinks(raw.get("turnkey_symlinks", ())),
        conditional_symlinks=_parse_conditional_symlinks(raw.get("conditional_symlinks", ())),
        git_global_config=tuple((str(k), str(v)) for k, v in git_raw.items()),
        post_commands=_parse_post_commands(raw.get("post_commands", ())),
        systemd=SystemdSpec(
            enable=tuple(systemd_raw.get("enable", ())),
            enable_now=tuple(systemd_raw.get("enable_now", ())),
        ),
        install_cleanup_timer=bool(raw.get("install_cleanup_timer", False)),
        vault_sweeps=bool(raw.get("vault_sweeps", False)),
    )


def _parse_recursive_chmods(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[RecursiveChmodSpec, ...]:
    return tuple(RecursiveChmodSpec(path=item["path"], mode=item["mode"]) for item in items)


def _parse_path_modes(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[PathModeSpec, ...]:
    return tuple(
        PathModeSpec(
            path=item["path"],
            mode=_parse_octal_mode(item["mode"]),
            ignore_missing=bool(item.get("ignore_missing", False)),
        )
        for item in items
    )


def _parse_assets(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[AssetInstallSpec, ...]:
    return tuple(
        AssetInstallSpec(src=item["src"], dest=item["dest"], mode=_parse_octal_mode(item["mode"]))
        for item in items
    )


def _parse_central_clis(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[CentralCliSpec, ...]:
    return tuple(CentralCliSpec(src=item["src"], bin_name=item["bin_name"]) for item in items)


def _parse_turnkey_symlinks(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[TurnkeySymlinkSpec, ...]:
    return tuple(TurnkeySymlinkSpec(target=item["target"], link_name=item["link_name"]) for item in items)


def _parse_conditional_symlinks(
    items: list[dict[str, Any]] | tuple[Any, ...],
) -> tuple[ConditionalSymlinkSpec, ...]:
    return tuple(ConditionalSymlinkSpec(target=item["target"], link=item["link"]) for item in items)


def _parse_post_commands(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[PostCommandSpec, ...]:
    return tuple(
        PostCommandSpec(
            argv=tuple(item["argv"]),
            env=tuple((str(k), str(v)) for k, v in item.get("env", {}).items()),
            map_first_arg=bool(item.get("map_first_arg", False)),
        )
        for item in items
    )


def _parse_chowns(items: list[dict[str, Any]] | tuple[Any, ...]) -> tuple[ChownSpec, ...]:
    return tuple(
        ChownSpec(
            path=item["path"],
            owner=item["owner"],
            if_exists=bool(item.get("if_exists", False)),
        )
        for item in items
    )


def _parse_octal_mode(value: str | int) -> int:
    if isinstance(value, int):
        return value
    return int(value, 8)

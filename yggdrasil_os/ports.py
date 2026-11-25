"""Port protocols and stdlib adapters for host provisioning."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CommandExecutionError(RuntimeError):
    """Raised when an external command exits with a non-zero status."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        cmd_str = " ".join(argv)
        detail = stderr.strip() or stdout.strip() or "no output"
        super().__init__(f"Command failed ({returncode}): {cmd_str}: {detail}")
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class AssetFetchError(RuntimeError):
    """Raised when an asset cannot be read locally or fetched remotely."""


@dataclass(frozen=True)
class CommandResult:
    """Immutable result from an external command execution."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Port for running external host commands."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        """Execute argv and return its captured CommandResult."""

    def has_command(self, name: str) -> bool:
        """Return True when executable name is available on PATH."""


class AssetFetcher(Protocol):
    """Port for resolving repository assets locally or over HTTP."""

    def local_path(self, rel_path: str) -> Path | None:
        """Return the local file path when available inside repo_root."""

    def fetch_bytes(self, rel_path: str) -> bytes:
        """Load asset bytes locally or remotely; fail loud on missing asset."""


class Clock(Protocol):
    """Port for monotonic time queries and sleep intervals."""

    def monotonic(self) -> float:
        """Return current monotonic timestamp in seconds."""

    def sleep(self, seconds: float) -> None:
        """Sleep for the requested duration in seconds."""


class SystemClock:
    """Production Clock backed by Python stdlib time."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class SubprocessCommandRunner:
    """Production CommandRunner backed by Python stdlib subprocess."""

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        input_text: str | None = None,
    ) -> CommandResult:
        merged_env = {**os.environ, **env} if env is not None else None
        completed = subprocess.run(
            list(argv),
            env=merged_env,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and completed.returncode != 0:
            raise CommandExecutionError(
                argv=argv,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def has_command(self, name: str) -> bool:
        return shutil.which(name) is not None


@dataclass(frozen=True)
class LocalOrRemoteAssetFetcher:
    """Resolve assets under repo_root first, falling back to raw_base HTTP."""

    repo_root: Path | None
    raw_base: str
    timeout_seconds: float = 15.0
    user_agent: str = "Yggdrasil-OS-Provisioner/1.0"

    def local_path(self, rel_path: str) -> Path | None:
        clean_rel = _normalize_rel_path(rel_path)
        if self.repo_root is None:
            return None
        candidate = (self.repo_root / clean_rel).resolve()
        root_resolved = self.repo_root.resolve()
        if not candidate.is_relative_to(root_resolved):
            raise AssetFetchError(f"Asset path escapes repo_root: {rel_path}")
        return candidate if candidate.is_file() else None

    def fetch_bytes(self, rel_path: str) -> bytes:
        local = self.local_path(rel_path)
        if local is not None:
            return local.read_bytes()
        clean_rel = _normalize_rel_path(rel_path)
        url = f"{self.raw_base.rstrip('/')}/{clean_rel}"
        request = Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status is not None and not (200 <= status < 300):
                    raise AssetFetchError(f"HTTP {status} fetching {url}")
                return response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise AssetFetchError(f"Failed to fetch {url}: {exc}") from exc


def _normalize_rel_path(rel_path: str) -> str:
    stripped = rel_path.lstrip("/")
    parts = Path(stripped).parts
    if not stripped or ".." in parts:
        raise AssetFetchError(f"Invalid relative asset path: {rel_path!r}")
    return Path(*parts).as_posix()

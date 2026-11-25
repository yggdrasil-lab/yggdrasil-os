"""Docker CLI port protocol and subprocess adapter."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from yggdrasil_os.ports import CommandExecutionError, CommandResult


class ReportedCommandError(CommandExecutionError):
    """CommandExecutionError whose diagnostic output was already emitted."""


class DockerPort(Protocol):
    """Port for executing Docker CLI commands with optional stdin and timeout."""

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
        """Execute `docker <args...>` and return its captured CommandResult."""


class SubprocessDockerPort:
    """Production DockerPort backed by Python stdlib subprocess."""

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
        argv = ["docker", *args]
        merged_env = {**os.environ, **env} if env is not None else None
        work_dir = str(cwd) if cwd is not None else None
        try:
            completed = subprocess.run(
                argv,
                env=merged_env,
                cwd=work_dir,
                input=stdin_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _handle_timeout(argv, exc, timeout=timeout, check=check)

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


def _handle_timeout(
    argv: Sequence[str],
    exc: subprocess.TimeoutExpired,
    *,
    timeout: float | None,
    check: bool,
) -> CommandResult:
    stdout = _coerce_stream_text(exc.stdout)
    stderr = _coerce_stream_text(exc.stderr)
    timeout_note = f"Command timed out after {timeout}s"
    combined_stderr = f"{stderr}\n{timeout_note}".strip() if stderr else timeout_note
    if check:
        raise CommandExecutionError(
            argv=argv,
            returncode=124,
            stdout=stdout,
            stderr=combined_stderr,
        ) from exc
    return CommandResult(returncode=124, stdout=stdout, stderr=combined_stderr)


def _coerce_stream_text(raw: bytes | str | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw

"""Docker Swarm stack deployment, image push retry, and recreate fallback."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence, TextIO

from yggdrasil_os.docker_port import DockerPort, ReportedCommandError
from yggdrasil_os.ports import Clock, CommandResult
from yggdrasil_os.secrets import prune_secrets

MAX_PUSH_ATTEMPTS = 3
MAX_TEARDOWN_POLLS = 35
TEARDOWN_POLL_INTERVAL = 2.0
TEARDOWN_CMD_TIMEOUT = 5.0

DEPLOY_USAGE = "Usage: deploy.sh [--skip-build] [--recreate] <STACK_NAME> [COMPOSE_FILES...]"

IMMUTABLE_DEPLOY_ERROR_RE = re.compile(
    r"only updates to Labels are allowed|failed to update config|failed to update secret"
    r"|is not allowed|is not supported|immutable",
    re.IGNORECASE,
)


class DeployConfigError(ValueError):
    """Raised when deploy CLI arguments or configuration are invalid."""


@dataclass(frozen=True)
class DeployConfig:
    """Immutable configuration for a Swarm stack deployment."""

    stack_name: str
    compose_files: tuple[Path, ...] = (Path("docker-compose.yml"),)
    skip_build: bool = False
    recreate: bool = False
    work_dir: Path = Path(".")


@dataclass(frozen=True)
class DeployResult:
    """Immutable summary returned after a successful stack deployment."""

    stack_name: str
    pushed_images: tuple[str, ...]
    recreated: bool
    pruned_secrets: tuple[str, ...]


def parse_deploy_args(
    argv: Sequence[str],
    *,
    work_dir: Path | None = None,
    out: TextIO | None = None,
) -> DeployConfig | None:
    """Parse interleaved deploy flags, stack name, and compose file paths."""
    out_stream = sys.stdout if out is None else out
    skip_build = False
    recreate = False
    positionals: list[str] = []

    for token in argv:
        if token in ("-h", "--help"):
            out_stream.write(f"{DEPLOY_USAGE}\n")
            return None
        if token == "--skip-build":
            skip_build = True
        elif token == "--recreate":
            recreate = True
        elif token.startswith("-"):
            raise DeployConfigError(f"Error: Unknown option '{token}'.\n{DEPLOY_USAGE}")
        else:
            positionals.append(token)

    if not positionals or not positionals[0].strip():
        raise DeployConfigError(DEPLOY_USAGE)

    stack_name = positionals[0]
    compose_args = positionals[1:] or ["docker-compose.yml"]
    compose_files = tuple(Path(item) for item in compose_args)
    return DeployConfig(
        stack_name=stack_name,
        compose_files=compose_files,
        skip_build=skip_build,
        recreate=recreate,
        work_dir=Path(".") if work_dir is None else work_dir,
    )


def load_dotenv(dotenv_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a .env file without mutating os.environ."""
    parsed: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        clean_key = key.strip()
        if clean_key:
            parsed[clean_key] = _parse_env_value(raw_val.strip())
    return parsed


def extract_built_images(compose_config_json: str) -> tuple[str, ...]:
    """Return deduplicated image refs for services declaring build in compose JSON."""
    data = json.loads(compose_config_json)
    services = data.get("services") or {}
    seen: dict[str, None] = {}
    for svc in services.values():
        if not isinstance(svc, dict) or svc.get("build") is None:
            continue
        image = str(svc.get("image") or "").strip()
        if image and image not in seen:
            seen[image] = None
    return tuple(seen.keys())


def _extract_label_keys(raw_labels: object) -> set[str]:
    if isinstance(raw_labels, Mapping):
        return {str(key).strip() for key in raw_labels.keys() if str(key).strip()}
    if isinstance(raw_labels, list):
        keys: set[str] = set()
        for item in raw_labels:
            if not isinstance(item, str):
                continue
            key = item.split("=", 1)[0].strip()
            if key:
                keys.add(key)
        return keys
    return set()


def _check_service_labels(service_name: str, raw_service: object) -> tuple[list[str], list[str]]:
    if not isinstance(raw_service, Mapping):
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    root_keys = _extract_label_keys(raw_service.get("labels"))
    deploy = raw_service.get("deploy")
    deploy_keys = _extract_label_keys(deploy.get("labels")) if isinstance(deploy, Mapping) else set()

    if "com.docker.compose.project" in deploy_keys:
        errors.append(
            f"Service '{service_name}' places 'com.docker.compose.project' under deploy.labels; "
            "move it to service root labels."
        )
    misplaced = sorted(
        key for key in root_keys if key.startswith("traefik.") or key.startswith("homepage.")
    )
    if misplaced:
        joined = ", ".join(misplaced)
        errors.append(
            f"Service '{service_name}' places {joined} under service root labels; "
            "move routing/dashboard labels to deploy.labels."
        )
    if "com.docker.compose.project" not in root_keys:
        warnings.append(
            f"Warning: Service '{service_name}' is missing 'com.docker.compose.project' under service root labels."
        )
    return errors, warnings


def validate_swarm_labels(compose_config_json: str) -> tuple[str, ...]:
    """Validate Swarm label placement in resolved `docker compose config --format json` output."""
    if not compose_config_json or not compose_config_json.strip():
        raise DeployConfigError("Compose config JSON is empty.")
    try:
        data = json.loads(compose_config_json)
    except json.JSONDecodeError as exc:
        raise DeployConfigError(f"Invalid compose config JSON: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise DeployConfigError("Compose config JSON must contain a 'services' object.")

    errors: list[str] = []
    warnings: list[str] = []
    for service_name, raw_service in data["services"].items():
        svc_errors, svc_warnings = _check_service_labels(str(service_name), raw_service)
        errors.extend(svc_errors)
        warnings.extend(svc_warnings)

    if errors:
        raise DeployConfigError("\n".join(errors))
    return tuple(warnings)


def remove_stack(
    stack_name: str,
    docker: DockerPort,
    clock: Clock,
    *,
    out: TextIO | None = None,
) -> None:
    """Remove a Swarm stack and wait for services, containers, and network teardown."""
    out_stream = sys.stdout if out is None else out
    out_stream.write(f"Removing existing {stack_name} stack...\n")
    docker.run(["stack", "rm", stack_name], check=False)

    _wait_for_teardown_step(
        banner="Waiting for stack to be removed...",
        active_message="Stack services still active, waiting...",
        args=("service", "ls"),
        is_active=lambda stdout: f"{stack_name}_" in stdout,
        docker=docker,
        clock=clock,
        out_stream=out_stream,
    )
    _wait_for_teardown_step(
        banner="Waiting for stack containers to stop...",
        active_message="Stack containers still stopping, waiting...",
        args=("ps", "-q", "--filter", f"label=com.docker.stack.namespace={stack_name}"),
        is_active=lambda stdout: bool(stdout.strip()),
        docker=docker,
        clock=clock,
        out_stream=out_stream,
    )
    _wait_for_teardown_step(
        banner="Waiting for network to be removed...",
        active_message="Stack network still active, waiting...",
        args=("network", "ls"),
        is_active=lambda stdout: f"{stack_name}_internal" in stdout,
        docker=docker,
        clock=clock,
        out_stream=out_stream,
    )


def deploy_stack(
    config: DeployConfig,
    docker: DockerPort,
    clock: Clock,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> DeployResult:
    """Build/push compose images, deploy Swarm stack with recreate fallback, prune secrets."""
    out_stream = sys.stdout if out is None else out
    err_stream = sys.stderr if err is None else err
    child_env = _build_child_env(config, env=env, out_stream=out_stream)

    compose_str = " ".join(str(path) for path in config.compose_files)
    out_stream.write(f"Starting deployment for stack: {config.stack_name}\n")
    out_stream.write(f"Using compose files: {compose_str}\n")

    compose_json = _resolve_and_validate_compose(config, docker, child_env, err_stream)
    pushed = _run_build_and_push(config, docker, clock, child_env, compose_json, out_stream, err_stream)
    recreated = _run_stack_deploy(config, docker, clock, child_env, out_stream, err_stream)

    out_stream.write(
        f"Deployment command submitted. Check status with: docker stack services {config.stack_name}\n"
    )
    out_stream.write("Pruning unused versioned Swarm secrets...\n")
    pruned = prune_secrets(docker, base=None, out=out_stream)
    return DeployResult(
        stack_name=config.stack_name,
        pushed_images=pushed,
        recreated=recreated,
        pruned_secrets=pruned,
    )


def _parse_env_value(raw_val: str) -> str:
    if len(raw_val) >= 2 and raw_val[0] == raw_val[-1] and raw_val[0] in ("'", '"'):
        return raw_val[1:-1]
    return re.sub(r"\s+#.*$", "", raw_val).strip()


def _build_child_env(
    config: DeployConfig,
    *,
    env: Mapping[str, str] | None,
    out_stream: TextIO,
) -> dict[str, str]:
    child_env: dict[str, str] = dict(os.environ if env is None else env)
    dotenv_path = config.work_dir / ".env"
    if dotenv_path.is_file():
        out_stream.write("Loading environment variables from .env...\n")
        child_env.update(load_dotenv(dotenv_path))
    child_env["STACK_NAME"] = config.stack_name
    child_env["COMPOSE_PROJECT_NAME"] = config.stack_name
    return child_env


def _resolve_and_validate_compose(
    config: DeployConfig,
    docker: DockerPort,
    child_env: Mapping[str, str],
    err_stream: TextIO,
) -> str:
    compose_flags = [arg for path in config.compose_files for arg in ("-f", str(path))]
    cfg_res = _run_or_raise(
        ["compose", *compose_flags, "config", "--format", "json"],
        docker,
        child_env,
        config.work_dir,
        out_stream=None,
        err_stream=err_stream,
    )
    for warning in validate_swarm_labels(cfg_res.stdout):
        err_stream.write(f"{warning}\n")
    return cfg_res.stdout


def _run_build_and_push(
    config: DeployConfig,
    docker: DockerPort,
    clock: Clock,
    child_env: Mapping[str, str],
    compose_json: str,
    out_stream: TextIO,
    err_stream: TextIO,
) -> tuple[str, ...]:
    if config.skip_build:
        out_stream.write("Skipping build step...\n")
        return ()

    build_flags = [arg for path in config.compose_files for arg in ("-f", str(path))]
    out_stream.write("Building images...\n")
    _run_or_raise(["compose", *build_flags, "build", "--pull"], docker, child_env, config.work_dir, out_stream, err_stream)

    if not child_env.get("REGISTRY_PREFIX"):
        return ()

    out_stream.write("Resolving built images to push...\n")
    images = extract_built_images(compose_json)
    if not images:
        out_stream.write("No built images to push.\n")
        return ()

    for image_ref in images:
        _push_image_with_retry(image_ref, docker, clock, child_env, out_stream, err_stream)
    return images


def _push_image_with_retry(
    image_ref: str,
    docker: DockerPort,
    clock: Clock,
    child_env: Mapping[str, str],
    out_stream: TextIO,
    err_stream: TextIO,
) -> None:
    for attempt in range(1, MAX_PUSH_ATTEMPTS + 1):
        out_stream.write(f"Pushing {image_ref} (attempt {attempt}/{MAX_PUSH_ATTEMPTS})...\n")
        res = docker.run(["push", image_ref], env=child_env, check=False)
        _emit_result(res, out_stream, err_stream)
        if res.returncode == 0:
            return
        if attempt >= MAX_PUSH_ATTEMPTS:
            err_stream.write(f"Error: Failed to push {image_ref} after {MAX_PUSH_ATTEMPTS} attempts.\n")
            raise ReportedCommandError(
                argv=("docker", "push", image_ref),
                returncode=res.returncode or 1,
                stdout=res.stdout,
                stderr=res.stderr,
            )
        backoff = attempt * 5
        err_stream.write(f"Warning: Push failed for {image_ref}; retrying in {backoff}s...\n")
        clock.sleep(backoff)


def _run_stack_deploy(
    config: DeployConfig,
    docker: DockerPort,
    clock: Clock,
    child_env: Mapping[str, str],
    out_stream: TextIO,
    err_stream: TextIO,
) -> bool:
    deploy_flags = [arg for path in config.compose_files for arg in ("-c", str(path))]
    deploy_args = [
        "stack",
        "deploy",
        "--with-registry-auth",
        "--prune",
        "--resolve-image",
        "always",
        *deploy_flags,
        config.stack_name,
    ]
    if config.recreate:
        remove_stack(config.stack_name, docker, clock, out=out_stream)
        out_stream.write(f"Deploying {config.stack_name} stack to Swarm...\n")
        _run_or_raise(deploy_args, docker, child_env, config.work_dir, out_stream, err_stream)
        return True

    out_stream.write(f"Deploying {config.stack_name} stack to Swarm (in-place rolling update)...\n")
    res = docker.run(deploy_args, env=child_env, cwd=config.work_dir, check=False)
    if res.returncode == 0:
        _emit_result(res, out_stream, err_stream)
        return False

    combined = _combine_output(res)
    if combined:
        err_stream.write(f"{combined}\n")
    if not IMMUTABLE_DEPLOY_ERROR_RE.search(combined):
        raise ReportedCommandError(
            argv=("docker", *deploy_args),
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
        )

    err_stream.write(
        "Warning: In-place deploy rejected due to immutable Swarm config/spec change; "
        f"falling back to stack recreate for {config.stack_name}...\n"
    )
    remove_stack(config.stack_name, docker, clock, out=out_stream)
    out_stream.write(f"Retrying {config.stack_name} stack deploy after recreate...\n")
    _run_or_raise(deploy_args, docker, child_env, config.work_dir, out_stream, err_stream)
    return True


def _wait_for_teardown_step(
    *,
    banner: str,
    active_message: str,
    args: Sequence[str],
    is_active: Callable[[str], bool],
    docker: DockerPort,
    clock: Clock,
    out_stream: TextIO,
) -> None:
    out_stream.write(f"{banner}\n")
    for step in range(MAX_TEARDOWN_POLLS):
        res = docker.run(args, timeout=TEARDOWN_CMD_TIMEOUT, check=False)
        if res.returncode != 0 or not is_active(res.stdout):
            return
        out_stream.write(f"{active_message} ({step + 1}/{MAX_TEARDOWN_POLLS})\n")
        clock.sleep(TEARDOWN_POLL_INTERVAL)


def _run_or_raise(
    args: Sequence[str],
    docker: DockerPort,
    child_env: Mapping[str, str],
    work_dir: Path,
    out_stream: TextIO | None,
    err_stream: TextIO,
) -> CommandResult:
    res = docker.run(args, env=child_env, cwd=work_dir, check=False)
    _emit_result(res, out_stream, err_stream)
    if res.returncode != 0:
        raise ReportedCommandError(
            argv=("docker", *args),
            returncode=res.returncode,
            stdout=res.stdout,
            stderr=res.stderr,
        )
    return res


def _emit_result(res: CommandResult, out_stream: TextIO | None, err_stream: TextIO) -> None:
    if out_stream is not None and res.stdout:
        out_stream.write(res.stdout)
        if not res.stdout.endswith("\n"):
            out_stream.write("\n")
    if res.stderr:
        err_stream.write(res.stderr)
        if not res.stderr.endswith("\n"):
            err_stream.write("\n")


def _combine_output(res: CommandResult) -> str:
    parts = [part.rstrip("\n") for part in (res.stdout, res.stderr) if part.strip()]
    return "\n".join(parts)

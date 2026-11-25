#!/usr/bin/env python3
"""Direct seam and zero-clone shim tests for deploy, secrets, and lifecycle."""

from __future__ import annotations

import contextlib
import hashlib
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
    CommandExecutionError,
    CommandResult,
    DeployConfig,
    DeployConfigError,
    ReportedCommandError,
    SecretConfigError,
    cli,
    compute_secret_name,
    deploy_stack,
    ensure_secret,
    extract_built_images,
    load_dotenv,
    parse_deploy_args,
    prune_secrets,
    remove_stack,
)

DEPLOY_SHIMS = (
    (REPO_ROOT / "deploy" / "deploy.sh", ["deploy"]),
    (REPO_ROOT / "deploy" / "ensure_secret.sh", ["ensure-secret"]),
    (REPO_ROOT / "deploy" / "prune_secrets.sh", ["prune-secrets"]),
    (REPO_ROOT / "deploy" / "verify_convergence.sh", ["verify-convergence"]),
)


class FakeClock:
    """Deterministic monotonic clock recording sleep durations."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeDockerPort:
    """In-memory DockerPort recording args, stdin, env, and returning queued results."""

    def __init__(
        self,
        handler: Callable[[tuple[str, ...], str | None, Mapping[str, str] | None], CommandResult]
        | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
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
        self.calls.append(
            {
                "args": cmd,
                "stdin_text": stdin_text,
                "timeout": timeout,
                "env": dict(env) if env is not None else None,
                "cwd": cwd,
            }
        )
        result = self._handler(cmd, stdin_text, env) if self._handler else CommandResult(0, "", "")
        if check and result.returncode != 0:
            raise CommandExecutionError(
                argv=("docker", *cmd),
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return result


class TestSecretsLifecycle(unittest.TestCase):
    def test_compute_secret_name_strips_trailing_newlines_for_echo_and_printf_parity(self) -> None:
        name_printf = compute_secret_name("cerberus_token", "super_secret")
        name_echo = compute_secret_name("cerberus_token", "super_secret\n")
        name_multi_nl = compute_secret_name("cerberus_token", "super_secret\n\n\n")
        self.assertEqual(name_printf, name_echo)
        self.assertEqual(name_printf, name_multi_nl)
        self.assertRegex(name_printf, r"^cerberus_token_[0-9a-f]{8}$")
        expected_hash = hashlib.md5(b"super_secret").hexdigest()[:8]
        self.assertEqual(name_printf, f"cerberus_token_{expected_hash}")

    def test_compute_secret_name_fails_loud_on_empty_base_or_payload(self) -> None:
        with self.assertRaises(SecretConfigError):
            compute_secret_name("", "secret")
        with self.assertRaises(SecretConfigError):
            compute_secret_name("   ", "secret")
        with self.assertRaises(SecretConfigError):
            compute_secret_name("base", "")
        with self.assertRaises(SecretConfigError):
            compute_secret_name("base", "\n\n")

    def test_ensure_secret_idempotent_skip_and_create_with_stripped_stdin(self) -> None:
        existing_docker = FakeDockerPort(lambda cmd, _s, _e: CommandResult(0, "[{}]", ""))
        full_existing = ensure_secret("db_pass", "alpha\n", existing_docker)
        self.assertEqual(full_existing, compute_secret_name("db_pass", "alpha"))
        self.assertEqual([c["args"] for c in existing_docker.calls], [("secret", "inspect", full_existing)])

        def create_handler(cmd: tuple[str, ...], _s: str | None, _e: Mapping[str, str] | None) -> CommandResult:
            if cmd[:2] == ("secret", "inspect"):
                return CommandResult(1, "", "No such secret")
            if cmd[:2] == ("secret", "create"):
                return CommandResult(0, "secret_id_ignored_on_stdout\n", "")
            return CommandResult(0, "", "")

        create_docker = FakeDockerPort(create_handler)
        full_created = ensure_secret("db_pass", "alpha\n", create_docker)
        self.assertEqual(full_created, full_existing)
        self.assertEqual(
            [(c["args"], c["stdin_text"]) for c in create_docker.calls],
            [
                (("secret", "inspect", full_created), None),
                (("secret", "create", full_created, "-"), "alpha"),
            ],
        )

    def test_prune_secrets_prevents_prefix_collision_and_substring_false_keep(self) -> None:
        secrets_list = "\n".join(
            [
                "db_pass_11111111",
                "db_pass_22222222",
                "db_pass_extra_33333333",
                "db_pass_unversioned",
                "other_44444444",
            ]
        )
        # Active secrets contain a longer name ending similarly and db_pass_11111111
        used_output = "prefix_db_pass_22222222 db_pass_11111111 "

        def handler(cmd: tuple[str, ...], _s: str | None, _e: Mapping[str, str] | None) -> CommandResult:
            if cmd[:2] == ("secret", "ls"):
                return CommandResult(0, secrets_list + "\n", "")
            if cmd[:2] == ("service", "ls"):
                return CommandResult(0, "svc1\nsvc2\n", "")
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(0, used_output, "")
            if cmd[:2] == ("secret", "rm"):
                if cmd[2] == "db_pass_22222222":
                    return CommandResult(0, "", "")
                return CommandResult(1, "", "failed")
            return CommandResult(0, "", "")

        docker = FakeDockerPort(handler)
        out = io.StringIO()
        removed = prune_secrets(docker, base="db_pass", out=out)

        self.assertEqual(removed, ("db_pass_22222222",))
        rm_targets = [c["args"][2] for c in docker.calls if c["args"][:2] == ("secret", "rm")]
        self.assertEqual(rm_targets, ["db_pass_22222222"])
        self.assertIn("[KEEP] db_pass_11111111 (In use)", out.getvalue())
        self.assertIn("[DELETE] db_pass_22222222", out.getvalue())
        self.assertNotIn("db_pass_extra_33333333", out.getvalue())
        self.assertNotIn("db_pass_unversioned", out.getvalue())

    def test_prune_secrets_handles_zero_candidates_empty_services_and_rm_failure(self) -> None:
        # 1. Zero matching secrets exits cleanly
        docker_empty = FakeDockerPort(lambda cmd, _s, _e: CommandResult(0, "unrelated_secret\n", ""))
        out_empty = io.StringIO()
        self.assertEqual(prune_secrets(docker_empty, base="missing_base", out=out_empty), ())
        self.assertIn("No secrets found with prefix missing_base_", out_empty.getvalue())
        self.assertEqual([c["args"] for c in docker_empty.calls], [("secret", "ls", "--format", "{{.Name}}")])

        # 2. Empty docker service ls -q skips service inspect; rm error is non-fatal
        def handler_no_svcs(cmd: tuple[str, ...], _s: str | None, _e: Mapping[str, str] | None) -> CommandResult:
            if cmd[:2] == ("secret", "ls"):
                return CommandResult(0, "app_a_aaaaaaaa\napp_b_bbbbbbbb\n", "")
            if cmd[:2] == ("service", "ls"):
                return CommandResult(0, "", "")
            if cmd[:2] == ("secret", "rm"):
                return CommandResult(1, "", "busy") if cmd[2] == "app_a_aaaaaaaa" else CommandResult(0, "", "")
            return CommandResult(0, "", "")

        docker_no_svcs = FakeDockerPort(handler_no_svcs)
        out_global = io.StringIO()
        removed = prune_secrets(docker_no_svcs, base=None, out=out_global)
        self.assertEqual(removed, ("app_b_bbbbbbbb",))
        self.assertFalse(any(c["args"][:2] == ("service", "inspect") for c in docker_no_svcs.calls))
        self.assertIn("Failed to delete app_a_aaaaaaaa", out_global.getvalue())


class TestDeployLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-deploy-lifecycle-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dotenv_parsing_and_child_env_propagation(self) -> None:
        dotenv = self.tmpdir / ".env"
        dotenv.write_text(
            "# comment\n"
            "\n"
            "export DOMAIN_NAME=\"example.test\"\n"
            "SINGLE_QUOTED='val with spaces'\n"
            "INLINE_COMMENT=abc123 # trailing note\n",
            encoding="utf-8",
        )
        parsed = load_dotenv(dotenv)
        self.assertEqual(
            parsed,
            {
                "DOMAIN_NAME": "example.test",
                "SINGLE_QUOTED": "val with spaces",
                "INLINE_COMMENT": "abc123",
            },
        )

        docker = FakeDockerPort()
        clock = FakeClock()
        cfg = DeployConfig(stack_name="mystack", skip_build=True, work_dir=self.tmpdir)
        out = io.StringIO()
        deploy_stack(cfg, docker, clock, env={"IMAGE_TAG": "v1.2.3"}, out=out, err=io.StringIO())

        self.assertIn("Loading environment variables from .env...", out.getvalue())
        stack_call = next(c for c in docker.calls if c["args"][:2] == ("stack", "deploy"))
        child_env = stack_call["env"]
        assert isinstance(child_env, dict)
        self.assertEqual(child_env["STACK_NAME"], "mystack")
        self.assertEqual(child_env["COMPOSE_PROJECT_NAME"], "mystack")
        self.assertEqual(child_env["IMAGE_TAG"], "v1.2.3")
        self.assertEqual(child_env["DOMAIN_NAME"], "example.test")

    def test_parse_deploy_args_interleaved_flags_and_missing_stack_error(self) -> None:
        cfg = parse_deploy_args(["mystack", "--recreate", "a.yml", "--skip-build", "b.yml"])
        assert cfg is not None
        self.assertEqual(cfg.stack_name, "mystack")
        self.assertEqual(cfg.compose_files, (Path("a.yml"), Path("b.yml")))
        self.assertTrue(cfg.recreate)
        self.assertTrue(cfg.skip_build)

        default_cfg = parse_deploy_args(["solo"])
        assert default_cfg is not None
        self.assertEqual(default_cfg.compose_files, (Path("docker-compose.yml"),))

        with self.assertRaises(DeployConfigError):
            parse_deploy_args(["--skip-build"])

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["deploy"]), 1)

    def test_remove_stack_polls_services_containers_and_network_with_clock(self) -> None:
        counts = {"service": 0, "ps": 0, "network": 0}

        def handler(cmd: tuple[str, ...], _s: str | None, _e: Mapping[str, str] | None) -> CommandResult:
            if cmd[:2] == ("service", "ls"):
                counts["service"] += 1
                return CommandResult(0, "mystack_api 1/1\n" if counts["service"] == 1 else "", "")
            if cmd[0] == "ps":
                counts["ps"] += 1
                return CommandResult(0, "cid1\n" if counts["ps"] == 1 else "", "")
            if cmd[:2] == ("network", "ls"):
                counts["network"] += 1
                return CommandResult(0, "mystack_internal\n" if counts["network"] == 1 else "", "")
            return CommandResult(0, "", "")

        docker = FakeDockerPort(handler)
        clock = FakeClock()
        out = io.StringIO()
        remove_stack("mystack", docker, clock, out=out)

        self.assertEqual(clock.sleeps, [2.0, 2.0, 2.0])
        self.assertIn("Stack services still active, waiting... (1/35)", out.getvalue())
        self.assertIn("Stack containers still stopping, waiting... (1/35)", out.getvalue())
        self.assertIn("Stack network still active, waiting... (1/35)", out.getvalue())

    def test_extract_built_images_and_global_prune_after_deploy(self) -> None:
        compose_json = json.dumps(
            {
                "services": {
                    "db": {"image": "postgres:16"},
                    "a": {"image": "reg/app:1", "build": {"context": "."}},
                    "b": {"image": "reg/app:1", "build": {"context": "."}},
                    "c": {"image": "reg/worker:1", "build": {"context": "./w"}},
                }
            }
        )
        self.assertEqual(extract_built_images(compose_json), ("reg/app:1", "reg/worker:1"))

        def handler(cmd: tuple[str, ...], _s: str | None, _e: Mapping[str, str] | None) -> CommandResult:
            if cmd[:2] == ("compose", "-f") and "config" in cmd:
                return CommandResult(0, compose_json, "")
            if cmd[:2] == ("secret", "ls"):
                return CommandResult(0, "old_secret_1234abcd\nactive_secret_9999aaaa\n", "")
            if cmd[:2] == ("service", "ls"):
                return CommandResult(0, "svc1\n", "")
            if cmd[:2] == ("service", "inspect"):
                return CommandResult(0, "active_secret_9999aaaa\n", "")
            return CommandResult(0, "", "")

        docker = FakeDockerPort(handler)
        clock = FakeClock()
        out = io.StringIO()
        result = deploy_stack(
            DeployConfig(stack_name="mystack"),
            docker,
            clock,
            env={"REGISTRY_PREFIX": "reg/"},
            out=out,
            err=io.StringIO(),
        )
        self.assertEqual(result.pushed_images, ("reg/app:1", "reg/worker:1"))
        self.assertEqual(result.pruned_secrets, ("old_secret_1234abcd",))
        self.assertEqual(
            [c["args"] for c in docker.calls if c["args"][:2] == ("secret", "rm")],
            [("secret", "rm", "old_secret_1234abcd")],
        )

    def test_cli_propagates_reported_exit_code_on_build_failure(self) -> None:
        docker = FakeDockerPort(lambda cmd, _s, _e: CommandResult(4, "", "build failed") if "build" in cmd else CommandResult(0, "", ""))
        clock = FakeClock()
        err = io.StringIO()
        with self.assertRaises(ReportedCommandError) as ctx:
            deploy_stack(DeployConfig(stack_name="mystack"), docker, clock, env={}, out=io.StringIO(), err=err)
        self.assertEqual(ctx.exception.returncode, 4)
        self.assertIn("build failed", err.getvalue())


class TestDeployZeroCloneShims(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-deploy-shims-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.archive_tmp_parent = self.tmpdir / "shim_tmp"
        self.archive_tmp_parent.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_all_deploy_shims_executable_valid_bash_and_resolve_subcommands(self) -> None:
        py_log = self.tmpdir / "python_calls.jsonl"
        mock_python = self.mock_bin / "python3"
        mock_python.write_text(
            f"#!{sys.executable}\n"
            "import importlib, json, os, sys\n"
            f"log_path = {str(py_log)!r}\n"
            "mod = importlib.import_module('yggdrasil_os')\n"
            "stdin_val = sys.stdin.read() if os.environ.get('READ_STDIN') == '1' else ''\n"
            "entry = {'argv': sys.argv[1:], 'module_file': mod.__file__, 'pythonpath': os.environ.get('PYTHONPATH', ''), 'stdin': stdin_val}\n"
            "with open(log_path, 'a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps(entry) + '\\n')\n"
            "sys.exit(int(os.environ.get('MOCK_PY_EXIT', '0')))\n",
            encoding="utf-8",
        )
        mock_python.chmod(0o755)

        for script, expected_subcmd in DEPLOY_SHIMS:
            with self.subTest(script=script.name):
                mode = stat.S_IMODE(script.stat().st_mode)
                self.assertEqual(mode & 0o111, 0o111, f"{script} must be executable")
                self.assertEqual(mode & 0o002, 0, f"{script} must not be world-writable")
                syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
                self.assertEqual(syntax.returncode, 0, syntax.stderr)

                py_log.unlink(missing_ok=True)
                env = {"PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin"}
                if script.name == "ensure_secret.sh":
                    env["READ_STDIN"] = "1"
                res = subprocess.run(
                    ["bash", str(script), "arg1"],
                    env=env,
                    input="payload\n" if script.name == "ensure_secret.sh" else "",
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                entry = json.loads(py_log.read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual(entry["argv"], ["-m", "yggdrasil_os", *expected_subcmd, "arg1"])
                self.assertEqual(Path(entry["module_file"]).resolve(), (REPO_ROOT / "yggdrasil_os" / "__init__.py").resolve())
                if script.name == "ensure_secret.sh":
                    self.assertEqual(entry["stdin"], "payload\n")

    def test_ensure_secret_shim_archive_passthrough_and_stdout_purity(self) -> None:
        archive_path = self.tmpdir / "yggdrasil-os-main.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(REPO_ROOT / "yggdrasil_os", arcname="yggdrasil-os-main/yggdrasil_os")

        mock_docker = self.mock_bin / "docker"
        created_stdin_log = self.tmpdir / "docker_secret_stdin.txt"
        mock_docker.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['secret', 'inspect']:\n"
            "    sys.exit(1)\n"
            "if args[:2] == ['secret', 'create']:\n"
            f"    with open({str(created_stdin_log)!r}, 'w', encoding='utf-8') as fh:\n"
            "        fh.write(sys.stdin.read())\n"
            "    print('noisy_docker_secret_id')\n"
            "    sys.exit(0)\n"
            "sys.exit(99)\n",
            encoding="utf-8",
        )
        mock_docker.chmod(0o755)

        detached_shim = self.tmpdir / "ensure_secret.sh"
        shutil.copy2(REPO_ROOT / "deploy" / "ensure_secret.sh", detached_shim)
        detached_shim.chmod(0o755)

        res = subprocess.run(
            ["bash", str(detached_shim), "my_secret"],
            input="super_secret\n",
            env={
                "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                "TMPDIR": str(self.archive_tmp_parent),
                "YGGDRASIL_OS_ARCHIVE_URL": archive_path.as_uri(),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        expected_name = compute_secret_name("my_secret", "super_secret")
        self.assertEqual(res.stdout, f"{expected_name}\n")
        self.assertEqual(res.stderr, "")
        self.assertEqual(created_stdin_log.read_text(encoding="utf-8"), "super_secret")
        self.assertEqual(list(self.archive_tmp_parent.iterdir()), [])

    def test_shim_root_override_invalid_fails_loud_and_tarball_cleans_on_nonzero(self) -> None:
        bad_root = self.tmpdir / "bad_root"
        bad_root.mkdir()
        res_bad = subprocess.run(
            ["bash"],
            input=(REPO_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8"),
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "YGGDRASIL_OS_ROOT": str(bad_root)},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_bad.returncode, 1)
        self.assertIn("does not contain yggdrasil_os/__init__.py", res_bad.stderr)

        archive_path = self.tmpdir / "yggdrasil-os-main.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(REPO_ROOT / "yggdrasil_os", arcname="yggdrasil-os-main/yggdrasil_os")

        mock_python = self.mock_bin / "python3"
        mock_python.write_text("#!/usr/bin/env bash\nexit 42\n", encoding="utf-8")
        mock_python.chmod(0o755)

        detached = self.tmpdir / "prune_secrets.sh"
        shutil.copy2(REPO_ROOT / "deploy" / "prune_secrets.sh", detached)
        detached.chmod(0o755)
        res_fail = subprocess.run(
            ["bash", str(detached), "tok"],
            env={
                "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
                "TMPDIR": str(self.archive_tmp_parent),
                "YGGDRASIL_OS_ARCHIVE_URL": archive_path.as_uri(),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res_fail.returncode, 42)
        self.assertEqual(list(self.archive_tmp_parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

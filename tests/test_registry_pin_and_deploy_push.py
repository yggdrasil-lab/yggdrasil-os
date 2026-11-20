#!/usr/bin/env python3
"""Unit and integration tests for registry host pinning and deploy push dedup."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PIN_SCRIPT = REPO_ROOT / "network" / "pin_registry_host.sh"
DEPLOY_SCRIPT = REPO_ROOT / "deploy" / "deploy.sh"
SETUP_GAIA_SCRIPT = REPO_ROOT / "hosts" / "setup_gaia.sh"
SETUP_MUSPELHEIM_SCRIPT = REPO_ROOT / "hosts" / "setup_muspelheim.sh"
SWARM_EXEC_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "swarm-exec.sh"
RESTART_DOCKER_SCRIPT = REPO_ROOT / "docker" / "restart_docker.sh"
MUSPELHEIM_RESTART_DOCKER_SCRIPT = REPO_ROOT / "hosts" / "scripts" / "muspelheim-restart-docker.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _find_odin_compose() -> Path | None:
    candidates = [
        REPO_ROOT.parent / "odin" / "docker-compose.yml",
        Path("/opt/atlas/vault/Forge/odin/docker-compose.yml"),
        Path("/app/vault/Forge/odin/docker-compose.yml"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


class TestBashSyntax(unittest.TestCase):
    def test_bash_syntax_valid(self) -> None:
        scripts = [
            PIN_SCRIPT,
            SETUP_GAIA_SCRIPT,
            SETUP_MUSPELHEIM_SCRIPT,
            DEPLOY_SCRIPT,
            SWARM_EXEC_SCRIPT,
            RESTART_DOCKER_SCRIPT,
            MUSPELHEIM_RESTART_DOCKER_SCRIPT,
        ]
        for script in scripts:
            with self.subTest(script=script.name):
                res = subprocess.run(
                    ["bash", "-n", str(script)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"bash -n failed for {script}: {res.stderr}",
                )

    def test_zero_clone_stdin_script_dir_resolution(self) -> None:
        # Simulates `curl ... | bash` where BASH_SOURCE[0] is unset under set -e
        snippet = (
            "set -e\n"
            "SCRIPT_DIR=\"\"\n"
            "if [ -n \"${BASH_SOURCE[0]:-}\" ] && [ -f \"${BASH_SOURCE[0]}\" ]; then\n"
            "    SCRIPT_DIR=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")\" >/dev/null 2>&1 && pwd)\"\n"
            "fi\n"
            "printf 'SCRIPT_DIR=[%s]\\n' \"$SCRIPT_DIR\"\n"
        )
        res = subprocess.run(
            ["bash"],
            input=snippet,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "SCRIPT_DIR=[]")


class TestPinRegistryHost(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-pin-registry-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.hosts_file = self.tmpdir / "hosts"
        self.hosts_file.write_text(
            "127.0.0.1\tlocalhost\n"
            "127.0.1.1\tgaia.example.test\tgaia\n"
            "\n"
            "# IPv6 hosts\n"
            "::1     localhost ip6-localhost ip6-loopback\n",
            encoding="utf-8",
        )
        self.hosts_file.chmod(0o644)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _base_env(self, include_mock_bin: bool = True) -> dict[str, str]:
        env = {
            "PATH": (
                f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin"
                if include_mock_bin
                else "/usr/local/bin:/usr/bin:/bin"
            ),
            "HOSTS_FILE": str(self.hosts_file),
            "HOME": str(self.tmpdir),
        }
        return env

    def _run_pin(self, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = self._base_env()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(PIN_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_fresh_append_idempotent_and_ip_update(self) -> None:
        # Fresh append with explicit domain and LAN IP
        res1 = self._run_pin(
            {
                "DOMAIN_NAME": "example.test",
                "REGISTRY_LAN_IP": "192.0.2.10",
            }
        )
        self.assertEqual(res1.returncode, 0, res1.stderr)
        content1 = self.hosts_file.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1\tlocalhost\n", content1)
        self.assertIn("127.0.1.1\tgaia.example.test\tgaia\n", content1)
        self.assertIn("::1     localhost ip6-localhost ip6-loopback\n", content1)
        self.assertEqual(
            content1.count("192.0.2.10 registry.example.test # yggdrasil-registry-pin"),
            1,
        )
        self.assertEqual(stat.S_IMODE(self.hosts_file.stat().st_mode), 0o644)

        # Idempotent re-run produces identical content and no duplicate lines
        res2 = self._run_pin(
            {
                "DOMAIN_NAME": "example.test",
                "REGISTRY_LAN_IP": "192.0.2.10",
            }
        )
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(self.hosts_file.read_text(encoding="utf-8"), content1)

        # Updating REGISTRY_LAN_IP replaces the previous pin line in place
        res3 = self._run_pin(
            {
                "DOMAIN_NAME": "example.test",
                "REGISTRY_LAN_IP": "192.0.2.20",
            }
        )
        self.assertEqual(res3.returncode, 0, res3.stderr)
        content3 = self.hosts_file.read_text(encoding="utf-8")
        self.assertNotIn("192.0.2.10", content3)
        self.assertEqual(
            content3.count("192.0.2.20 registry.example.test # yggdrasil-registry-pin"),
            1,
        )
        self.assertEqual(content3.count("# yggdrasil-registry-pin"), 1)
        self.assertEqual(stat.S_IMODE(self.hosts_file.stat().st_mode), 0o644)

    def test_replaces_existing_unpinned_entry_without_clobbering_shared_line(self) -> None:
        self.hosts_file.write_text(
            "127.0.0.1\tlocalhost\n"
            "198.51.100.5\tother.example.test\tregistry.example.test\n"
            "198.51.100.9\tregistry.example.test # stale manual entry\n",
            encoding="utf-8",
        )
        res = self._run_pin(
            {
                "REGISTRY_HOST": "registry.example.test",
                "REGISTRY_LAN_IP": "192.0.2.30",
            }
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        updated = self.hosts_file.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1\tlocalhost\n", updated)
        self.assertIn("198.51.100.5 other.example.test\n", updated)
        self.assertNotIn("198.51.100.9", updated)
        self.assertEqual(updated.count("registry.example.test"), 1)
        self.assertIn(
            "192.0.2.30 registry.example.test # yggdrasil-registry-pin\n",
            updated,
        )

    def test_manager_autodetect_via_ip_route_and_hostname_d(self) -> None:
        _write_executable(
            self.mock_bin / "hostname",
            "#!/usr/bin/env bash\n"
            "case \"${1:-}\" in\n"
            "  -d) echo 'auto.example.test' ;;\n"
            "  -f) echo 'gaia.auto.example.test' ;;\n"
            "  -I) echo '198.51.100.77' ;;\n"
            "  *) echo 'gaia' ;;\n"
            "esac\n",
        )
        _write_executable(
            self.mock_bin / "ip",
            "#!/usr/bin/env bash\n"
            "echo '1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.55 uid 1000'\n",
        )
        res = self._run_pin({"NODE_ROLE": "manager"})
        self.assertEqual(res.returncode, 0, res.stderr)
        content = self.hosts_file.read_text(encoding="utf-8")
        self.assertIn(
            "192.0.2.55 registry.auto.example.test # yggdrasil-registry-pin",
            content,
        )

    def test_manager_fallback_hostname_f_hosts_gaia_and_hostname_I(self) -> None:
        # hostname -d empty, hostname -f empty -> fallback to gaia.<domain> in HOSTS_FILE
        # ip route empty -> fallback to hostname -I
        _write_executable(
            self.mock_bin / "hostname",
            "#!/usr/bin/env bash\n"
            "case \"${1:-}\" in\n"
            "  -d) echo '' ;;\n"
            "  -f) echo 'gaia' ;;\n"
            "  -I) echo '192.0.2.88 172.17.0.1' ;;\n"
            "  *) echo 'gaia' ;;\n"
            "esac\n",
        )
        _write_executable(
            self.mock_bin / "ip",
            "#!/usr/bin/env bash\n"
            "exit 1\n",
        )
        res = self._run_pin({"NODE_ROLE": "manager"})
        self.assertEqual(res.returncode, 0, res.stderr)
        content = self.hosts_file.read_text(encoding="utf-8")
        self.assertIn(
            "192.0.2.88 registry.example.test # yggdrasil-registry-pin",
            content,
        )

    def test_worker_autodetect_via_docker_remote_managers(self) -> None:
        _write_executable(
            self.mock_bin / "hostname",
            "#!/usr/bin/env bash\n"
            "case \"${1:-}\" in\n"
            "  -d) echo '' ;;\n"
            "  -f) echo 'muspelheim.worker.example.test' ;;\n"
            "  *) echo 'muspelheim' ;;\n"
            "esac\n",
        )
        _write_executable(
            self.mock_bin / "docker",
            "#!/usr/bin/env bash\n"
            "if [ \"${1:-}\" = \"info\" ]; then\n"
            "  echo '192.0.2.42:2377'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
        )
        res = self._run_pin({"NODE_ROLE": "worker"})
        self.assertEqual(res.returncode, 0, res.stderr)
        content = self.hosts_file.read_text(encoding="utf-8")
        self.assertIn(
            "192.0.2.42 registry.worker.example.test # yggdrasil-registry-pin",
            content,
        )

    def test_fail_loud_when_hostname_or_ip_missing_or_invalid(self) -> None:
        # Remove gaia.<domain> from hosts_file so fallback cannot resolve domain
        original = "127.0.0.1\tlocalhost\n"
        self.hosts_file.write_text(original, encoding="utf-8")
        _write_executable(
            self.mock_bin / "hostname",
            "#!/usr/bin/env bash\n"
            "exit 0\n",
        )
        _write_executable(
            self.mock_bin / "ip",
            "#!/usr/bin/env bash\n"
            "exit 1\n",
        )
        _write_executable(
            self.mock_bin / "docker",
            "#!/usr/bin/env bash\n"
            "exit 1\n",
        )

        # Missing domain/hostname
        res_missing_host = self._run_pin({"REGISTRY_LAN_IP": "192.0.2.10"})
        self.assertNotEqual(res_missing_host.returncode, 0)
        self.assertIn("DOMAIN_NAME", res_missing_host.stderr)
        self.assertEqual(self.hosts_file.read_text(encoding="utf-8"), original)

        # Invalid hostname formats
        for bad_host in ["registry", "https://registry.example.test", "192.0.2.10", "bad host.test"]:
            with self.subTest(bad_host=bad_host):
                res = self._run_pin(
                    {"REGISTRY_HOST": bad_host, "REGISTRY_LAN_IP": "192.0.2.10"}
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("REGISTRY_HOST", res.stderr)
                self.assertEqual(self.hosts_file.read_text(encoding="utf-8"), original)

        # Missing IP on manager and worker
        for role in ["manager", "worker"]:
            with self.subTest(role=role):
                res = self._run_pin(
                    {"DOMAIN_NAME": "example.test", "NODE_ROLE": role}
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("REGISTRY_LAN_IP", res.stderr)
                self.assertEqual(self.hosts_file.read_text(encoding="utf-8"), original)

        # Invalid IPv4 formats
        for bad_ip in ["0.0.0.0", "256.0.0.1", "192.0.02.1", "not-an-ip", "192.0.2"]:
            with self.subTest(bad_ip=bad_ip):
                res = self._run_pin(
                    {"DOMAIN_NAME": "example.test", "REGISTRY_LAN_IP": bad_ip}
                )
                self.assertNotEqual(res.returncode, 0)
                self.assertIn("REGISTRY_LAN_IP", res.stderr)
                self.assertEqual(self.hosts_file.read_text(encoding="utf-8"), original)


class TestDeployPushDedupAndRetry(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-deploy-push-"))
        self.mock_bin = self.tmpdir / "bin"
        self.mock_bin.mkdir()
        self.work_dir = self.tmpdir / "stack"
        self.work_dir.mkdir()
        self.push_log = self.tmpdir / "push.log"
        self.sleep_log = self.tmpdir / "sleep.log"
        self.stack_env_log = self.tmpdir / "stack_env.log"
        self.stack_cmd_log = self.tmpdir / "stack_cmd.log"
        self.compose_json_file = self.tmpdir / "compose.json"
        self.fail_count_file = self.tmpdir / "fail_count.txt"
        self.stack_deploy_fail_count_file = self.tmpdir / "stack_deploy_fail_count.txt"
        self.stack_deploy_fail_msg_file = self.tmpdir / "stack_deploy_fail_msg.txt"
        self.stack_deploy_fail_rc_file = self.tmpdir / "stack_deploy_fail_rc.txt"

        _write_executable(
            self.mock_bin / "sleep",
            "#!/usr/bin/env bash\n"
            f"echo \"$*\" >> {self.sleep_log}\n",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _install_mock_docker(self, real_docker_for_config: bool = False) -> None:
        real_docker = shutil.which("docker") or "/usr/bin/docker"
        config_branch = (
            f"    exec \"{real_docker}\" \"$@\"\n"
            if real_docker_for_config
            else f"    cat {self.compose_json_file}\n    exit 0\n"
        )
        script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "cmd=\"${1:-}\"\n"
            "if [ \"$cmd\" = \"compose\" ]; then\n"
            "  for arg in \"$@\"; do\n"
            "    if [ \"$arg\" = \"config\" ]; then\n"
            f"{config_branch}"
            "    fi\n"
            "  done\n"
            "  exit 0\n"
            "elif [ \"$cmd\" = \"push\" ]; then\n"
            "  img=\"${2:-}\"\n"
            f"  echo \"$img\" >> {self.push_log}\n"
            f"  if [ -f {self.fail_count_file} ]; then\n"
            f"    rem=$(cat {self.fail_count_file})\n"
            "    if [ \"$rem\" -gt 0 ]; then\n"
            f"      echo $((rem - 1)) > {self.fail_count_file}\n"
            "      exit 1\n"
            "    fi\n"
            "  fi\n"
            "  exit 0\n"
            "elif [ \"$cmd\" = \"stack\" ]; then\n"
            "  shift\n"
            f"  echo \"$*\" >> {self.stack_cmd_log}\n"
            "  subcmd=\"${1:-}\"\n"
            "  if [ \"$subcmd\" = \"deploy\" ]; then\n"
            f"    echo \"IMAGE_TAG=${{IMAGE_TAG:-}}\" >> {self.stack_env_log}\n"
            f"    if [ -f {self.stack_deploy_fail_count_file} ]; then\n"
            f"      rem=$(cat {self.stack_deploy_fail_count_file})\n"
            "      if [ \"$rem\" -gt 0 ]; then\n"
            f"        echo $((rem - 1)) > {self.stack_deploy_fail_count_file}\n"
            f"        if [ -f {self.stack_deploy_fail_msg_file} ]; then\n"
            f"          cat {self.stack_deploy_fail_msg_file} >&2\n"
            "        fi\n"
            "        rc=1\n"
            f"        if [ -f {self.stack_deploy_fail_rc_file} ]; then\n"
            f"          rc=$(cat {self.stack_deploy_fail_rc_file})\n"
            "        fi\n"
            "        exit \"$rc\"\n"
            "      fi\n"
            "    fi\n"
            "    echo \"Updating service teststack_app\"\n"
            "  fi\n"
            "  exit 0\n"
            "elif [ \"$cmd\" = \"service\" ] || [ \"$cmd\" = \"ps\" ] || [ \"$cmd\" = \"network\" ] || [ \"$cmd\" = \"secret\" ]; then\n"
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        _write_executable(self.mock_bin / "docker", script)

    def _run_deploy(
        self,
        extra_env: dict[str, str] | None = None,
        compose_path: str = "docker-compose.yml",
        stack_name: str = "teststack",
        extra_args: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.mock_bin}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "REGISTRY_PREFIX": "registry.example.test/",
            "GIT_DIR": str(self.tmpdir / ".nonexistent-git"),
        }
        if extra_env:
            env.update(extra_env)
        cmd = ["bash", str(DEPLOY_SCRIPT), *(extra_args or []), stack_name, compose_path]
        return subprocess.run(
            cmd,
            cwd=str(self.work_dir),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_dedup_excludes_non_build_services_and_preserves_order(self) -> None:
        mock_compose = {
            "services": {
                "postgres": {"image": "postgres:16-alpine"},
                "agy-vision": {"image": "registry.example.test/hermes-agent:odin"},
                "huginn-gateway": {
                    "image": "registry.example.test/hermes-agent:odin",
                    "build": {"context": "./hermes-agent", "dockerfile": "Dockerfile"},
                },
                "huginn-dashboard": {
                    "image": "registry.example.test/hermes-agent:odin",
                    "build": {"context": "./hermes-agent", "dockerfile": "Dockerfile"},
                },
                "jev-shim": {
                    "image": "registry.example.test/jev-shim:odin",
                    "build": {"context": "./jev-shim", "dockerfile": "Dockerfile"},
                },
                "mnemosyne-dashboard": {
                    "image": "registry.example.test/mnemosyne-dashboard:odin",
                    "build": {"context": "./mnemosyne-dashboard"},
                },
            }
        }
        self.compose_json_file.write_text(json.dumps(mock_compose), encoding="utf-8")
        self._install_mock_docker()

        res = self._run_deploy()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        pushed = self.push_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            pushed,
            [
                "registry.example.test/hermes-agent:odin",
                "registry.example.test/jev-shim:odin",
                "registry.example.test/mnemosyne-dashboard:odin",
            ],
        )

    def test_zero_built_images_logs_cleanly_under_set_u(self) -> None:
        mock_compose = {
            "services": {
                "registry": {"image": "registry:2"},
                "postgres": {"image": "postgres:16-alpine"},
            }
        }
        self.compose_json_file.write_text(json.dumps(mock_compose), encoding="utf-8")
        self._install_mock_docker()

        res = self._run_deploy()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        self.assertIn("No built images to push.", res.stdout)
        self.assertFalse(self.push_log.exists())

    def test_push_retry_fails_twice_then_succeeds_on_third(self) -> None:
        mock_compose = {
            "services": {
                "app": {
                    "image": "registry.example.test/app:latest",
                    "build": {"context": "."},
                }
            }
        }
        self.compose_json_file.write_text(json.dumps(mock_compose), encoding="utf-8")
        self.fail_count_file.write_text("2", encoding="utf-8")
        self._install_mock_docker()

        res = self._run_deploy()
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        pushed = self.push_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            pushed,
            [
                "registry.example.test/app:latest",
                "registry.example.test/app:latest",
                "registry.example.test/app:latest",
            ],
        )
        sleeps = self.sleep_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sleeps, ["5", "10"])

    def test_push_retry_exhausted_exits_nonzero(self) -> None:
        mock_compose = {
            "services": {
                "app": {
                    "image": "registry.example.test/app:latest",
                    "build": {"context": "."},
                }
            }
        }
        self.compose_json_file.write_text(json.dumps(mock_compose), encoding="utf-8")
        self.fail_count_file.write_text("3", encoding="utf-8")
        self._install_mock_docker()

        res = self._run_deploy()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Failed to push registry.example.test/app:latest after 3 attempts", res.stderr)
        pushed = self.push_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(pushed), 3)
        sleeps = self.sleep_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(sleeps, ["5", "10"])

    def test_live_odin_compose_extracts_hermes_once(self) -> None:
        odin_compose = _find_odin_compose()
        if odin_compose is None or shutil.which("docker") is None:
            self.skipTest("Odin docker-compose.yml or docker binary not present")

        self._install_mock_docker(real_docker_for_config=True)
        dummy_env = {
            "STACK_NAME": "odin",
            "REGISTRY_PREFIX": "registry.example.invalid/",
            "DOMAIN_NAME": "example.invalid",
            "HOST_AGY_CONFIG_DIR": "/tmp/agy",
            "OBSIDIAN_VAULT_PATH": "/tmp/vault",
            "LLDAP_LDAP_BASE_DN": "dc=example,dc=invalid",
            "CHAT_MODEL": "local",
            "AGENT_MODEL": "m",
            "OLLAMA_BASE_MODEL": "q",
            "OLLAMA_MODEL_NAME": "m",
            "OLLAMA_CONTEXT_LENGTH": "1024",
            "OLLAMA_NUM_THREADS": "4",
            "MUNINN_MASTER_KEY": "k",
            "GEMINI_API_KEY": "k",
            "DEEPSEEK_API_KEY": "k",
            "API_SERVER_KEY": "k",
            "TYPESAFE_API_KEY": "k",
            "TYPESAFE_ENDPOINT": "http://x",
            "TYPESAFE_DEFAULT_MODEL": "m",
            "DISCORD_BOT_TOKEN": "t",
            "DISCORD_USER_ID": "1",
            "KANBAN_DISCORD_CHANNEL_ID": "1",
            "KANBAN_DEFAULT_ASSIGNEE": "w",
            "DISCORD_CHANNEL_INFO_ID": "1",
            "DISCORD_CHANNEL_HEALTH_ID": "1",
            "DISCORD_CHANNEL_WEALTH_ID": "1",
            "DISCORD_CHANNEL_WATCH_ID": "1",
            "DISCORD_CHANNEL_ALERT_ID": "1",
            "TAVILY_API_KEY": "k",
            "KRATOS_MCP_TOKEN": "k",
            "MNEMOSYNE_LLM_ENABLED": "true",
            "MNEMOSYNE_LLM_BASE_URL": "http://x",
            "MNEMOSYNE_LLM_MODEL": "m",
            "HERMES_GIT_USER_NAME": "h",
            "HERMES_GIT_USER_EMAIL": "h@x",
            "GH_TOKEN": "t",
            "LLDAP_LDAP_USER_PASS_NAME": "s1",
            "HERMES_SSH_KEY_NAME": "s2",
            "WEBUI_SECRET_KEY_NAME": "s3",
            "HERMES_CONFIG_SHA": "testsha",
        }
        res = self._run_deploy(
            extra_env=dummy_env,
            compose_path=str(odin_compose),
            stack_name="odin",
        )
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        pushed = self.push_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(pushed.count("registry.example.invalid/hermes-agent:odin"), 1)
        self.assertEqual(
            set(pushed),
            {
                "registry.example.invalid/hermes-agent:odin",
                "registry.example.invalid/jev-shim:odin",
                "registry.example.invalid/mnemosyne-dashboard:odin",
            },
        )
        for excluded in ["postgres", "ollama", "searxng", "muninn", "open-webui"]:
            for item in pushed:
                self.assertNotIn(excluded, item)

    def test_push_loop_preserves_exported_image_tag_for_stack_deploy(self) -> None:
        mock_compose = {
            "services": {
                "ui-storybook": {
                    "image": "registry.example.test/ui-storybook:storybook",
                    "build": {"context": "."},
                }
            }
        }
        self.compose_json_file.write_text(json.dumps(mock_compose), encoding="utf-8")
        self._install_mock_docker()

        res = self._run_deploy(extra_env={"IMAGE_TAG": "storybook"}, stack_name="ui")
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        pushed = self.push_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(pushed, ["registry.example.test/ui-storybook:storybook"])
        self.assertEqual(
            self.stack_env_log.read_text(encoding="utf-8").strip(),
            "IMAGE_TAG=storybook",
        )

    def test_default_deploy_is_in_place_without_stack_rm(self) -> None:
        self._install_mock_docker()

        res = self._run_deploy(extra_args=["--skip-build"])
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        stack_cmds = self.stack_cmd_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(stack_cmds), 1)
        self.assertFalse(any(cmd.startswith("rm ") for cmd in stack_cmds))
        self.assertEqual(
            stack_cmds[0],
            "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
        )
        self.assertIn("Updating service teststack_app", res.stdout)

    def test_recreate_flag_calls_stack_rm_before_deploy(self) -> None:
        self._install_mock_docker()

        res = self._run_deploy(extra_args=["--skip-build", "--recreate"])
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        stack_cmds = self.stack_cmd_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            stack_cmds,
            [
                "rm teststack",
                "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
            ],
        )

    def test_immutable_config_error_falls_back_to_recreate_and_succeeds(self) -> None:
        self._install_mock_docker()
        messages = [
            "Error response from daemon: rpc error: code = InvalidArgument desc = only updates to Labels are allowed",
            "failed to update config odin_hermes_cfg_abc123",
            "failed to update secret db_password_v2",
            "changing port 80 is not supported",
            "service mode change is not allowed",
            "renaming services is not supported",
            "service spec contains immutable field change",
        ]
        for msg in messages:
            with self.subTest(msg=msg):
                self.stack_cmd_log.unlink(missing_ok=True)
                self.stack_deploy_fail_count_file.write_text("1", encoding="utf-8")
                self.stack_deploy_fail_msg_file.write_text(f"{msg}\n", encoding="utf-8")

                res = self._run_deploy(extra_args=["--skip-build"])
                self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
                stack_cmds = self.stack_cmd_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(
                    stack_cmds,
                    [
                        "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
                        "rm teststack",
                        "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
                    ],
                )
                self.assertIn(
                    "Warning: In-place deploy rejected due to immutable Swarm config/spec change; falling back to stack recreate for teststack...",
                    res.stderr,
                )
                self.assertIn(msg, res.stderr)

    def test_immutable_fallback_retry_failure_exits_nonzero(self) -> None:
        self._install_mock_docker()
        self.stack_deploy_fail_count_file.write_text("2", encoding="utf-8")
        self.stack_deploy_fail_msg_file.write_text(
            "only updates to Labels are allowed\n",
            encoding="utf-8",
        )
        self.stack_deploy_fail_rc_file.write_text("3", encoding="utf-8")

        res = self._run_deploy(extra_args=["--skip-build"])
        self.assertEqual(res.returncode, 3)
        stack_cmds = self.stack_cmd_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            stack_cmds,
            [
                "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
                "rm teststack",
                "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
            ],
        )

    def test_non_immutable_deploy_failure_exits_original_code_without_stack_rm(self) -> None:
        self._install_mock_docker()
        self.stack_deploy_fail_count_file.write_text("1", encoding="utf-8")
        self.stack_deploy_fail_msg_file.write_text(
            "network aether-net not found\n",
            encoding="utf-8",
        )
        self.stack_deploy_fail_rc_file.write_text("2", encoding="utf-8")

        res = self._run_deploy(extra_args=["--skip-build"])
        self.assertEqual(res.returncode, 2)
        stack_cmds = self.stack_cmd_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            stack_cmds,
            [
                "deploy --with-registry-auth --prune --resolve-image always -c docker-compose.yml teststack",
            ],
        )
        self.assertFalse(any(cmd.startswith("rm ") for cmd in stack_cmds))
        self.assertIn("network aether-net not found", res.stderr)
        self.assertNotIn("falling back to stack recreate", res.stderr)


if __name__ == "__main__":
    unittest.main()

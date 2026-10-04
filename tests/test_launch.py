"""Adapted source launcher contracts, extended for Kit's shared lifecycle."""

import json
import os
import pty
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

from headroom_kit import proxy as kit_proxy
from headroom_kit import session as kit
from headroom_kit.runtime import TESTED_HEADROOM

ROOT = Path(__file__).resolve().parents[1]
STANDIN = Path(__file__).with_name("standin.py").read_text()
AGENTS = {
    "codex-headroom": "codex",
    "codex-app-headroom": "codex-app",
    "copilot-headroom": "copilot",
    "copilot-app-headroom": "copilot-app",
    "copilot-vscode-headroom": "copilot-vscode",
    "pi-headroom": "pi",
    "opencode-headroom": "opencode",
}


def unique_ports(count: int) -> list[int]:
    listeners = []
    try:
        for _ in range(count):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listeners.append(listener)
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in (
            "codex",
            "copilot",
            "pi",
            "opencode",
            "code",
            "code-insiders",
            "agent with spaces",
        ):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + STANDIN)
            path.chmod(0o755)
        self.interpreter = self.bin / "kit-python"
        self.interpreter.write_text(f"#!{sys.executable}\n" + STANDIN)
        self.interpreter.chmod(0o755)
        codex_port, copilot_port, pi_port, opencode_port, vscode_port = unique_ports(5)
        self.cfg = {
            "startupTimeout": 2,
            "openDashboard": True,
            "codexExecutable": "codex",
            "codexPort": codex_port,
            "codexAppPath": None,
            "copilotExecutable": "copilot",
            "copilotPort": copilot_port,
            "piExecutable": "pi",
            "piPort": pi_port,
            "opencodeExecutable": "opencode",
            "opencodePort": opencode_port,
            "vscodeChannel": "insiders",
            "vscodeExecutable": None,
            "vscodePort": vscode_port,
            "vscodeUserDataDir": None,
            "vscodeExtensionsDir": None,
        }
        self.defaults = self.root / "defaults.json"
        self.defaults.write_text(json.dumps(self.cfg))
        # Deliberately do not inherit real runtime configuration or credentials.
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "KIT_TEST_ROOT": str(self.root),
            "KIT_TEST_SRC": str(ROOT / "src"),
            "KIT_TEST_HEADROOM_VERSION": TESTED_HEADROOM,
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "VSCODE_IPC_HOOK_CLI": "fake-existing-editor",
            "VSCODE_PORTABLE": "fake-portable",
        }
        self.config = self.root / ".codex/config.toml"
        self.config.parent.mkdir()
        self.original = b'model = "existing-model"\n# Keep all existing preferences\n'
        self.config.write_bytes(self.original)
        self.addCleanup(self.stop_proxies)

    def stop_proxies(self) -> None:
        for key in ("codexPort", "copilotPort", "vscodePort", "piPort", "opencodePort"):
            state = kit_proxy.control(self.cfg[key])
            if state:
                kit_proxy.control(self.cfg[key], "stop", state["instance"])

    def events(self) -> list[dict[str, kit_proxy.Json]]:
        path = self.root / "events"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def invocation(self, command: str, args: Sequence[str]) -> list[str]:
        if command == "headroom-kit":
            return [str(self.interpreter), "-m", "headroom_kit", *args]
        return [
            str(self.interpreter),
            "-m",
            "headroom_kit",
            "--config",
            str(self.defaults),
            "run",
            AGENTS[command],
            "--",
            *args,
        ]

    def run_launcher(
        self,
        *args: str,
        command: str = "codex-headroom",
        mode: str = "",
        env: dict[str, str] | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.invocation(command, args),
            env=dict(self.env, KIT_TEST_MODE=mode, **(env or {})),
            cwd=self.root,
            capture_output=True,
            text=True,
            input=input,
            timeout=15,
        )

    def start(
        self,
        command: str = "codex-headroom",
        mode: str = "wait",
        env: dict[str, str] | None = None,
        args: Sequence[str] = (),
        stdin: int | None = None,
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            self.invocation(command, args),
            env=dict(self.env, KIT_TEST_MODE=mode, **(env or {})),
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin,
            text=True,
            start_new_session=True,
        )

        def cleanup() -> None:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

        self.addCleanup(cleanup)
        return process

    def wait_for(self, event: str, process: subprocess.Popen[str], count: int = 1) -> None:
        deadline = time.monotonic() + 8
        while len([e for e in self.events() if e["event"] == event]) < count:
            if process.poll() is not None:
                self.fail(process.communicate()[1])
            self.assertLess(time.monotonic(), deadline, f"Timed out waiting for {event}")
            time.sleep(0.02)

    def test_arguments_stdout_stdin_cwd_status_and_normal_config(self) -> None:
        args = ["exec", "--json", "a prompt with spaces and $(literal)"]
        result = self.run_launcher(*args, mode="stdin", input="prompt from stdin\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "AGENT_OUTPUT\nprompt from stdin\n")
        self.assertNotIn("fake-private", result.stderr)
        self.assertEqual(self.config.read_bytes(), self.original)
        event = next(e for e in self.events() if e["event"] == "agent")
        self.assertEqual(event["cwd"], str(self.root))
        self.assertEqual(
            event["args"],
            [
                *args,
                "-c",
                'model_provider="openai"',
                "-c",
                f'openai_base_url="http://127.0.0.1:{self.cfg["codexPort"]}/v1"',
            ],
        )
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_agent_keeps_caller_umask_and_proxy_stays_private(self) -> None:
        (self.bin / "codex").write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\nPath('agent-output').touch()\n"
        )
        result = subprocess.run(
            self.invocation("codex-headroom", []),
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
            umask=0o022,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "agent-output").stat().st_mode & 0o777, 0o644)
        control = kit_proxy.runtime_dir() / f"{self.cfg['codexPort']}.sock"
        self.assertEqual(control.stat().st_mode & 0o777, 0o700)

    def test_codex_config_overrides_share_the_subcommand_scope(self) -> None:
        args = [
            "-c",
            'model_reasoning_effort="low"',
            "exec",
            "--config=model_verbosity=low",
            "-cweb_search=disabled",
            "--",
            "a prompt with --config=literal",
        ]
        result = self.run_launcher(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = next(e for e in self.events() if e["event"] == "agent")["args"]
        # Codex 0.154.0 replaces global -c values when a subcommand has its
        # own -c values. Keep user settings and routing in that same scope.
        self.assertEqual(actual[0], "exec")
        self.assertEqual(actual[-2:], args[-2:])
        settings = dict(value.split("=", 1) for value in actual[2:-2:2])
        self.assertEqual(settings["model_reasoning_effort"], '"low"')
        self.assertEqual(settings["model_verbosity"], "low")
        self.assertEqual(settings["web_search"], "disabled")
        self.assertEqual(settings["model_provider"], '"openai"')
        self.assertEqual(
            settings["openai_base_url"],
            f'"http://127.0.0.1:{self.cfg["codexPort"]}/v1"',
        )

    def test_agent_failure_status_preserves_shared_proxy(self) -> None:
        result = self.run_launcher(mode="agent-failure")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_pi_launch_preserves_arguments_config_and_shared_proxy(self) -> None:
        config = self.root / ".pi/agent/models.json"
        config.parent.mkdir(parents=True)
        config.write_bytes(b'{"providers": {}}\n')
        args = ["--provider", "openai", "--no-extensions", "--", "literal --help"]
        result = self.run_launcher(*args, command="pi-headroom", mode="agent-failure")
        self.assertEqual(result.returncode, 37, result.stderr)
        event = next(e for e in self.events() if e["event"] == "agent")
        self.assertEqual(event["args"][:3], args[:3])
        self.assertEqual(event["args"][-2:], args[-2:])
        self.assertEqual(event["args"][3], "--extension")
        self.assertTrue(Path(event["args"][4]).is_file())
        self.assertEqual(
            event["env"]["HEADROOM_KIT_ENDPOINT"], f"http://127.0.0.1:{self.cfg['piPort']}/v1"
        )
        self.assertNotIn("HEADROOM_KIT_COPILOT_ENDPOINT", event["env"])
        self.assertEqual(config.read_bytes(), b'{"providers": {}}\n')
        self.assertEqual(event["cwd"], str(self.root))
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_pi_github_copilot_uses_the_shared_copilot_proxy(self) -> None:
        result = self.run_launcher(
            "--provider",
            "github-copilot",
            command="pi-headroom",
            mode="agent-failure",
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        event = next(e for e in self.events() if e["event"] == "agent")
        self.assertEqual(
            event["env"]["HEADROOM_KIT_COPILOT_ENDPOINT"],
            f"http://127.0.0.1:{self.cfg['copilotPort']}/v1",
        )
        starts = [e for e in self.events() if e["event"] == "proxy-start"]
        self.assertEqual(len(starts), 2)
        self.assertTrue(any("--openai-api-url" in e["args"] for e in starts))

    def test_pi_github_copilot_requires_headroom_login(self) -> None:
        result = self.run_launcher(
            "--provider",
            "github-copilot",
            command="pi-headroom",
            mode="auth-failure",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("headroom-kit copilot-auth login", result.stderr)
        self.assertNotIn("`headroom copilot-auth", result.stderr)
        self.assertFalse(any(e["event"] == "agent" for e in self.events()))
        self.assertFalse(any(e["event"] == "proxy-start" for e in self.events()))

    def test_opencode_github_copilot_uses_the_shared_copilot_proxy(self) -> None:
        result = self.run_launcher(
            "run",
            "--model",
            "github-copilot/gpt-4.1",
            command="opencode-headroom",
            mode="agent-failure",
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        event = [e for e in self.events() if e["event"] == "agent"][-1]
        plugin = json.loads(event["env"]["OPENCODE_CONFIG_CONTENT"])["plugins"][-1]
        self.assertEqual(
            plugin["options"]["copilot"], f"http://127.0.0.1:{self.cfg['copilotPort']}/v1"
        )
        self.assertEqual(
            plugin["options"]["endpoint"], f"http://127.0.0.1:{self.cfg['opencodePort']}/v1"
        )

    def test_opencode_github_copilot_requires_headroom_login(self) -> None:
        for option in ("--model", "-m"):
            with self.subTest(option=option):
                result = self.run_launcher(
                    "run",
                    option,
                    "github-copilot/gpt-4.1",
                    command="opencode-headroom",
                    mode="auth-failure",
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("headroom-kit copilot-auth login", result.stderr)
                self.assertNotIn("`headroom copilot-auth", result.stderr)
        self.assertFalse(any(e["event"] == "proxy-start" for e in self.events()))
        agents = [e for e in self.events() if e["event"] == "agent"]
        self.assertTrue(agents)
        self.assertTrue(all(e["args"] == ["--version"] for e in agents))
        self.assertTrue(all("OPENCODE_CONFIG_CONTENT" not in e.get("env", {}) for e in agents))

    def test_opencode_private_server_and_config_overlay(self) -> None:
        config = self.root / "opencode.jsonc"
        original = b'// Keep preferences\n{"model":"openai/test-only"}\n'
        config.write_bytes(original)
        inherited = {"plugins": ["existing-plugin"], "model": "openai/test-only"}
        result = self.run_launcher(
            "run",
            "--",
            "literal --server",
            command="opencode-headroom",
            env={"OPENCODE_CONFIG_CONTENT": json.dumps(inherited)},
            mode="agent-failure",
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        event = [e for e in self.events() if e["event"] == "agent"][-1]
        self.assertEqual(event["args"], ["run", "--standalone", "--", "literal --server"])
        content = json.loads(event["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(content["model"], inherited["model"])
        self.assertEqual(content["plugins"][0], "existing-plugin")
        plugin = content["plugins"][-1]
        self.assertTrue((Path(plugin["package"]) / "index.js").is_file())
        self.assertEqual(
            plugin["options"]["endpoint"], f"http://127.0.0.1:{self.cfg['opencodePort']}/v1"
        )
        self.assertEqual(config.read_bytes(), original)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_opencode_rejects_shared_servers_and_bad_inline_config(self) -> None:
        for args in (
            ["--server", "http://127.0.0.1:1"],
            ["run", "--standalone=false"],
            ["service", "start"],
            ["--log-level", "debug", "service", "start"],
        ):
            with self.subTest(args=args):
                result = self.run_launcher(*args, command="opencode-headroom")
                self.assertEqual(result.returncode, 1, result.stderr)
        for content in ("not JSON fake-secret", "[]", '{"plugins": "invalid"}'):
            result = self.run_launcher(
                command="opencode-headroom", env={"OPENCODE_CONFIG_CONTENT": content}
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("OPENCODE_CONFIG_CONTENT", result.stderr)
            self.assertNotIn("fake-secret", result.stderr)
        self.assertFalse(any(e["event"] in ("resolve", "proxy-start") for e in self.events()))

    def test_opencode_v1_is_rejected_before_runtime_resolution(self) -> None:
        result = self.run_launcher(
            command="opencode-headroom", env={"KIT_TEST_OPENCODE_VERSION": "1.2.0"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires OpenCode v2", result.stderr)
        self.assertFalse(any(e["event"] in ("resolve", "proxy-start") for e in self.events()))

    def test_cleanup_propagates_process_errors_and_restores_handlers(self) -> None:
        class InaccessibleProcess(subprocess.Popen[bytes]):
            def poll(self) -> int | None:
                raise PermissionError("Cannot inspect proxy process")

        handlers = {
            sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        metadata = self.root / "proxy.json"
        original = json.dumps({"owner": os.getpid()})
        metadata.write_text(original)
        with (
            InaccessibleProcess([sys.executable, "-c", "pass"]) as process,
            self.assertRaisesRegex(PermissionError, "Cannot inspect proxy process"),
        ):
            kit_proxy.cleanup_proxy(process, metadata)
        self.assertEqual(metadata.read_text(), original)
        self.assertEqual({sig: signal.getsignal(sig) for sig in handlers}, handlers)

    def test_help_version_no_resolution_or_state(self) -> None:
        for command in ("codex-headroom", "copilot-headroom", "pi-headroom", "opencode-headroom"):
            for args in (["--help"], ["--version"], ["exec", "--help"]):
                result = self.run_launcher(
                    *args, command=command, env={"HEADROOM_VERSION": "invalid"}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        for command in ("codex-app-headroom", "copilot-app-headroom", "copilot-vscode-headroom"):
            self.assertEqual(self.run_launcher("--help", command=command).returncode, 0)
        self.assertTrue(all(e["event"] == "agent" for e in self.events()))
        self.assertFalse((self.root / "state").exists())

    def test_missing_agent_precedes_download(self) -> None:
        result = self.run_launcher(env={"HEADROOM_CODEX_EXECUTABLE": "/missing/agent"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("HEADROOM_CODEX_EXECUTABLE", result.stderr)
        self.assertEqual(self.events(), [])

    def test_executable_with_spaces(self) -> None:
        result = self.run_launcher(
            "a b",
            env={"HEADROOM_CODEX_EXECUTABLE": str(self.bin / "agent with spaces")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(next(e for e in self.events() if e["event"] == "agent")["args"][0], "a b")

    def test_run_requires_the_argument_separator(self) -> None:
        result = subprocess.run(
            [str(self.interpreter), "-m", "headroom_kit", "run", "codex"],
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--", result.stderr)
        self.assertEqual(self.events(), [])

    def test_legacy_runtime_selection_is_rejected(self) -> None:
        result = self.run_launcher(env={"HEADROOM_VERSION": "unsupported"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("HEADROOM_VERSION", result.stderr)
        self.assertIn("stop", result.stderr)
        self.assertEqual(self.events(), [])
        raw = json.loads(self.defaults.read_text())
        raw.update(version="latest", uv="uvx", python=sys.executable)
        self.defaults.write_text(json.dumps(raw))
        result = self.run_launcher()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("version", result.stderr)
        self.assertEqual(self.events(), [])

    def test_runtime_version_matches_dependency_pin(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertIn(
            f"headroom-ai[proxy,code]=={TESTED_HEADROOM}", project["project"]["dependencies"]
        )

    def test_version_identifies_kit_and_headroom_without_a_proxy(self) -> None:
        result = subprocess.run(
            [str(self.interpreter), "-m", "headroom_kit", "--version"],
            env=dict(self.env, HEADROOM_VERSION="latest"),
            cwd="/",
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"headroom-kit 0.0.0+test\nheadroom-ai {TESTED_HEADROOM}\n")
        self.assertNotIn("latest", result.stdout)
        self.assertEqual(self.events(), [])

    def test_proxy_disables_allocator_reexec_before_launch(self) -> None:
        for inherited in ({}, {"HEADROOM_MALLOC_TUNING": "1"}):
            with self.subTest(inherited=inherited):
                result = self.run_launcher(mode="startup-failure", env=inherited)
                self.assertEqual(result.returncode, 1, result.stderr)
                event = [e for e in self.events() if e["event"] == "proxy-start"][-1]
                self.assertEqual(event["env"]["HEADROOM_MALLOC_TUNING"], "0")
                self.assertNotIn("HEADROOM_VERSION", event["env"])

    def test_bad_ports_and_conflicting_providers_fail_before_resolution(self) -> None:
        for env in (
            {"HEADROOM_CODEX_PORT": "0"},
            {"HEADROOM_CODEX_PORT": "65536"},
            {"HEADROOM_CODEX_PORT": "bad"},
            {"HEADROOM_CODEX_PORT": str(self.cfg["copilotPort"])},
            {"HEADROOM_STARTUP_TIMEOUT": "0"},
            {"HEADROOM_STARTUP_TIMEOUT": "-1"},
            {"HEADROOM_STARTUP_TIMEOUT": "bad"},
        ):
            self.assertEqual(self.run_launcher(env=env).returncode, 1)
        for args in (
            ["--oss"],
            ["-c", 'model_provider="custom"'],
            ["--config=openai_base_url=somewhere"],
            ["-cmodel_provider=custom"],
        ):
            self.assertEqual(self.run_launcher(*args).returncode, 1)
        self.assertEqual(self.events(), [])

    def test_startup_timeout_is_not_limited_to_port_range(self) -> None:
        result = self.run_launcher(env={"HEADROOM_STARTUP_TIMEOUT": "65536"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_http_proxy_environment_cannot_intercept_health(self) -> None:
        result = self.run_launcher(
            env={"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_client_proxy_exclusions_preserve_upstream_environment(self) -> None:
        proxy_vars = dict.fromkeys(
            (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
            ),
            "http://127.0.0.1:1",
        )
        cases = (
            ({}, "127.0.0.1,localhost,::1"),
            (
                {"NO_PROXY": "index.example.invalid"},
                "index.example.invalid,127.0.0.1,localhost,::1",
            ),
            (
                {"no_proxy": "internal.example.invalid"},
                "internal.example.invalid,127.0.0.1,localhost,::1",
            ),
            (
                {
                    "NO_PROXY": " index.example.invalid,127.0.0.1",
                    "no_proxy": "internal.example.invalid,localhost",
                },
                "index.example.invalid,127.0.0.1,internal.example.invalid,localhost,::1",
            ),
        )
        for command in (
            "codex-headroom",
            "copilot-headroom",
            "copilot-vscode-headroom",
            "pi-headroom",
            "opencode-headroom",
        ):
            for exclusions, expected in cases:
                with self.subTest(command=command, exclusions=exclusions):
                    self.stop_proxies()
                    inherited = dict(proxy_vars, **exclusions)
                    result = self.run_launcher(command=command, mode="agent-failure", env=inherited)
                    self.assertEqual(result.returncode, 37, result.stderr)
                    child = next(
                        e for e in reversed(self.events()) if e["event"] in ("agent", "editor")
                    )
                    upstream = next(
                        e for e in reversed(self.events()) if e["event"] == "proxy-start"
                    )
                    self.assertEqual(
                        child["proxy_env"], dict(proxy_vars, NO_PROXY=expected, no_proxy=expected)
                    )
                    self.assertEqual(upstream["proxy_env"], inherited)

    def test_client_wildcard_keeps_upstream_proxy_policy(self) -> None:
        inherited = dict.fromkeys(
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"),
            "http://127.0.0.1:1",
        )
        inherited.update(NO_PROXY="other.example.invalid", no_proxy="*")
        for command in (
            "codex-headroom",
            "copilot-headroom",
            "copilot-vscode-headroom",
            "pi-headroom",
            "opencode-headroom",
        ):
            with self.subTest(command=command):
                result = self.run_launcher(command=command, mode="agent-failure", env=inherited)
                self.assertEqual(result.returncode, 37, result.stderr)
                child = next(
                    e for e in reversed(self.events()) if e["event"] in ("agent", "editor")
                )
                upstream = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
                self.assertEqual(child["proxy_env"], {"NO_PROXY": "*", "no_proxy": "*"})
                self.assertEqual(upstream["proxy_env"], inherited)

    def test_client_environment_does_not_mutate_the_caller(self) -> None:
        inherited = {
            "NO_PROXY": "index.example.invalid",
            "no_proxy": "other.example.invalid",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "KEEP_ME": "unchanged",
        }
        original = dict(inherited)
        child = kit.client_environment(inherited)
        self.assertEqual(inherited, original)
        self.assertEqual(child["KEEP_ME"], "unchanged")
        self.assertEqual(child["HTTPS_PROXY"], inherited["HTTPS_PROXY"])
        self.assertEqual(child["NO_PROXY"], child["no_proxy"])
        child["KEEP_ME"] = "child only"
        self.assertEqual(inherited, original)

    def test_stale_pid_metadata_cannot_stop_unrelated_process(self) -> None:
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.terminate)
        metadata = self.root / f"state/headroom-kit/{self.cfg['codexPort']}.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"owner": sleeper.pid}))
        result = self.run_launcher("stop", str(self.cfg["codexPort"]), command="headroom-kit")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(sleeper.poll())
        self.assertEqual(self.run_launcher().returncode, 0)
        self.stop_proxies()
        self.assertIsNone(sleeper.poll())

    def test_failed_editor_proxy_does_not_open_editor(self) -> None:
        result = self.run_launcher(command="copilot-vscode-headroom", mode="startup-failure")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(any(e["event"] == "editor" for e in self.events()))

    def test_configurable_extension_path_with_spaces(self) -> None:
        extensions = self.root / "custom extensions"
        result = self.run_launcher(
            command="copilot-vscode-headroom",
            mode="agent-failure",
            env={"HEADROOM_VSCODE_EXTENSIONS_DIR": str(extensions)},
        )
        self.assertEqual(result.returncode, 37, result.stderr)
        event = next(e for e in self.events() if e["event"] == "editor")
        self.assertEqual(
            event["args"][event["args"].index("--extensions-dir") + 1], str(extensions)
        )

    def test_unexpected_proxy_version_fails_closed(self) -> None:
        result = self.run_launcher(env={"KIT_TEST_VERSION": "unsupported"})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not launched", result.stderr)
        self.assertNotIn("fake-private", result.stderr)
        self.assertFalse(any(e["event"] == "agent" for e in self.events()))

    def test_startup_failure_wrong_upstream_and_timeout(self) -> None:
        for mode in ("startup-failure", "wrong-upstream", "not-ready"):
            result = self.run_launcher(mode=mode, env={"HEADROOM_STARTUP_TIMEOUT": "1"})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("not launched", result.stderr)
        self.assertFalse(any(e["event"] == "agent" for e in self.events()))

    def test_occupied_socket_is_not_reused_or_killed(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", self.cfg["codexPort"]))
            listener.listen()
            result = self.run_launcher()
            self.assertEqual(result.returncode, 1)
            self.assertIn("occupied", result.stderr)
            self.assertEqual(listener.getsockname()[1], self.cfg["codexPort"])
        self.assertFalse(any(e["event"] == "proxy-start" for e in self.events()))

    def test_reuse_preserves_proxy_after_agent_failure(self) -> None:
        owner = self.start()
        self.wait_for("agent", owner)
        result = self.run_launcher(mode="agent-failure")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertIn("Reusing", result.stderr)
        self.assertIsNone(owner.poll())
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_sequential_clients_share_after_first_exit(self) -> None:
        for command in ("codex-headroom", "copilot-headroom", "pi-headroom", "opencode-headroom"):
            with self.subTest(command=command):
                before = len([e for e in self.events() if e["event"] == "proxy-start"])
                for _ in range(2):
                    result = self.run_launcher(command=command, mode="traffic")
                    self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    len([e for e in self.events() if e["event"] == "proxy-start"]), before + 1
                )
                replies = [e for e in self.events() if e["event"] == "client-response"][-2:]
                self.assertEqual(len({e["instance"] for e in replies}), 1)
                self.assertEqual([e["count"] for e in replies], [1, 2])
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_simultaneous_clients_wait_and_share(self) -> None:
        clients = [self.start(mode="wait-traffic") for _ in range(3)]
        for client in clients:
            self.wait_for("agent", client, count=3)
        self.wait_for("client-response", clients[0], count=6)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        replies = [e for e in self.events() if e["event"] == "client-response"]
        self.assertEqual(len({e["instance"] for e in replies}), 1)

    def test_first_client_exit_signal_or_kill_preserves_other_traffic(self) -> None:
        for sig in (None, signal.SIGINT, signal.SIGKILL):
            with self.subTest(signal=sig):
                exit_file = self.root / "finish-first-client"
                first = self.start(mode="wait-traffic", env={"KIT_TEST_EXIT_FILE": str(exit_file)})
                self.wait_for("client-response", first)
                second = self.start(mode="wait-traffic", env={"KIT_TEST_CLIENT": "survivor"})
                self.wait_for("agent", second, count=2)
                if sig is None:
                    exit_file.touch()
                else:
                    os.killpg(first.pid, sig)
                first.communicate(timeout=12)
                before = len([e for e in self.events() if e["event"] == "client-response"])
                self.wait_for("client-response", second, count=before + 3)
                result = self.run_launcher(mode="traffic")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
                second.terminate()
                second.communicate(timeout=12)
                self.stop_proxies()
                (self.root / "events").unlink()
                exit_file.unlink(missing_ok=True)

    def test_stop_selects_instance_and_next_launch_starts_cleanly(self) -> None:
        for command in ("codex-headroom", "pi-headroom"):
            self.assertEqual(self.run_launcher(command=command, mode="traffic").returncode, 0)
        state = kit_proxy.control(self.cfg["codexPort"])
        self.assertFalse(
            kit_proxy.control(self.cfg["codexPort"], "stop", "wrong-instance")["stopped"]
        )
        result = self.run_launcher("stop", str(self.cfg["codexPort"]), command="headroom-kit")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(kit_proxy.port_free(self.cfg["codexPort"]))
        self.assertTrue(kit_proxy.control(self.cfg["piPort"])["ready"])
        self.assertEqual(self.run_launcher(mode="traffic").returncode, 0)
        self.assertNotEqual(kit_proxy.control(self.cfg["codexPort"])["instance"], state["instance"])

    def test_account_separation_and_rotating_access_token(self) -> None:
        for token in ("fake-first-access", "fake-rotated-access"):
            result = self.run_launcher(
                command="copilot-headroom", mode="traffic", env={"KIT_TEST_ACCESS_TOKEN": token}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        result = self.run_launcher(
            command="copilot-headroom", env={"KIT_TEST_ACCOUNT": "fake-other-account"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("incompatible", result.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "agent"]), 2)
        self.assertNotIn("fake-", json.dumps(kit_proxy.control(self.cfg["copilotPort"])))

    def test_copilot_cli_and_editor_share_only_matching_context(self) -> None:
        result = self.run_launcher(command="copilot-headroom", env={"HEADROOM_COMPRESSORS": "log"})
        self.assertEqual(result.returncode, 0, result.stderr)
        for account, compressors, code in (
            ("fake-refresh-account-one", "log", 0),
            ("fake-other-account", "log", 1),
            ("fake-refresh-account-one", "search", 1),
        ):
            result = self.run_launcher(
                command="copilot-vscode-headroom",
                env={
                    "HEADROOM_VSCODE_PORT": str(self.cfg["copilotPort"]),
                    "HEADROOM_COMPRESSORS": compressors,
                    "KIT_TEST_ACCOUNT": account,
                },
            )
            self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        self.assertEqual(len([e for e in self.events() if e["event"] == "editor"]), 1)

    def test_failed_start_and_dead_proxy_recover(self) -> None:
        self.assertEqual(self.run_launcher(mode="startup-failure").returncode, 1)
        self.assertEqual(self.run_launcher(mode="traffic").returncode, 0)
        proxy = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
        os.kill(proxy["pid"], signal.SIGKILL)
        time.sleep(0.3)
        self.assertEqual(self.run_launcher(mode="traffic").returncode, 0)
        replies = [e for e in self.events() if e["event"] == "client-response"]
        self.assertNotEqual(replies[0]["instance"], replies[1]["instance"])

    def test_stale_socket_recovered_and_environment_change_refused(self) -> None:
        path = kit_proxy.runtime_dir() / f"{self.cfg['codexPort']}.sock"
        with socket.socket(socket.AF_UNIX) as stale:
            stale.bind(str(path))
        self.assertEqual(self.run_launcher(mode="traffic").returncode, 0)
        for variable in ("HTTPS_PROXY", "SSL_CERT_FILE", "OPENAI_API_KEY"):
            result = self.run_launcher(env={variable: "fake-other-context"})
            self.assertEqual(result.returncode, 1)
            self.assertIn("incompatible", result.stderr)
        self.assertEqual(
            self.run_launcher(mode="traffic", env={"PWD": "/elsewhere", "TERM": "dumb"}).returncode,
            0,
        )

    def test_status_and_stop_work_during_startup_without_resolution(self) -> None:
        first = self.start(mode="not-ready", env={"HEADROOM_STARTUP_TIMEOUT": "10"})
        self.wait_for("proxy-start", first)
        state = kit_proxy.control(self.cfg["codexPort"])
        self.assertFalse(state["ready"])
        result = self.run_launcher(
            "status", command="headroom-kit", env={"HEADROOM_VERSION": "invalid"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ready=False", result.stdout)
        result = self.run_launcher("stop", str(self.cfg["codexPort"]), command="headroom-kit")
        self.assertEqual(result.returncode, 0, result.stderr)
        first.communicate(timeout=12)
        self.assertNotEqual(first.returncode, 0)
        self.assertFalse(any(e["event"] in ("resolve", "agent") for e in self.events()))

    def test_killed_creator_during_startup_leaves_one_shared_proxy(self) -> None:
        first = self.start(mode="wait-traffic", env={"KIT_TEST_DELAY": "0.8"})
        self.wait_for("proxy-start", first)
        os.killpg(first.pid, signal.SIGKILL)
        first.communicate(timeout=12)
        result = self.run_launcher(mode="traffic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        self.assertEqual(len([e for e in self.events() if e["event"] == "client-response"]), 1)

    def test_simultaneous_editor_windows_share_transaction_lock(self) -> None:
        clients = [self.start(command="copilot-vscode-headroom", mode="") for _ in range(3)]
        for client in clients:
            _, error = client.communicate(timeout=12)
            self.assertEqual(client.returncode, 0, error)
        self.assertEqual(len([e for e in self.events() if e["event"] == "editor"]), 3)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_healthy_foreign_listener_is_neither_reused_nor_stopped(self) -> None:
        port = self.cfg["codexPort"]
        foreign = subprocess.Popen(
            [str(self.interpreter), "-m", "headroom.cli", "proxy", "--port", str(port)],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(foreign.wait)
        self.addCleanup(foreign.terminate)
        deadline = time.monotonic() + 3
        while not kit_proxy.compatible(kit_proxy.health(port), TESTED_HEADROOM, None):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertEqual(self.run_launcher().returncode, 1)
        self.assertEqual(self.run_launcher("stop", str(port), command="headroom-kit").returncode, 1)
        self.assertIsNone(foreign.poll())
        self.assertTrue(kit_proxy.health(port)["ready"])
        self.assertFalse(any(e["event"] == "agent" for e in self.events()))

    def test_plain_client_keeps_normal_route(self) -> None:
        self.assertEqual(self.run_launcher(mode="traffic").returncode, 0)
        before = len([e for e in self.events() if e["event"] == "proxy-request"])
        result = subprocess.run(
            [str(self.bin / "codex")], env=self.env, capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout, "AGENT_OUTPUT\n")
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-request"]), before)
        event = self.events()[-1]
        self.assertEqual(event["args"], [])

    def test_legacy_version_does_not_attach_or_stop_a_running_proxy(self) -> None:
        owner = self.start()
        self.wait_for("agent", owner)
        for version in ("unsupported", "latest"):
            result = self.run_launcher(env={"HEADROOM_VERSION": version})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("HEADROOM_VERSION", result.stderr)
            self.assertNotIn("Stopped", result.stderr)
        self.assertIsNone(owner.poll())

    def test_interrupt_preserves_shared_proxy(self) -> None:
        process = self.start()
        self.wait_for("agent", process)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 130, stderr)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))
        self.assertFalse(kit_proxy.port_free(self.cfg["codexPort"]))

    def test_detached_owner_signal_releases_port_for_the_next_launch(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                self.assert_owner_signal_releases_port(sig)
                self.stop_proxies()
                (self.root / "events").unlink(missing_ok=True)

    def test_detached_owner_signal_during_spawn_cleans_up(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                result = self.run_launcher(env={"KIT_TEST_OWNER_SIGNAL": str(sig)})
                self.assertEqual(result.returncode, 1, result.stderr)
                proxy_pid = int(
                    next(
                        event["pid"] for event in self.events() if event["event"] == "proxy-spawned"
                    )
                )
                socket_path = kit_proxy.runtime_dir() / f"{self.cfg['codexPort']}.sock"
                try:
                    self.wait_until_released(proxy_pid, socket_path)
                    self.assertTrue(kit_proxy.port_free(self.cfg["codexPort"]))
                    self.assertFalse(any(event["event"] == "agent" for event in self.events()))
                    again = self.run_launcher(mode="traffic")
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertIn("Started Headroom", again.stderr)
                finally:
                    if self.process_alive(proxy_pid):
                        os.killpg(proxy_pid, signal.SIGKILL)
                    self.stop_proxies()
                    socket_path.unlink(missing_ok=True)
                    (self.root / "events").unlink(missing_ok=True)

    def assert_owner_signal_releases_port(self, sig: int) -> None:
        result = self.run_launcher(mode="traffic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(event["event"] == "proxy-stop" for event in self.events()))
        proxy_pid = int(
            next(event["pid"] for event in self.events() if event["event"] == "proxy-start")
        )
        self.assertTrue(self.process_alive(proxy_pid))
        owner = self.parent_pid(proxy_pid)
        self.assertGreater(owner, 1)
        socket_path = kit_proxy.runtime_dir() / f"{self.cfg['codexPort']}.sock"
        self.assertTrue(socket_path.exists())
        os.kill(owner, sig)
        self.wait_until_released(proxy_pid, socket_path)
        self.assertTrue(kit_proxy.port_free(self.cfg["codexPort"]))
        self.assertTrue(any(event["event"] == "proxy-stop" for event in self.events()))
        again = self.run_launcher(mode="traffic")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("Started Headroom", again.stderr)

    def parent_pid(self, pid: int) -> int:
        output = subprocess.check_output(["/bin/ps", "-o", "ppid=", "-p", str(pid)], text=True)
        return int(output)

    def process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def wait_until_released(self, proxy_pid: int, socket_path: Path) -> None:
        deadline = time.monotonic() + 8
        while self.process_alive(proxy_pid) or socket_path.exists():
            self.assertLess(time.monotonic(), deadline, "Owner cleanup did not release the proxy")
            time.sleep(0.02)

    def test_reused_proxy_survives_interrupt(self) -> None:
        owner = self.start()
        self.wait_for("agent", owner)
        borrower = self.start()
        self.wait_for("agent", borrower, count=2)
        borrower.terminate()
        borrower.communicate(timeout=12)
        self.assertEqual(borrower.returncode, 143)
        self.assertIsNone(owner.poll())
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_foreground_terminal_is_inherited(self) -> None:
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        process = self.start(stdin=slave)
        self.wait_for("agent", process)
        self.assertTrue(next(e for e in self.events() if e["event"] == "agent")["tty"])

    def test_native_compression_overrides_and_restart_for_every_wrapper(self) -> None:
        overrides = {
            "HEADROOM_SAVINGS_PROFILE": "balanced",
            "HEADROOM_COMPRESSORS": "log",
            "HEADROOM_MODE": "token",
            "HEADROOM_LOSSLESS": "1",
            "HEADROOM_DISABLE_KOMPRESS": "1",
            "HEADROOM_DISABLE_KOMPRESS_FALLBACK": "1",
            "HEADROOM_OUTPUT_SHAPER": "on",
            "HEADROOM_EFFORT_ROUTER": "off",
            "HEADROOM_VERBOSITY_AUTOTUNE": "off",
        }
        for command in (
            "codex-headroom",
            "copilot-headroom",
            "copilot-vscode-headroom",
            "pi-headroom",
            "opencode-headroom",
        ):
            with self.subTest(command=command):
                self.stop_proxies()
                result = self.run_launcher(command=command, env=overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                event = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
                for key, value in overrides.items():
                    self.assertEqual(event["env"][key], value)
                again = self.run_launcher(command=command, env=overrides)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn("Reusing", again.stderr)
                for key in overrides:
                    changed = dict(
                        overrides,
                        **{key: "search" if key == "HEADROOM_COMPRESSORS" else "different"},
                    )
                    result = self.run_launcher(command=command, env=changed)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("incompatible managed proxy", result.stderr)
                self.stop_proxies()
                result = self.run_launcher(command=command)
                self.assertEqual(result.returncode, 0, result.stderr)
                event = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
                self.assertEqual(event["env"]["HEADROOM_SAVINGS_PROFILE"], "coding")

    def test_privacy_and_routing_remain_managed_without_compression_flags(self) -> None:
        result = self.run_launcher(
            env={
                "HEADROOM_BEACON": "on",
                "HEADROOM_OUTPUT_SHAPER": "on",
                "HEADROOM_HOST": "0.0.0.0",
                "HEADROOM_WORKERS": "8",
                "HEADROOM_BACKEND": "bedrock",
                "HEADROOM_PROXY_TOKEN": "fake-token",
                "HEADROOM_MODEL_ROUTER_ENABLED": "1",
                "OPENAI_TARGET_API_URL": "https://example.invalid",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        event = next(e for e in self.events() if e["event"] == "proxy-start")
        self.assertEqual(event["cwd"], "/")
        for arg in ("--no-cache", "--no-rate-limit", "--no-learn"):
            self.assertIn(arg, event["args"])
        for arg in ("--mode", "--lossless", "--disable-kompress", "--disable-kompress-fallback"):
            self.assertNotIn(arg, event["args"])
        self.assertEqual(event["env"]["HEADROOM_BEACON"], "off")
        self.assertEqual(event["env"]["HEADROOM_OUTPUT_SHAPER"], "on")
        for key in (
            "HEADROOM_HOST",
            "HEADROOM_WORKERS",
            "HEADROOM_BACKEND",
            "HEADROOM_PROXY_TOKEN",
            "HEADROOM_MODEL_ROUTER_ENABLED",
            "OPENAI_TARGET_API_URL",
        ):
            self.assertIsNone(event["env"][key])

    def test_persistent_metrics_defaults_for_every_proxy(self) -> None:
        for command in (
            "codex-headroom",
            "copilot-headroom",
            "copilot-vscode-headroom",
            "pi-headroom",
            "opencode-headroom",
        ):
            with self.subTest(command=command):
                result = self.run_launcher(command=command)
                self.assertEqual(result.returncode, 0, result.stderr)
        starts = [e for e in self.events() if e["event"] == "proxy-start"]
        self.assertEqual(len(starts), 5)
        paths = set()
        for event in starts:
            env = event["env"]
            port = event["args"][event["args"].index("--port") + 1]
            expected = self.root / ".headroom/headroom-kit" / port / "proxy_savings.json"
            self.assertEqual(env["HEADROOM_SAVINGS_PATH"], str(expected))
            paths.add(env["HEADROOM_SAVINGS_PATH"])
            self.assertEqual(env["HEADROOM_WORKSPACE_DIR"], str(self.root / ".headroom"))
            self.assertEqual(
                env["HEADROOM_SAVINGS_EVENTS_PATH"],
                str(self.root / ".headroom/savings_events.jsonl"),
            )
            self.assertEqual(env["HEADROOM_TELEMETRY"], "on")
            self.assertIsNone(env["HEADROOM_STATELESS"])
            for key in ("HEADROOM_BEACON", "HEADROOM_LOG_MESSAGES"):
                self.assertEqual(env[key], "off")
            self.assertIn("--no-learn", event["args"])
            for flag in ("--stateless", "--telemetry", "--no-telemetry"):
                self.assertNotIn(flag, event["args"])
        self.assertEqual(len(paths), 5)

    def test_native_metrics_overrides_and_reuse(self) -> None:
        overrides = {
            "HEADROOM_STATELESS": "1",
            "HEADROOM_TELEMETRY": "off",
            "HEADROOM_WORKSPACE_DIR": "workspace with spaces",
            "HEADROOM_SAVINGS_PATH": "counters/custom.json",
            "HEADROOM_SAVINGS_EVENTS_PATH": "~/events/custom.jsonl",
        }
        for command in ("codex-headroom", "copilot-headroom", "pi-headroom", "opencode-headroom"):
            with self.subTest(command=command):
                self.stop_proxies()
                result = self.run_launcher(command=command, env=overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                event = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
                expected = dict(overrides)
                for key in (
                    "HEADROOM_WORKSPACE_DIR",
                    "HEADROOM_SAVINGS_PATH",
                    "HEADROOM_SAVINGS_EVENTS_PATH",
                ):
                    expected[key] = str(self.root / overrides[key].removeprefix("~/"))
                for key, value in expected.items():
                    self.assertEqual(event["env"][key], value)
                again = self.run_launcher(command=command, env=expected)
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn("Reusing", again.stderr)
                for key in overrides:
                    changed = dict(overrides, **{key: "0" if key == "HEADROOM_STATELESS" else "on"})
                    result = self.run_launcher(command=command, env=changed)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("incompatible managed proxy", result.stderr)

    def test_empty_paths_use_defaults_and_workspace_selects_storage(self) -> None:
        paths = ("HEADROOM_WORKSPACE_DIR", "HEADROOM_SAVINGS_PATH", "HEADROOM_SAVINGS_EVENTS_PATH")
        for workspace in ("", "  ", "~/custom workspace", "relative workspace"):
            with self.subTest(workspace=workspace):
                self.stop_proxies()
                overrides = dict.fromkeys(paths, " ")
                overrides["HEADROOM_WORKSPACE_DIR"] = workspace
                result = self.run_launcher(env=overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                event = next(e for e in reversed(self.events()) if e["event"] == "proxy-start")
                root = self.root / (
                    workspace.removeprefix("~/") if workspace.strip() else ".headroom"
                )
                self.assertEqual(event["env"]["HEADROOM_WORKSPACE_DIR"], str(root))
                self.assertEqual(
                    event["env"]["HEADROOM_SAVINGS_PATH"],
                    str(root / "headroom-kit" / str(self.cfg["codexPort"]) / "proxy_savings.json"),
                )
                self.assertEqual(
                    event["env"]["HEADROOM_SAVINGS_EVENTS_PATH"], str(root / "savings_events.jsonl")
                )
                again = self.run_launcher(env={"HEADROOM_WORKSPACE_DIR": str(root)})
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn("Reusing", again.stderr)

    def test_copilot_native_routing_preserves_model_selection(self) -> None:
        for args in (
            ["--model", "gemini-3.8-flash"],
            ["--model=gpt-5.4"],
            ["--model", "claude-sonnet-5"],
            ["--model", "future-model"],
            ["--model", "auto"],
            [],
            ["--", "literal --model=auto"],
        ):
            with self.subTest(args=args):
                result = self.run_launcher(
                    *args,
                    command="copilot-headroom",
                    env={"COPILOT_MODEL": "saved-model"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                event = next(e for e in reversed(self.events()) if e["event"] == "agent")
                self.assertEqual(event["args"], args)
                self.assertEqual(
                    {k: v for k, v in event["env"].items() if k.startswith("COPILOT_")},
                    {
                        "COPILOT_API_URL": f"http://127.0.0.1:{self.cfg['copilotPort']}",
                        "COPILOT_MODEL": "saved-model",
                    },
                )
                self.assertNotIn("fake-test-token", result.stdout + result.stderr)
        starts = [e for e in self.events() if e["event"] == "proxy-start"]
        self.assertEqual(len(starts), 1)
        for flag in ("--openai-api-url", "--anthropic-api-url"):
            self.assertEqual(
                starts[0]["args"][starts[0]["args"].index(flag) + 1],
                "https://api.githubcopilot.com",
            )

    def test_copilot_native_routing_removes_inherited_byok_settings(self) -> None:
        inherited = dict.fromkeys(
            (
                "COPILOT_PROVIDER_TYPE",
                "COPILOT_PROVIDER_BASE_URL",
                "COPILOT_PROVIDER_API_KEY",
                "COPILOT_PROVIDER_API_KEY_COMMAND",
                "COPILOT_PROVIDER_BEARER_TOKEN",
                "COPILOT_PROVIDER_WIRE_API",
                "COPILOT_PROVIDER_TRANSPORT",
                "COPILOT_PROVIDER_MODEL_ID",
                "COPILOT_PROVIDER_WIRE_MODEL",
                "COPILOT_PROVIDER_MAX_PROMPT_TOKENS",
                "COPILOT_PROVIDER_MAX_OUTPUT_TOKENS",
                "COPILOT_PROVIDER_HEADERS",
                "COPILOT_PROVIDER_FUTURE_OPTION",
            ),
            "fake-byok-setting",
        )
        inherited["COPILOT_API_URL"] = "https://elsewhere.example.invalid"
        result = self.run_launcher(command="copilot-headroom", env=inherited)
        self.assertEqual(result.returncode, 0, result.stderr)
        event = next(e for e in self.events() if e["event"] == "agent")
        self.assertEqual(
            {k: v for k, v in event["env"].items() if k.startswith("COPILOT_")},
            {"COPILOT_API_URL": f"http://127.0.0.1:{self.cfg['copilotPort']}"},
        )

    def test_copilot_instances_share_across_model_and_client_env(self) -> None:
        first = self.run_launcher("--model", "grok-4.6", command="copilot-headroom", mode="traffic")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.run_launcher(
            "--model",
            "gpt-6-astra",
            "--reasoning-effort",
            "high",
            command="copilot-headroom",
            mode="traffic",
            env={
                "OPENAI_API_KEY": "fake-other-key",
                "GITHUB_TOKEN": "fake-github-token",
                "MallocNanoZone": "0",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "SSL_CERT_FILE": str(self.root / "missing.pem"),
            },
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Reusing", second.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)

    def test_copilot_identity_ignores_interpreter_noise(self) -> None:
        auth = kit_proxy.CopilotAuth("https://api.githubcopilot.com", "fake-refresh")
        first = kit_proxy.identity(TESTED_HEADROOM, "copilot", {"OPENAI_API_KEY": "one"}, auth)
        second = kit_proxy.identity(
            TESTED_HEADROOM,
            "copilot",
            {
                "OPENAI_API_KEY": "two",
                "GITHUB_TOKEN": "fake",
                "MallocNanoZone": "0",
                "HOME": "/elsewhere",
            },
            auth,
        )
        self.assertEqual(first, second)
        other = kit_proxy.identity(
            TESTED_HEADROOM,
            "copilot",
            {"OPENAI_API_KEY": "one"},
            kit_proxy.CopilotAuth("https://api.githubcopilot.com", "fake-other"),
        )
        self.assertNotEqual(first, other)

    def test_copilot_auth_failure_no_proxy_or_agent(self) -> None:
        result = self.run_launcher(command="copilot-headroom", mode="auth-failure")
        self.assertEqual(result.returncode, 1)
        self.assertIn("headroom-kit copilot-auth login", result.stderr)
        self.assertNotIn("`headroom copilot-auth", result.stderr)
        self.assertNotIn("fake-private", result.stderr)
        self.assertEqual(self.events(), [])

    def test_copilot_transport_and_service_failures_do_not_blame_login(self) -> None:
        for mode, expected in (
            ("auth-transport", "transport failed"),
            ("auth-read", "transport failed"),
            ("auth-service", "service failed"),
            ("auth-rejected", "headroom-kit copilot-auth login"),
        ):
            with self.subTest(mode=mode):
                result = self.run_launcher(command="copilot-headroom", mode=mode)
                self.assertEqual(result.returncode, 1)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("fake-private", result.stderr)
                if mode != "auth-rejected":
                    self.assertNotIn("copilot-auth login", result.stderr)
                self.assertEqual(self.events(), [])

    def test_copilot_success_after_failed_candidate(self) -> None:
        result = self.run_launcher(command="copilot-headroom", mode="auth-recovery")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fake-private", result.stderr)
        self.assertIn("agent", [e["event"] for e in self.events()])

    def test_editor_isolation_preferences_port_and_shared_proxy(self) -> None:
        data = self.root / "isolated editor with spaces"
        settings = data / "User/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('// Existing preferences\n{"editor.fontSize": 19}\n')
        project = self.root / "project with spaces"
        project.mkdir()
        process = self.start(
            command="copilot-vscode-headroom",
            mode="",
            args=[str(project)],
            env={
                "HEADROOM_VSCODE_USER_DATA_DIR": str(data),
                "HEADROOM_VSCODE_CHANNEL": "stable",
            },
        )
        self.wait_for("editor", process)
        stdout, stderr = process.communicate(timeout=12)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        event = next(e for e in self.events() if e["event"] == "editor")
        self.assertEqual(
            event["args"],
            [
                "--user-data-dir",
                str(data),
                "--extensions-dir",
                str(self.root / ".vscode/extensions"),
                "--sync",
                "off",
                "--new-window",
                str(project),
            ],
        )
        self.assertEqual(event["env"], {})
        self.assertIn("Existing preferences", settings.read_text())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertIn(f"http://127.0.0.1:{self.cfg['vscodePort']}/dashboard", stderr)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_editor_failure_preserves_shared_proxy(self) -> None:
        result = self.run_launcher(command="copilot-vscode-headroom", mode="agent-failure")
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertFalse(any(e["event"] == "proxy-stop" for e in self.events()))

    def test_repeated_editor_wrapper_opens_new_window(self) -> None:
        process = self.start(command="copilot-vscode-headroom", mode="")
        self.wait_for("editor", process)
        result = self.run_launcher(command="copilot-vscode-headroom")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len([e for e in self.events() if e["event"] == "editor"]), 2)
        self.assertEqual(len([e for e in self.events() if e["event"] == "proxy-start"]), 1)

    def test_normal_editor_data_rejected_and_untouched(self) -> None:
        base = (
            self.root / "Library/Application Support"
            if sys.platform == "darwin"
            else self.root / "config"
        )
        normal = base / "Code"
        settings = normal / "User/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(self.original)
        alias = self.root / "alias"
        alias.symlink_to(normal, target_is_directory=True)
        for path in (normal, alias):
            result = self.run_launcher(
                command="copilot-vscode-headroom",
                env={"HEADROOM_VSCODE_USER_DATA_DIR": str(path)},
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(settings.read_bytes(), self.original)
        self.assertEqual(self.events(), [])

    def assert_settings_escape_rejected(
        self, link_user: bool = False, return_inside: bool = False
    ) -> None:
        normal = self.root / "normal Code/User/settings.json"
        normal.parent.mkdir(parents=True)
        original = b'{\n  "editor.fontSize": 19,\n}\n'
        normal.write_bytes(original)
        data = self.root / "isolated Headroom"
        data.mkdir()
        if link_user:
            (data / "User").symlink_to(normal.parent, target_is_directory=True)
            if return_inside:
                inside = data / "preferences.json"
                inside.write_bytes(original)
                normal.unlink()
                normal.symlink_to(inside)
        else:
            (data / "User").mkdir()
            (data / "User/settings.json").symlink_to(normal)
        result = self.run_launcher(
            command="copilot-vscode-headroom",
            mode="agent-failure",
            env={"HEADROOM_VSCODE_USER_DATA_DIR": str(data)},
        )
        self.assertEqual(normal.read_bytes(), original)
        self.assertFalse(any(e["event"] == "editor" for e in self.events()))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("settings path must stay inside", result.stderr)

    def test_editor_settings_symlink_cannot_modify_normal_settings(self) -> None:
        self.assert_settings_escape_rejected()

    def test_editor_user_symlink_cannot_modify_normal_settings(self) -> None:
        self.assert_settings_escape_rejected(link_user=True)

    def test_editor_intermediate_directory_cannot_escape_then_return_inside(self) -> None:
        self.assert_settings_escape_rejected(link_user=True, return_inside=True)

    def test_editor_isolation_arguments_cannot_be_overridden(self) -> None:
        result = self.run_launcher("--user-data-dir=/normal", command="copilot-vscode-headroom")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.events(), [])

    def desktop(self, running: bool = False, platform: str = "darwin") -> kit.Desktop:
        lookup = self.bin / "lookup-app"
        lookup.write_text(
            f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
            f"Path({str(self.root / 'lookup-args')!r}).write_text(repr(sys.argv[1:]))\n"
            + ("print('ASN:existing-app')\n" if running else "")
        )
        lookup.chmod(0o700)
        return kit.Desktop(platform=platform, lookup=str(lookup), open="/stand-in/open")

    def test_app_launch_receives_endpoint_without_ownership(self) -> None:
        events = []
        calls = []

        def proxy(
            cfg: kit.Config, version: str, kind: str, port: int, auth: kit.CopilotAuth | None
        ) -> str:
            events.append("ready")
            return f"http://127.0.0.1:{port}"

        def launch(argv: Sequence[str], env: Mapping[str, str] | None) -> int:
            calls.append(list(argv))
            return 9

        self.assertEqual(
            kit.session(
                dict(self.cfg, openDashboard=False),
                "codex-app-headroom",
                [],
                TESTED_HEADROOM,
                desktop=self.desktop(),
                start_proxy=proxy,
                launch=launch,
            ),
            9,
        )
        self.assertEqual(
            calls,
            [
                [
                    "/stand-in/open",
                    "--env",
                    f"CODEX_APP_SERVER_OPENAI_BASE_URL=http://127.0.0.1:{self.cfg['codexPort']}/v1",
                    "--env",
                    "CODEX_APP_SERVER_FORCE_CLI=1",
                    "-b",
                    "com.openai.codex",
                ]
            ],
        )
        self.assertEqual(events, ["ready"])

    def test_running_app_refused_without_quitting(self) -> None:
        with self.assertRaisesRegex(kit.KitError, "Quit Codex first"):
            kit.app_target(self.cfg, self.desktop(running=True))
        self.assertEqual(
            (self.root / "lookup-args").read_text(), repr(["find", "bundleID=com.openai.codex"])
        )

    def test_app_unsupported_platform(self) -> None:
        with self.assertRaisesRegex(kit.KitError, "macOS only"):
            kit.app_target(self.cfg, self.desktop(platform="linux"))
        self.assertFalse((self.root / "lookup-args").exists())

    def test_health_requires_version_upstream_and_ready(self) -> None:
        data = {
            "status": "healthy",
            "ready": True,
            "version": TESTED_HEADROOM,
            "config": {"openai_api_url": None},
        }
        self.assertTrue(kit_proxy.compatible(data, TESTED_HEADROOM, None))
        for bad in (
            {**data, "version": "unsupported"},
            {**data, "ready": False},
            {**data, "config": {}},
            {**data, "config": {"openai_api_url": "https://api.githubcopilot.com"}},
        ):
            self.assertFalse(kit_proxy.compatible(bad, TESTED_HEADROOM, None))


if __name__ == "__main__":
    unittest.main()

"""Opt-in real runtime check. Uses temporary state and no model credentials."""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headroom_kit.runtime import TESTED_HEADROOM  # noqa: E402


def stop_shared(package: Path, env: dict[str, str], port: int) -> None:
    result = subprocess.run(
        [str(package / "bin/headroom-kit"), "stop", str(port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    with socket.socket() as listener:
        assert listener.connect_ex(("127.0.0.1", port)) != 0


def smoke_api_clients(package: Path, env: dict[str, str], agent: Path, root: Path) -> None:
    for name in ("pi", "opencode"):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            client_port = listener.getsockname()[1]
        child_env = dict(env, SMOKE_PORT=str(client_port))
        child_env[f"HEADROOM_{name.upper()}_EXECUTABLE"] = str(agent)
        child_env[f"HEADROOM_{name.upper()}_PORT"] = str(client_port)
        result = subprocess.run(
            [str(package / "bin/headroom-kit"), "run", name, "--"],
            env=child_env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["version"] == TESTED_HEADROOM and payload["ready"] is True
        assert payload["upstream"] is None
        assert ("--extension" if name == "pi" else "--standalone") in payload["args"]
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", client_port)) == 0
        stop_shared(package, child_env, client_port)
        print(
            f"Real {name} proxy readiness, launch options, independent lifetime, explicit stop: PASS"
        )


def smoke_adapters(package: Path, env: dict[str, str], root: Path) -> None:
    python = package / "bin/python"
    if not python.is_file():
        python = package / "bin/python3"
    openssl = shutil.which("openssl")
    if not python.is_file() or openssl is None:
        raise RuntimeError("The install prefix must contain Python, and openssl must be on PATH.")
    for script in ("smoke_writer.py", "smoke_auth.py", "smoke_tls.py", "smoke_metrics.py"):
        subprocess.run(
            [str(python), "-I", str(Path(__file__).with_name(script).resolve())],
            env=dict(env, KIT_TEST_OPENSSL=openssl),
            cwd=root,
            check=True,
            timeout=300,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Install prefix containing bin/headroom-kit")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="headroom-kit-runtime-") as temporary:
        root = Path(temporary).resolve()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        agent = root / "stand-in agent"
        agent.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, urllib.request\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('opencode v2.0.3')\n"
            "    sys.exit(0)\n"
            "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
            "with opener.open('http://127.0.0.1:' + os.environ['SMOKE_PORT'] + '/health') as response:\n"
            "    health = json.load(response)\n"
            "print(json.dumps({'args': sys.argv[1:], 'version': health['version'], 'ready': health['ready'],\n"
            "                  'upstream': health['config']['openai_api_url']}))\n"
        )
        agent.chmod(0o700)
        config = root / ".codex/config.toml"
        config.parent.mkdir()
        original = 'model = "test-only"\n'
        config.write_text(original)
        env = {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(root / "config"),
            "HEADROOM_CODEX_EXECUTABLE": str(agent),
            "HEADROOM_CODEX_PORT": str(port),
            "SMOKE_PORT": str(port),
        }
        # Deliberately omit the user's environment, token files, and live ports.
        result = subprocess.run(
            [str(args.package / "bin/headroom-kit"), "--version"],
            env=env,
            cwd="/",
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert (
            "headroom-kit " in result.stdout and f"headroom-ai {TESTED_HEADROOM}" in result.stdout
        ), result.stdout
        print("Installed Kit and Headroom versions reported without a proxy: PASS")
        result = subprocess.run(
            [
                str(args.package / "bin/headroom-kit"),
                "run",
                "codex",
                "--",
                "exec",
                "--json",
                "stub only",
            ],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["version"] == TESTED_HEADROOM and payload["ready"] is True
        assert payload["upstream"] is None
        assert payload["args"] == [
            "exec",
            "--json",
            "stub only",
            "-c",
            'model_provider="openai"',
            "-c",
            f'openai_base_url="http://127.0.0.1:{port}/v1"',
        ]
        assert config.read_text() == original
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", port)) == 0
        again = subprocess.run(
            [str(args.package / "bin/headroom-kit"), "run", "codex", "--", "exec", "stub"],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert again.returncode == 0 and "Reusing" in again.stderr, again.stderr
        stop_shared(args.package, env, port)
        print(
            "Real Codex proxy readiness, routing arguments, clean JSON, configuration preservation, reuse, explicit stop: PASS"
        )
        smoke_api_clients(args.package, env, agent, root)
        smoke_adapters(args.package, env, root)
        # Home/state is discarded; cache reuse is explicit and contains no auth.
        assert not (root / ".headroom/copilot-auth.json").exists()
    return 0


if __name__ == "__main__":
    sys.exit(main())

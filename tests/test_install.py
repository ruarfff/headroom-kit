"""Install built distributions into clean environments and exercise the public CLI."""

import json
import os
import shutil
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(
    os.environ.get("HEADROOM_KIT_INSTALL") != "1",
    reason="set HEADROOM_KIT_INSTALL=1 to install wheel and sdist",
)


def _run(
    args: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _build(tmp_path: Path) -> tuple[Path, Path]:
    dist = tmp_path / "dist"
    result = _run(["uv", "build", "--out-dir", str(dist)], ROOT)
    assert result.returncode == 0, result.stderr
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(wheels) == 1 and len(sdists) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
    assert any(name.endswith("resources/pi-extension.mjs") for name in names)
    assert any(name.endswith("resources/opencode-plugin/package.json") for name in names)
    assert any(name.endswith("resources/opencode-plugin/index.js") for name in names)
    return wheels[0], sdists[0]


def _install(tmp_path: Path, artifact: Path, label: str) -> Path:
    home = tmp_path / label
    home.mkdir()
    venv = home / "venv"
    result = _run(["uv", "venv", "--python", "3.13", str(venv)], home)
    assert result.returncode == 0, result.stderr
    result = _run(
        ["uv", "pip", "install", "--python", str(venv / "bin/python"), str(artifact)], home
    )
    assert result.returncode == 0, result.stderr
    return venv


def _assert_cli(venv: Path, work: Path) -> None:
    python = venv / "bin/python"
    cli = venv / "bin/headroom-kit"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work)}
    version = _run([str(cli), "--version"], work, env)
    assert version.returncode == 0, version.stderr
    assert version.stdout.startswith("headroom-kit ")
    assert "headroom-ai 0.37.0" in version.stdout
    module = _run([str(python), "-I", "-m", "headroom_kit", "--help"], Path("/"), env)
    assert module.returncode == 0, module.stderr
    assert "headroom-kit" in module.stdout
    resources = _run(
        [
            str(python),
            "-I",
            "-c",
            "from headroom_kit.runtime import package_path; "
            "print(package_path('resources', 'pi-extension.mjs')); "
            "print(package_path('resources', 'opencode-plugin'))",
        ],
        Path("/"),
        env,
    )
    assert resources.returncode == 0, resources.stderr
    extension, plugin = resources.stdout.splitlines()
    assert Path(extension).is_file()
    assert (Path(plugin) / "package.json").is_file()
    assert str(venv) in extension


def _lifecycle(venv: Path, work: Path) -> None:
    python = str(venv / "bin/python")
    cli = str(venv / "bin/headroom-kit")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    agent = work / "agent"
    agent.write_text(
        f"#!{python}\n"
        "import json, os, sys, urllib.request\n"
        "opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n"
        "with opener.open('http://127.0.0.1:' + os.environ['SMOKE_PORT'] + '/health') as response:\n"
        "    print(json.dumps(json.load(response)))\n"
    )
    agent.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work),
        "HEADROOM_CODEX_EXECUTABLE": str(agent),
        "HEADROOM_CODEX_PORT": str(port),
        "SMOKE_PORT": str(port),
        "HEADROOM_DISABLE_KOMPRESS": "1",
        "HEADROOM_DISABLE_KOMPRESS_FALLBACK": "1",
        "HEADROOM_STARTUP_TIMEOUT": "180",
    }
    first = subprocess.run(
        [cli, "run", "codex", "--", "exec", "stub"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert first.returncode == 0, first.stderr
    assert "Started Headroom" in first.stderr
    health = json.loads(first.stdout)
    assert health["ready"] is True and health["version"] == "0.37.0"
    again = subprocess.run(
        [cli, "run", "codex", "--", "exec", "stub"],
        cwd=Path("/"),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert again.returncode == 0, again.stderr
    assert "Reusing Headroom" in again.stderr
    status = subprocess.run([cli, "status"], cwd=Path("/"), env=env, capture_output=True, text=True)
    assert status.returncode == 0 and str(port) in status.stdout
    stopped = subprocess.run(
        [cli, "stop", str(port)], cwd=Path("/"), env=env, capture_output=True, text=True, timeout=30
    )
    assert stopped.returncode == 0, stopped.stderr


def _assert_tool_auth(tmp_path: Path, artifact: Path) -> None:
    home = tmp_path / "tool-home"
    tool_dir = home / "tools"
    bin_dir = home / "bin"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    uv = shutil.which("uv")
    assert uv
    installed = _run(
        ["uv", "tool", "install", "--python", "3.13", str(artifact)],
        unrelated,
        {
            "HOME": str(home),
            "PATH": f"{Path(uv).parent}:/usr/bin:/bin",
            "UV_TOOL_DIR": str(tool_dir),
            "UV_TOOL_BIN_DIR": str(bin_dir),
        },
    )
    assert installed.returncode == 0, installed.stderr
    clean = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    missing = _run(["sh", "-c", "command -v headroom"], Path("/"), clean)
    assert missing.returncode != 0
    help_result = _run(["headroom-kit", "copilot-auth", "--help"], Path("/"), clean)
    assert help_result.returncode == 0, help_result.stderr
    assert "headroom-kit copilot-auth" in help_result.stdout
    assert "login" in help_result.stdout
    status = _run(["headroom-kit", "copilot-auth", "status"], unrelated, clean)
    assert status.returncode == 0, status.stderr
    assert "not logged in" in status.stdout


def _assert_prefix_import(venv: Path, work: Path) -> None:
    python = venv / "bin/python"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(work)}
    help_result = _run(
        [str(python), "-I", "-m", "headroom_kit", "copilot-auth", "--help"],
        Path("/"),
        env,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "headroom-kit copilot-auth" in help_result.stdout
    base = _run([str(python), "-c", "import sys; print(sys._base_executable)"], work, env)
    assert base.returncode == 0, base.stderr
    site = next((venv / "lib").glob("python*/site-packages"))
    wrapper = work / "pythonpath-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nexport PYTHONPATH="{site}"\nexec "{base.stdout.strip()}" -I "$@"\n'
    )
    wrapper.chmod(0o755)
    wrapped = _run([str(wrapper), "-m", "headroom_kit", "--help"], Path("/"), env)
    assert wrapped.returncode != 0
    assert "No module named" in wrapped.stderr
    capability = _run(
        [str(python), "-I", str(ROOT / "tests/test_code_capability.py")],
        Path("/"),
        {**env, "HF_HUB_OFFLINE": "1"},
    )
    assert capability.returncode == 0, capability.stderr + capability.stdout


def test_wheel_and_sdist_install_and_run(tmp_path: Path) -> None:
    wheel, sdist = _build(tmp_path)
    for label, artifact in (("wheel", wheel), ("sdist", sdist)):
        venv = _install(tmp_path, artifact, label)
        work = tmp_path / f"{label}-work"
        work.mkdir()
        _assert_cli(venv, work)
        if label == "wheel":
            _lifecycle(venv, work)
            _assert_tool_auth(tmp_path, artifact)
            _assert_prefix_import(venv, work)

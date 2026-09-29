"""Live share check: real CLIs, real proxies, real provider calls.

Requires the Codex, Copilot, Pi, and OpenCode CLIs on PATH. Missing tools fail
before any proxy starts. Run from a checkout with Python 3.13.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Reply with the single word ok. Do not run tools."
CLIS = ("codex", "copilot", "pi", "opencode")
CASES = (*CLIS, "pi-copilot", "opencode-copilot")
QUOTA_MARKERS = ("quota", "rate limit", "rate_limit", "too many requests", "usage limit")


def which(name: str) -> str | None:
    found = shutil.which(name)
    return os.path.abspath(found) if found else None


def python313() -> str | None:
    if sys.version_info[:2] == (3, 13):
        return sys.executable
    return which("python3.13")


def missing_tools() -> list[str]:
    missing = [name for name in CLIS if not which(name)]
    if not python313():
        missing.append("python3.13")
    return missing


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


def kit_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("Headroom Kit:")]


def provider_quota(output: str) -> bool:
    blob = output.lower()
    return any(token in blob for token in QUOTA_MARKERS)


def client_ok(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 0 or provider_quota(f"{result.stdout}\n{result.stderr}")


def share_error(
    name: str,
    first: subprocess.CompletedProcess[str],
    second: subprocess.CompletedProcess[str] | None = None,
) -> str | None:
    if "Started Headroom" not in first.stderr and "Reusing Headroom" not in first.stderr:
        return f"{name}: first launch failed (exit {first.returncode}).\n" + "\n".join(
            first.stderr.splitlines()[-30:]
        )
    if second is None:
        return None
    if "Reusing Headroom" not in second.stderr:
        return (
            f"{name}: second launch did not reuse the proxy (exit {second.returncode}).\n"
            + "\n".join(second.stderr.splitlines()[-30:])
        )
    if client_ok(first) and client_ok(second):
        return None
    return (
        f"{name}: client failed after proxy share (exit {first.returncode}/{second.returncode}).\n"
        + "\n".join((first.stderr + "\n" + second.stderr).splitlines()[-30:])
    )


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("HEADROOM_")}
    env.update(TERM="dumb", NO_COLOR="1", GIT_TERMINAL_PROMPT="0")
    if extra:
        env.update(extra)
    return env


def run_kit(
    python: str,
    defaults: Path,
    command: str,
    args: list[str],
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            python,
            "-I",
            "-m",
            "headroom_kit",
            "--config",
            str(defaults),
            "run",
            command.removesuffix("-headroom"),
            "--",
            *args,
        ],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=240,
    )


def stop(python: str, defaults: Path, env: dict[str, str], port: int) -> None:
    subprocess.run(
        [python, "-I", "-m", "headroom_kit", "stop", str(port)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def github_copilot_models(listing: str) -> bool:
    return any(line.startswith("github-copilot/") for line in listing.splitlines())


def opencode_copilot_ready(executable: str) -> bool:
    try:
        listing = subprocess.run(
            [executable, "models"], capture_output=True, text=True, timeout=30, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return github_copilot_models(listing)


def wrapper(name: str) -> str:
    if name.startswith("opencode"):
        return "opencode-headroom"
    if name.startswith("pi"):
        return "pi-headroom"
    return f"{name}-headroom"


def arguments(name: str, second: bool) -> list[str]:
    if name == "codex":
        return ["exec", "--skip-git-repo-check", "--sandbox", "read-only", PROMPT]
    if name == "copilot":
        args = ["--model", "gpt-6-astra" if second else "grok-4.6"]
        if second:
            args += ["--reasoning-effort", "high"]
        return [*args, "-p", PROMPT, "-s", "--allow-all-tools"]
    copilot_model = "gpt-6-astra" if second else "grok-4.6"
    if name == "pi-copilot":
        return [
            "--provider",
            "github-copilot",
            "--model",
            copilot_model,
            "--no-tools",
            "--no-extensions",
            "-p",
            PROMPT,
        ]
    if name == "opencode-copilot":
        return ["run", "--model", f"github-copilot/{copilot_model}", PROMPT]
    if name == "pi":
        return [
            "--provider",
            "openai-codex",
            "--model",
            "gpt-5.6-luna",
            "--no-tools",
            "--no-extensions",
            "-p",
            PROMPT,
        ]
    return ["run", "--model", "opencode/mimo-v2.5-free", PROMPT]


def check(name: str, python: str, defaults: Path, cwd: Path) -> str | None:
    env = child_env()
    try:
        first = run_kit(python, defaults, wrapper(name), arguments(name, False), env, cwd)
    except subprocess.TimeoutExpired:
        return f"{name}: first launch timed out"
    print(*kit_lines(first.stderr), sep="\n")
    error = share_error(name, first)
    if error:
        return error
    extra = (
        {"MallocNanoZone": "0", "OPENAI_API_KEY": "qa-ignored-openai-key"}
        if name == "copilot"
        else None
    )
    try:
        second = run_kit(
            python, defaults, wrapper(name), arguments(name, True), child_env(extra), cwd
        )
    except subprocess.TimeoutExpired:
        return f"{name}: second launch timed out"
    print(*kit_lines(second.stderr), sep="\n")
    error = share_error(name, first, second)
    if error:
        return error
    if first.returncode or second.returncode:
        print(f"{name}: proxy shared; provider quota is not a Kit failure", flush=True)
        return None
    print(f"{name}: two real clients, one proxy: PASS", flush=True)
    return None


def main() -> int:
    missing = missing_tools()
    if missing:
        print("QA requires these on PATH / installed:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        return 1
    python = python313()
    assert python is not None
    ports = dict(zip((*CLIS, "vscode"), unique_ports(len(CLIS) + 1), strict=True))
    with tempfile.TemporaryDirectory(prefix="headroom-kit-qa-") as temporary:
        cwd = Path(temporary).resolve()
        defaults = cwd / "defaults.json"
        defaults.write_text(
            json.dumps(
                {
                    "startupTimeout": 180,
                    "codexExecutable": which("codex"),
                    "codexPort": ports["codex"],
                    "codexAppPath": None,
                    "copilotExecutable": which("copilot"),
                    "copilotPort": ports["copilot"],
                    "piExecutable": which("pi"),
                    "piPort": ports["pi"],
                    "opencodeExecutable": which("opencode"),
                    "opencodePort": ports["opencode"],
                    "vscodeChannel": "stable",
                    "vscodeExecutable": None,
                    "vscodePort": ports["vscode"],
                    "vscodeUserDataDir": None,
                    "vscodeExtensionsDir": None,
                }
            )
        )
        env = child_env()
        failed: list[str] = []
        opencode = which("opencode")
        try:
            for name in CASES:
                if name == "opencode-copilot" and opencode and not opencode_copilot_ready(opencode):
                    print(
                        "opencode-copilot: skipped (OpenCode has no github-copilot models)",
                        flush=True,
                    )
                    continue
                error = check(name, python, defaults, cwd)
                if error:
                    print(error, file=sys.stderr, flush=True)
                    failed.append(name)
        finally:
            for port in ports.values():
                stop(python, defaults, env, port)
        if failed:
            print("QA failed: " + ", ".join(failed), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

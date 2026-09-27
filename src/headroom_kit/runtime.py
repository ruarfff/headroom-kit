"""Preserve foreground process behavior and integration defaults."""

import importlib.metadata
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Never, TypedDict

TESTED_HEADROOM = "0.37.0"
LEGACY_KEYS = ("version", "uv", "python")
ENV_OPTIONS = {
    "startupTimeout": "HEADROOM_STARTUP_TIMEOUT",
    "codexExecutable": "HEADROOM_CODEX_EXECUTABLE",
    "codexPort": "HEADROOM_CODEX_PORT",
    "codexAppPath": "HEADROOM_CODEX_APP_PATH",
    "copilotExecutable": "HEADROOM_COPILOT_EXECUTABLE",
    "copilotPort": "HEADROOM_COPILOT_PORT",
    "piExecutable": "HEADROOM_PI_EXECUTABLE",
    "piPort": "HEADROOM_PI_PORT",
    "opencodeExecutable": "HEADROOM_OPENCODE_EXECUTABLE",
    "opencodePort": "HEADROOM_OPENCODE_PORT",
    "vscodeChannel": "HEADROOM_VSCODE_CHANNEL",
    "vscodeExecutable": "HEADROOM_VSCODE_EXECUTABLE",
    "vscodePort": "HEADROOM_VSCODE_PORT",
    "vscodeUserDataDir": "HEADROOM_VSCODE_USER_DATA_DIR",
    "vscodeExtensionsDir": "HEADROOM_VSCODE_EXTENSIONS_DIR",
}


class Config(TypedDict):
    startupTimeout: int
    codexExecutable: str
    codexPort: int
    codexAppPath: str | None
    copilotExecutable: str
    copilotPort: int
    piExecutable: str
    piPort: int
    opencodeExecutable: str
    opencodePort: int
    vscodeChannel: str
    vscodeExecutable: str | None
    vscodePort: int
    vscodeUserDataDir: str | None
    vscodeExtensionsDir: str | None


type Json = str | int | float | bool | None | list[Json] | dict[str, Json]

DEFAULTS: Config = {
    "startupTimeout": 180,
    "codexExecutable": "codex",
    "codexPort": 8788,
    "codexAppPath": None,
    "copilotExecutable": "copilot",
    "copilotPort": 8787,
    "piExecutable": "pi",
    "piPort": 8790,
    "opencodeExecutable": "opencode",
    "opencodePort": 8791,
    "vscodeChannel": "stable",
    "vscodeExecutable": None,
    "vscodePort": 8787,
    "vscodeUserDataDir": None,
    "vscodeExtensionsDir": None,
}


class KitError(Exception): ...


def say(message: str) -> None:
    print(f"Headroom Kit: {message}", file=sys.stderr, flush=True)


def stop(signum: int, _frame: FrameType | None) -> Never:
    raise SystemExit(128 + signum)


def terminate(
    process: subprocess.Popen[str] | subprocess.Popen[bytes], group: bool = False
) -> None:
    if process.poll() is None:
        pid = -process.pid if group else process.pid
        try:
            os.kill(pid, signal.SIGTERM)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.kill(pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            process.wait()


def run_agent(argv: Sequence[str], env: Mapping[str, str] | None = None) -> int:
    # Same foreground process group and inherited terminal: no stdin proxying.
    child = subprocess.Popen(argv, env=env)
    try:
        code = child.wait()
        return code if code >= 0 else 128 - code
    finally:
        terminate(child)


def package_path(*parts: str) -> Path:
    resource = files("headroom_kit").joinpath(*parts)
    path = Path(str(resource))
    if not path.exists():
        raise KitError(
            "Headroom Kit resources are not available as files in this environment. "
            "Install the package; a checkout or the working directory is not used."
        )
    return path


def read_config(path: str | None) -> dict[str, Json]:
    if path is None:
        return {}
    try:
        raw = json.loads(Path(path).read_text())
    except json.JSONDecodeError as error:
        raise KitError(f"--config is not valid JSON ({error}).") from None
    if not isinstance(raw, dict):
        raise KitError("--config must be a JSON object of integration defaults.")
    return raw


def legacy_names(raw: Mapping[str, Json], env: Mapping[str, str]) -> list[str]:
    found = [key for key in LEGACY_KEYS if key in raw]
    if "HEADROOM_VERSION" in env:
        found.append("HEADROOM_VERSION")
    return found


def migration_message(found: Sequence[str]) -> str:
    return (
        f"Runtime selection is no longer supported ({', '.join(found)}). "
        "Install headroom-kit with headroom-ai 0.37.0, then remove HEADROOM_VERSION, "
        "version, uv, and python from the environment and config file. "
        "Stop an old managed proxy with headroom-kit stop <port> before launching again."
    )


def reject_legacy(raw: Mapping[str, Json], env: Mapping[str, str]) -> None:
    found = legacy_names(raw, env)
    if found:
        raise KitError(migration_message(found))


def reject_unknown(raw: Mapping[str, Json]) -> None:
    unknown = sorted(key for key in raw if key not in DEFAULTS and key not in LEGACY_KEYS)
    if unknown:
        raise KitError(
            "Unknown configuration: "
            + ", ".join(unknown)
            + ". Use the documented integration names."
        )


def merge_config(raw: Mapping[str, Json]) -> Config:
    cfg: dict[str, Json] = dict(DEFAULTS)
    cfg.update({key: value for key, value in raw.items() if key in DEFAULTS})
    cfg.update({key: os.environ[var] for key, var in ENV_OPTIONS.items() if var in os.environ})
    return cfg


def load_config(path: str | None) -> Config:
    raw = read_config(path)
    reject_legacy(raw, os.environ)
    reject_unknown(raw)
    return merge_config(raw)


def validate(cfg: Config) -> None:
    for key, variable in ENV_OPTIONS.items():
        if isinstance(cfg[key], str) and not cfg[key].strip():
            raise KitError(f"{variable} must not be empty. Unset it to use the default.")
    validate_numbers(cfg)
    if cfg["vscodeChannel"] not in ("stable", "insiders"):
        raise KitError("HEADROOM_VSCODE_CHANNEL must be stable or insiders.")


def validate_numbers(cfg: Config) -> None:
    ports = ("codexPort", "copilotPort", "vscodePort", "piPort", "opencodePort")
    for key in (*ports, "startupTimeout"):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            raise KitError(f"{ENV_OPTIONS[key]} must be an integer.") from None
        if key != "startupTimeout" and not 1 <= cfg[key] <= 65535:
            raise KitError(f"{ENV_OPTIONS[key]} must be between 1 and 65535.")
    if cfg["startupTimeout"] < 1:
        raise KitError("HEADROOM_STARTUP_TIMEOUT must be positive.")
    if cfg["codexPort"] in (cfg["copilotPort"], cfg["vscodePort"]):
        raise KitError("Codex and Copilot must use separate ports.")
    for key in ("piPort", "opencodePort"):
        if any(cfg[key] == cfg[other] for other in ports if key != other):
            raise KitError("Pi and OpenCode must use separate ports from other wrappers.")


def executable(value: str | None, variable: str) -> str:
    found = shutil.which(os.path.expanduser(value)) if value else None
    if not found:
        raise KitError(
            f"Agent not found. Install it separately or set {variable} to its executable path."
        )
    return os.path.abspath(found)


def privacy(env: Mapping[str, str]) -> dict[str, str]:
    result = dict(env)
    result.setdefault("HEADROOM_TELEMETRY", "on")
    result.update(
        HEADROOM_BEACON="off",
        HEADROOM_LOG_MESSAGES="off",
        HEADROOM_CODEX_WIRE_DEBUG="off",
        DO_NOT_TRACK="1",
    )
    result.pop("HEADROOM_LOG_FILE", None)
    result.pop("HEADROOM_VERSION", None)
    return result


def installed_headroom_version() -> str:
    try:
        version = importlib.metadata.version("headroom-ai")
    except importlib.metadata.PackageNotFoundError:
        raise KitError(
            "headroom-ai is not installed in this Python environment. "
            "Install headroom-kit so this interpreter can import headroom-ai 0.37.0. "
            "A separate headroom executable is not enough."
        ) from None
    if version != TESTED_HEADROOM:
        raise KitError(
            f"This Headroom Kit release requires headroom-ai {TESTED_HEADROOM}; found {version}. "
            "Reinstall the tested release. Kit does not select another version at launch."
        )
    return version

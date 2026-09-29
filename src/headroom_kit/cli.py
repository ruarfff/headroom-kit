"""Public command surface for the installed Headroom Kit package."""

import importlib.metadata
import signal
import subprocess
import sys
from collections.abc import Sequence

from headroom_kit.copilot import run_copilot_auth
from headroom_kit.proxy import management
from headroom_kit.runtime import (
    KitError,
    executable,
    installed_headroom_version,
    load_config,
    merge_config,
    read_config,
    run_agent,
    say,
    stop,
    validate,
)
from headroom_kit.session import preflight, session

AGENTS = {
    "codex": "codex-headroom",
    "codex-app": "codex-app-headroom",
    "copilot": "copilot-headroom",
    "copilot-vscode": "copilot-vscode-headroom",
    "pi": "pi-headroom",
    "opencode": "opencode-headroom",
}
CLI_AGENTS = ("codex-headroom", "copilot-headroom", "pi-headroom", "opencode-headroom")
GUI_AGENTS = ("codex-app-headroom", "copilot-vscode-headroom")


def main(argv: Sequence[str] | None = None) -> int:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:
        return dispatch(list(sys.argv[1:] if argv is None else argv))
    except KitError as error:
        say(str(error))
    except (OSError, subprocess.SubprocessError):
        say("A local process or file operation failed. Check paths, permissions, and disk space.")
    return 1


def dispatch(argv: list[str]) -> int:
    if not argv or argv == ["--help"] or argv == ["-h"]:
        print_help()
        return 0 if argv else 2
    if argv == ["--version"]:
        return print_version()
    config, rest = take_config(argv)
    if not rest:
        print_help()
        return 2
    command, *args = rest
    if command == "copilot-auth":
        return run_copilot_auth(args)
    if command == "status":
        return status(args)
    if command == "stop":
        return management(["stop", *args])
    if command == "run":
        return run(config, args)
    if command in ("--help", "-h"):
        print_help()
        return 0
    raise KitError(f"Unknown command {command!r}. Run headroom-kit --help.")


def take_config(argv: list[str]) -> tuple[str | None, list[str]]:
    if not argv or argv[0] != "--config":
        return None, argv
    if len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
        raise KitError("--config requires a path to a JSON object.")
    return argv[1], argv[2:]


def status(args: list[str]) -> int:
    if args:
        raise KitError("status does not take arguments. Run headroom-kit status.")
    return management(["status"])


def run(config: str | None, args: list[str]) -> int:
    if not args or args[0] in ("--help", "-h"):
        print_run_help()
        return 0
    agent, *rest = args
    command = AGENTS.get(agent)
    if command is None:
        raise KitError(
            f"Unknown agent {agent!r}. Choose {', '.join(AGENTS)}. Put agent arguments after --."
        )
    agent_args = agent_arguments(rest)
    if forwards_help(command, agent_args):
        return forward_agent(config, command, agent_args)
    cfg = load_config(config)
    validate(cfg)
    return session(cfg, command, agent_args, installed_headroom_version())


def agent_arguments(args: list[str]) -> list[str]:
    if not args or args[0] != "--":
        raise KitError("Put agent arguments after --. Example: headroom-kit run codex --")
    return args[1:]


def forwards_help(command: str, args: list[str]) -> bool:
    if command in GUI_AGENTS and args in (["--help"], ["-h"]):
        return True
    options = args[: args.index("--")] if "--" in args else args
    return command in CLI_AGENTS and any(is_help_option(command, arg) for arg in options)


def is_help_option(command: str, arg: str) -> bool:
    return arg in ("--help", "-h", "--version", "-V") or (
        arg == "-v" and command in ("pi-headroom", "opencode-headroom")
    )


def forward_agent(config: str | None, command: str, args: list[str]) -> int:
    if command in GUI_AGENTS:
        print_gui_help(command)
        return 0
    cfg = merge_config(read_config(config))
    if command == "opencode-headroom":
        binary = executable(cfg["opencodeExecutable"], "HEADROOM_OPENCODE_EXECUTABLE")
        return run_agent([binary, *args])
    binary = preflight(cfg, command, args)
    if not isinstance(binary, str):
        raise KitError("The agent executable could not be resolved.")
    return run_agent([binary, *args])


def print_version() -> int:
    print(f"headroom-kit {distribution_version('headroom-kit')}")
    print(f"headroom-ai {distribution_version('headroom-ai')}")
    return 0


def distribution_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def print_help() -> None:
    print(
        """Usage:
  headroom-kit [--config PATH] run <agent> -- <agent arguments...>
  headroom-kit copilot-auth login
  headroom-kit copilot-auth status
  headroom-kit status
  headroom-kit stop <port>
  headroom-kit --help
  headroom-kit --version

Agents: codex, codex-app, copilot, copilot-vscode, pi, opencode.

--config is a JSON object of non-secret integration defaults. HEADROOM_*
environment variables override the file, which overrides built-in defaults.
HEADROOM_VERSION and runtime-selection settings are rejected.

copilot-auth runs Headroom's Copilot login in this install. It does not
replace the headroom command and does not need a separate Headroom install.
status and stop do not need provider credentials or model downloads."""
    )


def print_run_help() -> None:
    print("Usage: headroom-kit [--config PATH] run <agent> -- <agent arguments...>")
    print("Agents: " + ", ".join(AGENTS))
    print("Agent help skips the proxy: headroom-kit run codex -- --help")


def print_gui_help(command: str) -> None:
    if command == "copilot-vscode-headroom":
        print("Usage: headroom-kit run copilot-vscode -- [path]")
    else:
        print("Usage: headroom-kit run codex-app --")
    print("Launch with a shared local Headroom proxy. Use headroom-kit status/stop to manage it.")
    print(
        "Normal Codex and VS Code launches keep their existing settings. "
        "Configure through HEADROOM_*."
    )

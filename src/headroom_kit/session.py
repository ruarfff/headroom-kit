"""Route each client for one launch without changing its normal configuration."""

import contextlib
import json
import os
import plistlib
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from headroom_kit.copilot import CopilotAuth, copilot_auth
from headroom_kit.proxy import ensure_proxy, locked
from headroom_kit.runtime import (
    Config,
    KitError,
    executable,
    package_path,
    run_agent,
)


@dataclass(frozen=True)
class Desktop:
    platform: str = sys.platform
    lookup: str = "/usr/bin/lsappinfo"
    open: str = "/usr/bin/open"


type Proxy = Callable[
    [Config, str, str, int, CopilotAuth | None],
    str,
]
type Agent = Callable[[Sequence[str], Mapping[str, str] | None], int]


def app_target(cfg: Config, desktop: Desktop = Desktop()) -> list[str]:
    if desktop.platform != "darwin":
        raise KitError("codex-app supports macOS only. Use headroom-kit run codex on Linux.")
    bundle = "com.openai.codex"
    target = ["-b", bundle]
    if cfg["codexAppPath"]:
        path = Path(cfg["codexAppPath"]).expanduser().resolve()
        try:
            with (path / "Contents/Info.plist").open("rb") as file:
                bundle = plistlib.load(file)["CFBundleIdentifier"]
        except (OSError, ValueError, KeyError):
            raise KitError(
                "HEADROOM_CODEX_APP_PATH must point to an installed macOS app bundle."
            ) from None
        target = ["-a", str(path)]
    if subprocess.run(
        [desktop.lookup, "find", f"bundleID={bundle}"], capture_output=True, text=True, check=True
    ).stdout.strip():
        raise KitError(
            "Quit Codex first, then rerun headroom-kit run codex-app. "
            "An existing app cannot receive new launch settings."
        )
    return target


def editor_paths(cfg: Config) -> tuple[Path, Path]:
    insiders = cfg["vscodeChannel"] == "insiders"
    name = "Code - Insiders" if insiders else "Code"
    base = (
        Path.home() / "Library/Application Support"
        if sys.platform == "darwin"
        else Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    )
    data = Path(cfg["vscodeUserDataDir"] or base / f"{name} Headroom").expanduser().resolve()
    # Check both channels and symlinks. Arbitrary custom normal directories cannot
    # be inferred: the documented contract requires a dedicated directory.
    for channel in ("Code", "Code - Insiders"):
        normal = (base / channel).resolve()
        if data == normal or data in normal.parents or normal in data.parents:
            raise KitError(
                "HEADROOM_VSCODE_USER_DATA_DIR must be separate from normal VS Code data."
            )
    extensions = (
        Path(
            cfg["vscodeExtensionsDir"]
            or Path.home() / (".vscode-insiders/extensions" if insiders else ".vscode/extensions")
        )
        .expanduser()
        .resolve()
    )
    editor_settings(data)
    return data, extensions


def editor_settings(data: Path) -> Path:
    """Check each path component before handing a destination to Headroom."""
    settings = data / "User/settings.json"
    try:
        if data.resolve() != data or any(
            not path.resolve().is_relative_to(data) for path in (settings.parent, settings)
        ):
            raise ValueError
        return settings.resolve()
    except (OSError, RuntimeError, ValueError):
        raise KitError(
            "The VS Code settings path must stay inside the isolated user-data directory. "
            "Remove escaping User/settings.json or User directory symlinks, or choose a dedicated directory."
        ) from None


def configure_editor(data: Path, port: int) -> None:
    # Startup can take time. Recheck immediately before the real writer.
    settings = editor_settings(data)
    try:
        from click import ClickException
        from headroom.providers.copilot import configure_vscode_proxy_settings
    except ImportError:
        raise KitError(
            "This Headroom release lacks the editor writer. "
            "Install headroom-ai 0.37.0 in this environment."
        ) from None
    try:
        with contextlib.redirect_stdout(sys.stderr):
            configure_vscode_proxy_settings(settings, f"http://127.0.0.1:{port}")
    except (ClickException, OSError, RuntimeError, TypeError, ValueError):
        raise KitError(
            "Cannot configure isolated editor settings. Check JSONC/Headroom marker conflicts, "
            "or reinstall headroom-ai 0.37.0 in this environment."
        ) from None


def check_codex_options(args: list[str]) -> None:
    options = args[: args.index("--")] if "--" in args else args
    for index, arg in enumerate(options):
        if arg in ("--oss", "--local-provider"):
            raise KitError("codex supports only the built-in OpenAI provider through headroom-kit.")
        setting = (
            args[index + 1]
            if arg in ("-c", "--config") and index + 1 < len(args)
            else arg.removeprefix("--config=")
            if arg.startswith("--config=")
            else arg[2:]
            if arg.startswith("-c") and arg != "-c"
            else ""
        )
        key = setting.split("=", 1)[0].strip()
        if key in ("model_provider", "openai_base_url") or key.startswith("model_providers."):
            raise KitError(
                "Provider and endpoint overrides conflict with headroom-kit codex routing. "
                "Use the normal codex command for custom providers."
            )


def preflight(
    cfg: Config, command: str, args: list[str], desktop: Desktop = Desktop()
) -> str | list[str] | None:
    if command == "codex-headroom":
        check_codex_options(args)
        agent = executable(cfg["codexExecutable"], "HEADROOM_CODEX_EXECUTABLE")
    elif command == "copilot-headroom":
        agent = executable(cfg["copilotExecutable"], "HEADROOM_COPILOT_EXECUTABLE")
    elif command == "pi-headroom":
        agent = executable(cfg["piExecutable"], "HEADROOM_PI_EXECUTABLE")
    elif command == "opencode-headroom":
        agent = executable(cfg["opencodeExecutable"], "HEADROOM_OPENCODE_EXECUTABLE")
        opencode_arguments(args)
        opencode_config(os.environ.get("OPENCODE_CONFIG_CONTENT", "{}"), cfg["opencodePort"])
        result = subprocess.run([agent, "--version"], capture_output=True, text=True, timeout=10)
        if result.returncode or not result.stdout.strip().removeprefix("opencode v").startswith(
            "2."
        ):
            raise KitError("opencode requires OpenCode v2 (tested with 2.0.3).")
    elif command == "codex-app-headroom":
        if args:
            raise KitError("Use headroom-kit run codex-app without arguments, or --help.")
        agent = app_target(cfg, desktop)
    elif command == "copilot-vscode-headroom":
        if len(args) > 1 or (args and args[0].startswith("-")):
            raise KitError(
                "Use headroom-kit run copilot-vscode -- [path]. Configure isolation through HEADROOM_VSCODE_*."
            )
        agent = executable(
            cfg["vscodeExecutable"]
            or ("code-insiders" if cfg["vscodeChannel"] == "insiders" else "code"),
            "HEADROOM_VSCODE_EXECUTABLE",
        )
        editor_paths(cfg)
    else:
        agent = None
    return agent


def codex_arguments(args: list[str], endpoint: str) -> list[str]:
    # Codex 0.154.0 replaces global -c values when a subcommand supplies -c.
    # Collect all overrides into the final option scope, before a literal --.
    options = []
    overrides = []
    index = 0
    while index < len(args) and args[index] != "--":
        arg = args[index]
        if arg in ("-c", "--config"):
            if index + 1 == len(args):
                raise KitError(f"{arg} requires a key=value argument.")
            index += 1
            overrides += ["-c", args[index]]
        elif arg.startswith("--config="):
            overrides += ["-c", arg.removeprefix("--config=")]
        elif arg.startswith("-c"):
            overrides += ["-c", arg[2:]]
        else:
            options.append(arg)
        index += 1
    return [
        *options,
        *overrides,
        "-c",
        'model_provider="openai"',
        "-c",
        f'openai_base_url="{endpoint}"',
        *args[index:],
    ]


def client_environment(env: Mapping[str, str]) -> dict[str, str]:
    """Keep local client traffic direct without changing upstream proxy policy."""
    child = dict(env)
    exclusions = [
        entry.strip()
        for key in ("NO_PROXY", "no_proxy")
        for entry in env.get(key, "").split(",")
        if entry.strip()
    ]
    if "*" in exclusions:
        # Some clients require an exact "*"; Codex also needs proxy variables removed.
        for key in ("http_proxy", "https_proxy", "all_proxy"):
            child.pop(key, None)
            child.pop(key.upper(), None)
        child.update(NO_PROXY="*", no_proxy="*")
        return child
    bypass = ",".join(dict.fromkeys([*exclusions, "127.0.0.1", "localhost", "::1"]))
    child.update(NO_PROXY=bypass, no_proxy=bypass)
    return child


def with_options(args: list[str], options: list[str]) -> list[str]:
    end = args.index("--") if "--" in args else len(args)
    return [*args[:end], *options, *args[end:]]


def opencode_arguments(args: list[str]) -> list[str]:
    options = args[: args.index("--")] if "--" in args else args
    if any(
        arg.split("=", 1)[0] in ("--server", "--standalone", "--no-standalone") for arg in options
    ):
        raise KitError("opencode manages its own private server. Omit server flags.")
    remaining = iter(options)
    for arg in remaining:
        if arg in ("--log-level", "--prompt", "--session", "-s", "--completions"):
            next(remaining, None)
        elif not arg.startswith("-"):
            if arg not in ("run", "mini") and not arg.startswith((".", "/", "~")):
                raise KitError(
                    "Use headroom-kit run opencode -- [./directory], run, or mini. Use normal opencode for other commands."
                )
            break
    return with_options(args, ["--standalone"])


def opencode_config(content: str, port: int, copilot: str | None = None) -> str:
    try:
        config = json.loads(content)
        if not isinstance(config, dict) or not isinstance(config.get("plugins", []), list):
            raise ValueError
    except ValueError:
        raise KitError(
            "OPENCODE_CONFIG_CONTENT must be a JSON object with a plugins array if present. Use a JSONC file for comments."
        ) from None
    options = {"endpoint": f"http://127.0.0.1:{port}/v1"}
    if copilot:
        options["copilot"] = copilot
    config["plugins"] = [
        *config.get("plugins", []),
        {
            "package": str(package_path("resources", "opencode-plugin")),
            "options": options,
        },
    ]
    return json.dumps(config)


def selected_provider(args: list[str]) -> str | None:
    options = args[: args.index("--")] if "--" in args else args
    provider = None
    from_model = None
    index = 0
    while index < len(options):
        arg = options[index]
        if arg == "--provider" and index + 1 < len(options):
            provider = options[index + 1]
            index += 2
            continue
        if arg.startswith("--provider="):
            provider = arg.removeprefix("--provider=")
        elif arg == "--model" and index + 1 < len(options):
            from_model = options[index + 1].split("/", 1)[0]
            index += 2
            continue
        elif arg.startswith("--model="):
            from_model = arg.removeprefix("--model=").split("/", 1)[0]
        index += 1
    return provider or from_model


def copilot_endpoint(
    cfg: Config,
    version: str,
    args: list[str],
    authorize: Callable[[], CopilotAuth],
    start_proxy: Proxy,
) -> str | None:
    provider = selected_provider(args)
    if provider in {"openai", "openai-codex", "anthropic", "opencode"}:
        return None
    try:
        auth = authorize()
    except KitError:
        if provider == "github-copilot":
            raise
        return None
    return start_proxy(cfg, version, "copilot", cfg["copilotPort"], auth) + "/v1"


def session(
    cfg: Config,
    command: str,
    args: list[str],
    version: str,
    *,
    desktop: Desktop = Desktop(),
    authorize: Callable[[], CopilotAuth] = copilot_auth,
    start_proxy: Proxy = ensure_proxy,
    launch: Agent = run_agent,
) -> int:
    agent = preflight(cfg, command, args, desktop)
    os.umask(0o077)
    if command in ("pi-headroom", "opencode-headroom"):
        kind = command.removesuffix("-headroom")
        port = cfg[f"{kind}Port"]
        copilot = copilot_endpoint(cfg, version, args, authorize, start_proxy)
        endpoint = start_proxy(cfg, version, kind, port, None)
        env = client_environment(os.environ)
        if kind == "pi":
            env["HEADROOM_KIT_ENDPOINT"] = endpoint + "/v1"
            if copilot:
                env["HEADROOM_KIT_COPILOT_ENDPOINT"] = copilot
            args = with_options(
                args, ["--extension", str(package_path("resources", "pi-extension.mjs"))]
            )
        else:
            env["OPENCODE_CONFIG_CONTENT"] = opencode_config(
                env.get("OPENCODE_CONFIG_CONTENT", "{}"), port, copilot
            )
            args = opencode_arguments(args)
        return launch([agent, *args], env)
    if command.startswith("codex"):
        endpoint = start_proxy(cfg, version, "codex", cfg["codexPort"], None) + "/v1"
        if command == "codex-headroom":
            return launch([agent, *codex_arguments(args, endpoint)], client_environment(os.environ))
        target = app_target(cfg, desktop)
        return launch(
            [
                desktop.open,
                "--env",
                f"CODEX_APP_SERVER_OPENAI_BASE_URL={endpoint}",
                "--env",
                "CODEX_APP_SERVER_FORCE_CLI=1",
                *target,
            ],
            None,
        )
    port = cfg["copilotPort"] if command == "copilot-headroom" else cfg["vscodePort"]
    auth = authorize()
    endpoint = start_proxy(cfg, version, "copilot", port, auth)
    if command == "copilot-headroom":
        env = client_environment(os.environ)
        # Keep Copilot's catalog, auto selection, and per-model wire routing.
        # Any inherited BYOK setting can put it back in the single-model lane.
        env = {key: value for key, value in env.items() if not key.startswith("COPILOT_PROVIDER_")}
        env["COPILOT_API_URL"] = endpoint
        return launch([agent, *args], env)
    data, extensions = editor_paths(cfg)
    with locked(
        data / ".headroom-launcher.lock",
        "Timed out waiting for editor startup.",
        cfg["startupTimeout"],
    ):
        configure_editor(data, port)
        env = client_environment(os.environ)
        for key in ("VSCODE_IPC_HOOK_CLI", "VSCODE_PORTABLE"):
            env.pop(key, None)
        return launch(
            [
                agent,
                "--user-data-dir",
                str(data),
                "--extensions-dir",
                str(extensions),
                "--sync",
                "off",
                "--new-window",
                str(Path(args[0] if args else ".").resolve()),
            ],
            env,
        )

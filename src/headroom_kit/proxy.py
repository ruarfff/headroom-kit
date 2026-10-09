"""Own shared proxy startup, compatibility, control, and subscription authentication."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import runpy
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from headroom_kit.compression import protect_unretrievable_routes
from headroom_kit.copilot import LOGIN_COMMAND, CopilotAuth, managed_copilot_auth
from headroom_kit.runtime import Config, Json, KitError, privacy, say, terminate

# Compression controls only. Routing, auth, learning, and process settings stay
# under Kit's control; profile values come from the resolved Headroom release.
COMPRESSION_ENV = (
    "HEADROOM_SAVINGS_PROFILE",
    "HEADROOM_SAVINGS_TARGET",
    "HEADROOM_MODE",
    "HEADROOM_COMPRESSORS",
    "HEADROOM_TARGET_RATIO",
    "HEADROOM_MIN_TOKENS",
    "HEADROOM_MIN_CHARS_FOR_BLOCK",
    "HEADROOM_MAX_ITEMS",
    "HEADROOM_SMART_CRUSHER_COMPACTION",
    "HEADROOM_ACCURACY_GUARD",
    "HEADROOM_CODE_AWARE_ENABLED",
    "HEADROOM_DEDUPE",
    "HEADROOM_TOOL_SEARCH",
    "HEADROOM_TOOL_DESC_MAX_CHARS",
    "HEADROOM_EXCLUDE_TOOLS",
    "HEADROOM_NO_CCR",
    "HEADROOM_CCR_INLINE_RESOLVE",
    "HEADROOM_NO_CCR_PROACTIVE_EXPANSION",
    "HEADROOM_OUTPUT_SHAPER",
    "HEADROOM_EFFORT_ROUTER",
    "HEADROOM_VERBOSITY_AUTOTUNE",
)
COMPRESSION_PREFIXES = (
    "HEADROOM_COMPRESS_",
    "HEADROOM_COMPRESSION_",
    "HEADROOM_PROTECT_",
    "HEADROOM_LOSSLESS",
    "HEADROOM_LOSSY_",
    "HEADROOM_KOMPRESS_",
    "HEADROOM_DISABLE_KOMPRESS",
    "HEADROOM_FORCE_KOMPRESS",
)
METRICS_ENV = (
    "HEADROOM_STATELESS",
    "HEADROOM_TELEMETRY",
    "HEADROOM_WORKSPACE_DIR",
    "HEADROOM_SAVINGS_PATH",
    "HEADROOM_SAVINGS_EVENTS_PATH",
)


def health(port: int) -> dict[str, Json]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            data = json.loads(response.read(65536))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def compatible(data: dict[str, Json], version: str, upstream: str | None) -> bool:
    config = data.get("config")
    return (
        isinstance(config, dict)
        and data.get("status") == "healthy"
        and data.get("ready") is True
        and data.get("version") == version
        and "openai_api_url" in config
        and config["openai_api_url"] == upstream
    )


def port_free(port: int) -> bool:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


@contextlib.contextmanager
def locked(path: Path, message: str, timeout: float = 0) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as lock:
        deadline = time.monotonic() + timeout
        while not acquire(lock.fileno()):
            if time.monotonic() >= deadline:
                raise KitError(message)
            time.sleep(0.05)
        yield


def proxy_environment(kind: str, port: int, inherited: Mapping[str, str]) -> dict[str, str]:
    from headroom.agent_savings import apply_agent_savings_env_defaults

    blocked = {"OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "PYTHONPATH", "PYTHONHOME"}
    env = {
        key: value
        for key, value in inherited.items()
        if key not in blocked
        and (
            key in METRICS_ENV
            or key in COMPRESSION_ENV
            or key.startswith(COMPRESSION_PREFIXES)
            or not key.startswith(("HEADROOM_", "GITHUB_COPILOT_"))
        )
        and not key.endswith("TARGET_API_URL")
    }
    env = privacy(env)
    # Headroom reads this name as its own build override. Version selection is gone.
    env.pop("HEADROOM_VERSION", None)
    # Resolve storage before the detached owner changes its working directory to /.
    workspace = Path(env.get("HEADROOM_WORKSPACE_DIR", "").strip() or "~/.headroom")
    workspace = workspace.expanduser().resolve()
    for key, default in (
        ("HEADROOM_WORKSPACE_DIR", workspace),
        ("HEADROOM_SAVINGS_PATH", workspace / "headroom-kit" / str(port) / "proxy_savings.json"),
        ("HEADROOM_SAVINGS_EVENTS_PATH", workspace / "savings_events.jsonl"),
    ):
        env[key] = str(Path(env.get(key, "").strip() or default).expanduser().resolve())
    # Headroom's allocator re-exec would discard the inherited-socket/auth adapter.
    env.update(HEADROOM_AGENT_TYPE=kind, HEADROOM_MALLOC_TUNING="0")
    if sys.platform == "darwin":
        env.setdefault("MallocAggressiveMadvise", "1")
        env.setdefault("MallocLargeCache", "0")
    # Click reads envvars while parsing, before the proxy callback runs. Seed
    # here, before both the compatibility hash and the isolated CLI subprocess.
    apply_agent_savings_env_defaults(env)
    return env


def proxy_args(kind: str, port: int, upstream: str | None) -> list[str]:
    args = [
        "proxy",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-learn",
    ]
    if kind != "copilot":
        args += ["--no-cache", "--no-rate-limit"]
    else:
        args += ["--openai-api-url", upstream, "--anthropic-api-url", upstream]
    return args


def runtime_dir() -> Path:
    # One namespace per OS user and actual port, independent of HOME/XDG/TMPDIR.
    path = Path("/tmp") / f"headroom-kit-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise KitError("The shared Kit runtime directory must be owned by you with mode 0700.")
    return path


def acquire(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def control(port: int, action: str = "status", instance: str | None = None) -> dict[str, Json]:
    path = runtime_dir() / f"{port}.sock"
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise KitError(f"Unsafe control socket on port {port}.")
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(12 if action == "stop" else 1)
            client.connect(str(path))
            client.sendall(json.dumps({"action": action, "instance": instance}).encode() + b"\n")
            with client.makefile("rb") as stream:
                result = json.loads(stream.readline(65536))
            return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def identity(version: str, kind: str, env: dict[str, str], auth: CopilotAuth | None) -> str:
    implementation = hashlib.sha256(
        b"".join(
            path.read_bytes()
            for path in sorted(Path(__file__).parent.rglob("*"))
            if path.suffix in (".py", ".mjs", ".js", ".json")
        )
    ).hexdigest()
    # Copilot clients share one subscription proxy. Model, interpreter, and pane env
    # must not mint a new identity. Effective native settings apply to every proxy.
    # Other agents also hash upstream credentials.
    payload: list[Json] = [version, implementation, kind]
    if kind == "copilot":
        payload.append([auth.api_url, auth.refresh_oauth_token] if auth else None)
        payload.append({key: value for key, value in env.items() if key.startswith("HEADROOM_")})
    else:
        payload.append(
            {
                key: value
                for key, value in env.items()
                if (
                    key.lower().endswith("_proxy")
                    or key.endswith(("_API_KEY", "_API_TOKEN", "_TARGET_API_URL"))
                    or key.startswith(("HEADROOM_", "OPENAI_", "ANTHROPIC_", "AZURE_", "SSL_"))
                    or key in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
                )
            }
        )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def ensure_proxy(
    cfg: Config, version: str, kind: str, port: int, auth: CopilotAuth | None = None
) -> str:
    env = proxy_environment(kind, port, os.environ)
    if auth and not auth.refresh_oauth_token:
        raise KitError(f"Shared Copilot requires reusable OAuth. Run `{LOGIN_COMMAND}`.")
    fingerprint = identity(version, kind, env, auth)
    deadline = time.monotonic() + cfg["startupTimeout"]
    owner = None
    lock_path = runtime_dir() / f"{port}.lock"
    with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a") as lock:
        while time.monotonic() < deadline:
            state = control(port)
            if state:
                if state.get("identity") != fingerprint:
                    raise KitError(
                        f"Port {port} has an incompatible managed proxy. Stop it explicitly or choose another port."
                    )
                if state.get("ready"):
                    say(f"{'Started' if owner else 'Reusing'} Headroom {version} on port {port}.")
                    say(f"Dashboard: http://127.0.0.1:{port}/dashboard")
                    return f"http://127.0.0.1:{port}"
            if owner is not None and owner.poll() is not None:
                raise KitError(
                    f"Headroom {version} exited before readiness on port {port}. The agent was not launched."
                )
            if owner is None and acquire(lock.fileno()):
                owner = spawn_owner(cfg, version, kind, port, auth, env, fingerprint, lock.fileno())
            time.sleep(0.05)
    raise KitError(
        f"Headroom did not become ready on port {port} within the startup deadline. The agent was not launched."
    )


def spawn_owner(
    cfg: Config,
    version: str,
    kind: str,
    port: int,
    auth: CopilotAuth | None,
    env: dict[str, str],
    fingerprint: str,
    lock: int,
) -> subprocess.Popen[bytes]:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise KitError(
                f"Port {port} is occupied by an unrelated or incompatible listener. It was not changed."
            ) from None
        listener.listen()
        if auth:
            env.update(
                GITHUB_COPILOT_API_TOKEN="headroom-kit-expired-seed",
                GITHUB_COPILOT_API_URL=auth.api_url,
                GITHUB_COPILOT_REFRESH_OAUTH_TOKEN=auth.refresh_oauth_token,
                GITHUB_COPILOT_API_TOKEN_EXPIRES_AT="0",
            )
        child = subprocess.Popen(
            [sys.executable, "-I", "-m", "headroom_kit._owner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            start_new_session=True,
            pass_fds=(lock, listener.fileno()),
            umask=0o077,
        )
        payload = dict(
            version=version,
            kind=kind,
            port=port,
            identity=fingerprint,
            upstream=auth.api_url if auth else None,
            timeout=cfg["startupTimeout"],
            listener=listener.fileno(),
        )
        child.stdin.write(json.dumps(payload).encode())
        child.stdin.close()
        return child


def owner_main() -> int:
    payload = json.load(sys.stdin)
    port = payload["port"]
    path = runtime_dir() / f"{port}.sock"
    # The inherited lifetime lock proves that any previous socket is abandoned.
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise KitError("Unsafe stale control socket.")
        path.unlink()
    with socket.socket(socket.AF_UNIX) as server:
        process = None
        try:
            server.bind(str(path))
            server.listen()
            server.settimeout(0.2)
            # Defer owner signals until the child can be cleaned up. This owner has no threads.
            previous = signal.pthread_sigmask(
                signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            )
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        "-m",
                        "headroom_kit._serve",
                        str(payload["listener"]),
                        *proxy_args(payload["kind"], port, payload["upstream"]),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(payload["listener"],),
                    start_new_session=True,
                    preexec_fn=partial(signal.pthread_sigmask, signal.SIG_SETMASK, previous),
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            os.close(payload["listener"])
            state = {key: payload[key] for key in ("port", "version", "kind", "identity")}
            state.update(instance=uuid.uuid4().hex, ready=False)
            serve_control(server, process, state, payload["upstream"], payload["timeout"])
        finally:
            cleanup_proxy(process, path)
    return 0


def serve_control(
    server: socket.socket,
    process: subprocess.Popen[bytes],
    state: dict[str, Json],
    upstream: str | None,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    started = False
    while process.poll() is None:
        if not started:
            started = compatible(health(state["port"]), state["version"], upstream)
            state["ready"] = started
            if not started and time.monotonic() >= deadline:
                raise KitError("Shared proxy startup timed out.")
        try:
            client, _ = server.accept()
        except TimeoutError:
            continue
        with client:
            client.settimeout(1)
            try:
                with client.makefile("rb") as stream:
                    request = json.loads(stream.readline(65536))
                stopping = (
                    request.get("action") == "stop" and request.get("instance") == state["instance"]
                )
                if stopping:
                    terminate(process, group=True)
                    state["ready"] = False
                elif not started:
                    started = compatible(health(state["port"]), state["version"], upstream)
                    state["ready"] = started
                client.sendall(json.dumps(dict(state, stopped=stopping)).encode() + b"\n")
                if stopping:
                    return
            except (OSError, ValueError, AttributeError):
                continue


def cleanup_proxy(process: subprocess.Popen[bytes] | None, path: Path) -> None:
    previous = {
        s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    try:
        if process is not None:
            terminate(process, group=True)
        path.unlink(missing_ok=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def serve_main() -> int:
    import uvicorn

    native_run = uvicorn.run

    def run(app: "FastAPI", **options: Json) -> None:
        protect_unretrievable_routes(app)
        native_run(app, **options)

    if os.environ.get("HEADROOM_AGENT_TYPE") == "copilot":
        managed_copilot_auth()
    # Reserve the listening socket before spawning. Health can never certify a
    # foreign process that wins a check-then-bind race.
    uvicorn.run = partial(run, fd=int(sys.argv[1]))
    sys.argv = ["headroom", *sys.argv[2:]]
    runpy.run_module("headroom.cli", run_name="__main__")
    return 0


def management(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="headroom-kit")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    stop = commands.add_parser("stop", help="Interrupt all clients using the selected proxy")
    stop.add_argument("port", type=int)
    options = parser.parse_args(args)
    if options.action == "status":
        for path in sorted(runtime_dir().glob("*.sock")):
            state = control(int(path.stem))
            if state:
                print(
                    f"{state['port']} {state['kind']} Headroom {state['version']} ready={state['ready']} instance={state['instance']}"
                )
            else:
                print(f"{path.stem} unavailable (starting or stale)")
        return 0
    if not 1 <= options.port <= 65535:
        parser.error("port must be between 1 and 65535")
    state = control(options.port)
    if not state:
        raise KitError(
            f"No responsive managed proxy on port {options.port}; no process was stopped."
        )
    result = control(options.port, "stop", state["instance"])
    if not result.get("stopped"):
        raise KitError("The managed instance changed or did not confirm shutdown. Retry status.")
    say(f"Stopped shared proxy on port {options.port}; attached clients are interrupted.")
    return 0

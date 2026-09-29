"""Executable stand-ins: no real agents, accounts, or model endpoints."""

import io
import json
import os
import runpy
import signal
import socket
import sys
import time
import types
import urllib.error
import urllib.request
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import FrameType
from typing import Never

type Json = str | int | float | bool | None | list[Json] | dict[str, Json]

ROOT = Path(os.environ["KIT_TEST_ROOT"])
MODE = os.environ.get("KIT_TEST_MODE", "")
TESTED_HEADROOM = os.environ["KIT_TEST_HEADROOM_VERSION"]
PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


def record(event: str, **values: Json) -> None:
    with (ROOT / "events").open("a") as file:
        file.write(json.dumps(dict(event=event, **values)) + "\n")


def run_agent(name: str) -> int:
    is_editor = name.startswith("code") and name != "codex"
    event = "editor" if is_editor else "agent"
    record(
        event,
        args=sys.argv[1:],
        proxy_env={key: os.environ[key] for key in PROXY_VARIABLES if key in os.environ},
        pid=os.getpid(),
        cwd=os.getcwd(),
        tty=os.isatty(0),
        env={
            k: os.environ[k]
            for k in os.environ
            if k.startswith("COPILOT_PROVIDER_")
            or k
            in (
                "COPILOT_API_URL",
                "COPILOT_MODEL",
                "HEADROOM_KIT_ENDPOINT",
                "HEADROOM_KIT_COPILOT_ENDPOINT",
                "OPENCODE_CONFIG_CONTENT",
                "VSCODE_IPC_HOOK_CLI",
                "VSCODE_PORTABLE",
            )
        },
    )
    if name == "opencode" and sys.argv[1:] == ["--version"]:
        print(os.environ.get("KIT_TEST_OPENCODE_VERSION", "opencode v2.0.3"))
        return 0
    if not is_editor:
        print("AGENT_OUTPUT", flush=True)
    if MODE == "stdin":
        print(sys.stdin.read(), end="")
    if MODE in ("traffic", "wait-traffic"):
        client_traffic(name)
    if MODE in ("wait", "wait-traffic"):
        while True:
            if MODE == "wait-traffic":
                client_traffic(name)
            if (
                os.environ.get("KIT_TEST_EXIT_FILE")
                and Path(os.environ["KIT_TEST_EXIT_FILE"]).exists()
            ):
                break
            time.sleep(0.1)
    return 37 if MODE == "agent-failure" else 0


def client_traffic(name: str) -> None:
    endpoint = os.environ.get("COPILOT_API_URL") or os.environ.get("HEADROOM_KIT_ENDPOINT")
    if name == "codex":
        endpoint = next(
            arg.split("=", 1)[1].strip('"')
            for arg in sys.argv
            if arg.startswith("openai_base_url=")
        )
    elif name == "opencode":
        endpoint = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])["plugins"][-1]["options"][
            "endpoint"
        ]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(endpoint + "/responses", data=b"{}", method="POST")
    with opener.open(request, timeout=2) as response:
        record(
            "client-response",
            client=os.environ.get("KIT_TEST_CLIENT", "default"),
            **json.load(response),
        )


def bind_standin(
    port: int, handler: type[HTTPServer], inherited: bool, fd: int | None
) -> HTTPServer:
    if not inherited:
        return HTTPServer(("127.0.0.1", port), handler)
    server = HTTPServer(("127.0.0.1", port), handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=fd if fd is not None else -1)
    server.server_address = server.socket.getsockname()
    return server


def serve_proxy() -> int:
    # Managed: headroom_kit._serve <fd> <args>. Foreign: headroom.cli proxy --port N.
    inherited = sys.argv[0].endswith("_serve")
    if inherited:
        fd = int(sys.argv[1])
        args = sys.argv[2:]
    else:
        fd = None
        args = sys.argv[1:]
    if args == ["--version"]:
        print("headroom " + os.environ.get("HEADROOM_VERSION", TESTED_HEADROOM))
        return 0
    record(
        "proxy-start",
        pid=os.getpid(),
        args=args,
        proxy_env={key: os.environ[key] for key in PROXY_VARIABLES if key in os.environ},
        cwd=os.getcwd(),
        env={
            k: os.environ.get(k)
            for k in (
                "HEADROOM_MALLOC_TUNING",
                "HEADROOM_BEACON",
                "HEADROOM_TELEMETRY",
                "HEADROOM_LOG_MESSAGES",
                "HEADROOM_STATELESS",
                "HEADROOM_WORKSPACE_DIR",
                "HEADROOM_SAVINGS_PATH",
                "HEADROOM_SAVINGS_EVENTS_PATH",
                "HEADROOM_SAVINGS_PROFILE",
                "HEADROOM_COMPRESSORS",
                "HEADROOM_MODE",
                "HEADROOM_LOSSLESS",
                "HEADROOM_DISABLE_KOMPRESS",
                "HEADROOM_DISABLE_KOMPRESS_FALLBACK",
                "HEADROOM_HOST",
                "HEADROOM_WORKERS",
                "HEADROOM_BACKEND",
                "HEADROOM_PROXY_TOKEN",
                "HEADROOM_MODEL_ROUTER_ENABLED",
                "OPENAI_TARGET_API_URL",
                "HEADROOM_OUTPUT_SHAPER",
                "HEADROOM_EFFORT_ROUTER",
                "HEADROOM_VERBOSITY_AUTOTUNE",
                "DO_NOT_TRACK",
            )
        },
    )
    print("fake-private-proxy-diagnostic", flush=True)
    if MODE == "startup-failure":
        return 7
    port = int(args[args.index("--port") + 1])
    upstream = args[args.index("--openai-api-url") + 1] if "--openai-api-url" in args else None
    if MODE == "wrong-upstream":
        upstream = "https://unrelated.example.invalid"
    ready_at = time.monotonic() + float(os.environ.get("KIT_TEST_DELAY", "0.15"))

    count = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            nonlocal count
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            count += 1
            data = json.dumps({"instance": os.getpid(), "count": count}).encode()
            probe = next(
                (
                    value
                    for value in ("shared-probe-one", "shared-probe-two")
                    if value.encode() in body
                ),
                "auxiliary",
            )
            record("proxy-request", instance=os.getpid(), count=count, probe=probe)
            if MODE == "real-routing":
                data = b'{"error":{"type":"invalid_request_error","message":"local routing test"}}'
            self.send_response(400 if MODE == "real-routing" else 200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            data = json.dumps(
                {
                    "status": "healthy",
                    "ready": MODE != "not-ready" and time.monotonic() >= ready_at,
                    "version": os.environ.get("KIT_TEST_VERSION", TESTED_HEADROOM),
                    "config": {"openai_api_url": upstream},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: str | int) -> None:
            pass

    server = bind_standin(port, Handler, inherited, fd)

    def stop(signum: int, frame: FrameType | None) -> Never:
        server.server_close()
        record("proxy-stop")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    server.serve_forever()
    return 0


class TruncatedResponse(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        raise IncompleteRead(b"fake-private-partial-body", 1000)


def auth_urlopen(request: urllib.request.Request, *, timeout: float) -> TruncatedResponse:
    if MODE == "auth-read":
        return TruncatedResponse()
    if MODE in ("auth-service", "auth-rejected"):
        raise urllib.error.HTTPError(
            request.full_url,
            503 if MODE == "auth-service" else 401,
            "fake-private-response",
            {},
            None,
        )
    raise ConnectionError("fake-private-transport")


def patch_versions() -> None:
    import importlib.metadata as metadata

    original = metadata.version

    def version(name: str) -> str:
        if name == "headroom-ai":
            return TESTED_HEADROOM
        if name == "headroom-kit":
            return "0.0.0+test"
        return original(name)

    metadata.version = version


def subscription_auth(fake: types.ModuleType) -> types.SimpleNamespace | None:
    if MODE == "auth-failure":
        print("fake-private-auth-diagnostic")
        raise ValueError("fake-private-auth-diagnostic")
    if MODE in ("auth-transport", "auth-service", "auth-rejected", "auth-recovery", "auth-read"):
        try:
            with fake._urlopen(
                urllib.request.Request("https://example.invalid"), timeout=1
            ) as response:
                response.read()
        except (OSError, IncompleteRead):
            if MODE != "auth-recovery":
                return None
    return types.SimpleNamespace(
        api_url="https://api.githubcopilot.com",
        token=os.environ.get("KIT_TEST_ACCESS_TOKEN", "fake-test-token"),
        refresh_oauth_token=os.environ.get("KIT_TEST_ACCOUNT", "fake-refresh-account-one"),
        api_token_expires_at=None,
    )


def install_editor_fake() -> None:
    click = types.ModuleType("click")
    click.ClickException = ValueError
    sys.modules["click"] = click
    provider = types.ModuleType("headroom.providers.copilot")

    def configure(path: Path, endpoint: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = path.read_text() if path.exists() else "{}"
        path.write_text(content + "\n// Fake Headroom block: " + endpoint)

    provider.configure_vscode_proxy_settings = configure
    sys.modules["headroom.providers.copilot"] = provider


def install_fakes(interpreter: str) -> None:
    patch_versions()
    sys.executable = interpreter
    fake = types.ModuleType("headroom.copilot_auth")
    fake.resolve_subscription_bearer_token_details = lambda: subscription_auth(fake)
    fake._urlopen = auth_urlopen
    headroom = types.ModuleType("headroom")
    headroom.copilot_auth = fake
    savings = types.ModuleType("headroom.agent_savings")

    def apply_defaults(env: dict[str, str]) -> None:
        # Only a call sentinel; smoke_compression.py checks the real profile.
        env.setdefault("HEADROOM_SAVINGS_PROFILE", "coding")

    savings.apply_agent_savings_env_defaults = apply_defaults
    sys.modules["headroom.agent_savings"] = savings
    proxy = types.ModuleType("headroom.proxy")
    proxy.ssl_context = types.SimpleNamespace(build_httpx_verify=lambda: True)
    sys.modules["headroom"] = headroom
    sys.modules["headroom.proxy"] = proxy
    sys.modules["headroom.copilot_auth"] = fake
    install_editor_fake()


def interrupt_owner_startup(frame: FrameType, event: str, arg: object) -> object:
    if (
        event == "return"
        and frame.f_code.co_name == "__init__"
        and frame.f_globals.get("__name__") == "subprocess"
        and frame.f_back is not None
        and frame.f_back.f_code.co_name == "owner_main"
    ):
        sys.settrace(None)
        record("proxy-spawned", pid=frame.f_locals["self"].pid)
        os.kill(os.getpid(), int(os.environ["KIT_TEST_OWNER_SIGNAL"]))
    return interrupt_owner_startup


def delegate_module() -> int:
    interpreter = str(Path(sys.argv[0]).resolve())
    src = os.environ.get("KIT_TEST_SRC")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    module = sys.argv[sys.argv.index("-m") + 1]
    rest = sys.argv[sys.argv.index("-m") + 2 :]
    sys.argv = [module, *rest]
    if module == "headroom_kit._serve" or not module.startswith("headroom_kit"):
        return serve_proxy()
    install_fakes(interpreter)
    if module == "headroom_kit._owner" and "KIT_TEST_OWNER_SIGNAL" in os.environ:
        sys.settrace(interrupt_owner_startup)
    runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


def main() -> int:
    name = Path(sys.argv[0]).name
    if name in ("codex", "copilot", "pi", "opencode", "agent with spaces", "code", "code-insiders"):
        return run_agent(name)
    if "-m" in sys.argv:
        return delegate_module()
    return 2


if __name__ == "__main__":
    sys.exit(main())

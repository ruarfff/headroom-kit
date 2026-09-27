"""Pinned-runtime comparison using real proxies, synthetic content, and local upstreams."""

import argparse
import copy
import importlib.metadata
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))
from compression_fixtures import (
    FIXTURES,
    LOSSY_LOG,
    RETRIEVAL_MARKER,
    ROUTES,
    Upstream,
    request_body,
    upstream,
)

from headroom_kit.proxy import health, proxy_args, proxy_environment
from headroom_kit.runtime import Json, privacy, terminate

OLD_FLAGS = ["--mode", "cache", "--lossless", "--disable-kompress", "--disable-kompress-fallback"]
ORIGINAL = "Seeded recovery fixture: exact content for the transport checks."


@dataclass(frozen=True)
class Proxy:
    port: int
    startup_seconds: float

    def get(self, path: str) -> dict[str, Json]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{self.port}{path}", timeout=30) as response:
            return json.load(response)

    def post(self, route: str, body: dict[str, Json], session: str) -> str:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{ROUTES[route]}",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer fake-fixture-key",
                "x-api-key": "fake-fixture-key",
                "anthropic-version": "2023-06-01",
                "x-headroom-session-id": session,
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=60) as response:
            return response.read().decode()


@contextmanager
def start_proxy(
    policy: str, target: Upstream, kind: str = "codex", overrides: Mapping[str, str] | None = None
) -> Iterator[Proxy]:
    started = time.perf_counter()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        inherited = dict(os.environ, **(overrides or {}))
        env = proxy_environment(kind, port, inherited) if policy == "native" else privacy(inherited)
        if policy == "old":
            env.update(
                HEADROOM_OUTPUT_SHAPER="off",
                HEADROOM_EFFORT_ROUTER="off",
                HEADROOM_VERBOSITY_AUTOTUNE="off",
            )
        args = proxy_args(kind, port, target.url)
        if kind != "copilot":
            args += ["--openai-api-url", target.url, "--anthropic-api-url", target.url]
        if policy == "old":
            args += OLD_FLAGS
        process = subprocess.Popen(
            [sys.executable, "-I", "-m", "headroom_kit._serve", str(listener.fileno()), *args],
            env=env,
            pass_fds=(listener.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 90
            while not health(port).get("ready"):
                assert process.poll() is None, "Fixture proxy exited before readiness"
                assert time.monotonic() < deadline, "Fixture proxy startup timed out"
                time.sleep(0.1)
            yield Proxy(port, round(time.perf_counter() - started, 2))
        finally:
            terminate(process)


def profile_contracts(target: Upstream) -> None:
    from headroom.agent_savings import get_agent_savings_profile

    for name in ("coding", "balanced", "general", "agent-90"):
        profile = get_agent_savings_profile(name)
        for kind in ("codex", "copilot", "pi", "opencode"):
            inherited = dict(os.environ, HEADROOM_SAVINGS_PROFILE=name)
            env = proxy_environment(kind, 8788, inherited)
            assert all(env[key] == value for key, value in profile.proxy_env().items())
            with start_proxy("native", target, kind, {"HEADROOM_SAVINGS_PROFILE": name}) as proxy:
                config = proxy.get("/health")["config"]
                assert config["savings_profile"] == name
                assert config["protect_recent"] == profile.protect_recent
                assert config["min_tokens_to_crush"] == profile.min_tokens_to_compress
                assert proxy.get("/stats")["compression_cache"]["mode"] == profile.proxy_mode
    overrides = {
        "HEADROOM_MODE": "token",
        "HEADROOM_LOSSLESS": "1",
        "HEADROOM_DISABLE_KOMPRESS": "1",
        "HEADROOM_DISABLE_KOMPRESS_FALLBACK": "1",
        "HEADROOM_OUTPUT_SHAPER": "on",
        "HEADROOM_EFFORT_ROUTER": "on",
        "HEADROOM_VERBOSITY_AUTOTUNE": "on",
    }
    env = proxy_environment("copilot", 8788, dict(os.environ, **overrides))
    assert all(env[key] == value for key, value in overrides.items())
    with start_proxy("native", target, "copilot", overrides) as proxy:
        config = proxy.get("/health")["config"]
        assert config["disable_kompress"] and config["disable_kompress_fallback"]
        assert proxy.get("/stats")["compression_cache"]["mode"] == "token"
    for kind in ("codex", "copilot", "pi", "opencode"):
        with start_proxy("native", target, kind, {"HEADROOM_COMPRESSORS": "log"}) as proxy:
            target.reset()
            body = request_body("anthropic", FIXTURES["json"], "selection", False)
            assert "fixture complete" in proxy.post("anthropic", body, f"selection-{kind}")
            assert FIXTURES["json"] in strings(target.calls[0]["messages"]), (
                kind,
                "An unselected compressor changed the JSON fixture",
            )


def strings(value: Json) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def count(value: Json) -> int:
    import tiktoken

    return len(tiktoken.get_encoding("cl100k_base").encode(json.dumps(value)))


def fixture_case(
    proxy: Proxy, target: Upstream, route: str, stream: bool, fixture: str, text: str, policy: str
) -> dict[str, Json]:
    target.reset()
    body = request_body(route, text, fixture, stream)
    key = "input" if route == "responses" else "messages"
    session = f"{route}-{stream}-{fixture}"
    started = time.perf_counter()
    result = proxy.post(route, body, session)
    elapsed = (time.perf_counter() - started) * 1000
    assert "fixture complete" in result and "headroom_retrieve" not in result, (route, fixture)
    if stream:
        end = {
            "responses": "response.completed",
            "chat": "[DONE]",
            "anthropic": "event: message_stop",
        }
        assert end[route] in result, (route, fixture, "stream did not complete")
    assert target.calls, (route, fixture, "no upstream request")
    forwarded = target.calls[0][key]
    assert forwarded[1] == body[key][1], (route, fixture, "tool call changed")
    if fixture in ("read", "short"):
        assert text in strings(forwarded), (route, fixture, "protected bytes changed")
    row = {
        "route": route,
        "stream": stream,
        "fixture": fixture,
        "input_tokens": count(body[key]),
        "forwarded_tokens": count(forwarded),
        "schema_tokens_saved": count(body.get("tools", []))
        - count(target.calls[0].get("tools", [])),
        "latency_ms": round(elapsed, 2),
        # Includes local HTTP, parsing, and compression, not upstream service time.
        "compression_path_ms": round((target.received_at - started) * 1000, 2),
    }
    if policy == "native" and fixture == "json":
        first = copy.deepcopy(forwarded)
        body[key] += [
            {"role": "assistant", "content": "Continue."},
            {"role": "user", "content": "Report the final result."},
        ]
        target.reset()
        assert "fixture complete" in proxy.post(route, body, session)
        assert target.calls[0][key][: len(first)] == first, (
            route,
            stream,
            "forwarded prefix changed",
        )
    return row


def lossy_retrieval_case(proxy: Proxy, target: Upstream, stream: bool) -> dict[str, Json]:
    text = f"INFO mode_{stream}: retrieval fixture\n{LOSSY_LOG}"
    row = fixture_case(proxy, target, "anthropic", stream, "lossy-log", text, "native")
    forwarded = "\n".join(strings(target.calls[0]["messages"]))
    omitted = [line for line in text.splitlines() if line not in forwarded]
    assert omitted and row["forwarded_tokens"] < row["input_tokens"], "Lossy compression skipped"
    markers = RETRIEVAL_MARKER.findall(forwarded)
    assert len(markers) == 1, "No generated retrieval marker"
    hash_key = markers[0]
    assert text in strings(proxy.get(f"/v1/retrieve/{hash_key}")), "Original was not stored"
    assert target.retrievals == markers and len(target.calls) == 2, "Retrieval skipped"
    results = target.calls[1]["messages"][-1]["content"]
    retrieved = json.dumps(
        next(block for block in results if block.get("tool_use_id") == "call_retrieve")
    )
    assert all(line in retrieved for line in omitted), "Omitted lines not retrieved"
    assert any(
        "router:tool_result:log" in request["transforms_applied"]
        for request in proxy.get("/stats")["recent_requests"]
    ), "LogCompressor was not applied"
    return dict(
        row,
        omitted_lines=len(omitted),
        retrieval_marker=hash_key,
        upstream_streams=[body.get("stream") for body in target.calls],
    )


def seeded_retrieval_checks(proxy: Proxy, target: Upstream) -> int:
    from headroom.cache.compression_store import get_compression_store
    from headroom.ccr.tool_injection import create_ccr_tool_definition

    # The proxy and fixture process share the native SQLite CCR store, not a mock.
    hash_key = get_compression_store().store(
        original=ORIGINAL, compressed="Recovery fixture", original_item_count=1
    )
    assert ORIGINAL in json.dumps(proxy.get(f"/v1/retrieve/{hash_key}"))
    for route in ("responses", "anthropic"):
        for stream in (False, True):
            target.reset()
            body = request_body(route, f"Recovery fixture <<ccr:{hash_key}>>", "retrieval", stream)
            if route == "responses":
                tool = create_ccr_tool_definition("openai")["function"]
                body["tools"].append(dict(type="function", **tool))
            result = proxy.post(route, body, f"retrieve-{route}-{stream}")
            assert "fixture complete" in result and "headroom_retrieve" not in result
            assert len(target.retrievals) == 1 and len(target.calls) == 2, (
                route,
                stream,
                "retrieval not exercised",
            )
            assert ORIGINAL in json.dumps(target.calls[1]), "Removed content was not returned"
    retrieval_gaps(proxy, target, hash_key)
    return 4


def retrieval_gaps(proxy: Proxy, target: Upstream, hash_key: str) -> None:
    # These assertions are the reproducer for the pinned OpenAI route exception.
    # If upstream fixes the gaps, recheck and remove the lossless exception.
    for route in ("responses", "chat"):
        for stream in (False, True):
            target.reset()
            body = request_body(
                route, f"Recovery fixture <<ccr:{hash_key}>>", "retrieval-gap", stream
            )
            result = proxy.post(route, body, f"gap-{route}-{stream}")
            if route == "chat" and not stream:
                assert "headroom_retrieve" in result, "Recheck the OpenAI lossless exception"
            else:
                assert not target.retrievals, "Recheck the OpenAI lossless exception"
                assert "headroom_retrieve" not in json.dumps(target.calls[0].get("tools", []))


def worker(policy: str) -> None:
    import tree_sitter_language_pack  # noqa: F401
    from headroom.transforms.kompress_compressor import is_kompress_available

    assert importlib.metadata.version("headroom-ai") == "0.37.0"
    assert is_kompress_available(), "Kompress dependencies are missing"
    logging.disable(logging.CRITICAL)
    with upstream() as target:
        if policy == "native":
            profile_contracts(target)
        with start_proxy(policy, target) as proxy:
            rows = [
                fixture_case(proxy, target, route, stream, fixture, text, policy)
                for route in ROUTES
                for stream in (False, True)
                for fixture, text in FIXTURES.items()
            ]
            report = {
                "policy": policy,
                "startup_seconds": proxy.startup_seconds,
                "rows": rows,
                "seeded_retrievals": seeded_retrieval_checks(proxy, target)
                if policy == "native"
                else 0,
                "lossy_retrievals": [],
                "retrieval_failures": 0,
            }
        if policy == "native":
            for stream in (False, True):
                overrides = {
                    "HEADROOM_COMPRESSORS": "log",
                    "HEADROOM_MODE": "token",
                    "HEADROOM_LOSSLESS": "0",
                    "HEADROOM_MIN_TOKENS": "128",
                    "HEADROOM_NO_CCR_PROACTIVE_EXPANSION": "1",
                    "HEADROOM_WORKSPACE_DIR": str(Path.home() / f"lossy-{stream}"),
                    "HF_HUB_OFFLINE": "1",
                }
                with start_proxy("native", target, overrides=overrides) as proxy:
                    report["lossy_retrievals"].append(lossy_retrieval_case(proxy, target, stream))
        print(json.dumps(report))


def compare(reports: list[dict[str, Json]]) -> None:
    old, native = reports
    baseline = {(row["route"], row["stream"], row["fixture"]): row for row in old["rows"]}
    for row in native["rows"]:
        if row["fixture"] != "json":
            continue
        previous = baseline[(row["route"], row["stream"], "json")]["forwarded_tokens"]
        if row["route"] == "anthropic":
            assert row["forwarded_tokens"] < previous
        else:
            # Removing Kit's OpenAI guard must fail this safety regression.
            assert row["forwarded_tokens"] == previous
    assert native["seeded_retrievals"] == 4
    assert len(native["lossy_retrievals"]) == 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("old", "native"))
    parser.add_argument(
        "--model-cache", type=Path, help="Reusable Hugging Face hub cache (models only)"
    )
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    reports = []
    with tempfile.TemporaryDirectory(prefix="headroom-kit-compression-") as temporary:
        root = Path(temporary)
        for policy in ("old", "native"):
            home = root / policy
            home.mkdir()
            env = privacy(
                {
                    "HOME": str(home),
                    "PATH": os.defpath,
                    "HEADROOM_MALLOC_TUNING": "0",
                    "HEADROOM_AGENT_TYPE": "codex",
                    "HF_HUB_CACHE": str(args.model_cache or root / "models"),
                    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                }
            )
            result = subprocess.run(
                [sys.executable, "-I", str(Path(__file__).resolve()), "--worker", policy],
                env=env,
                cwd=home,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode:
                print(result.stderr[-5000:], file=sys.stderr)
                raise SystemExit(result.returncode)
            report = json.loads(result.stdout)
            reports.append(report)
            print(json.dumps(report))
    compare(reports)


if __name__ == "__main__":
    main()

"""Real 0.37.0 metrics writers and graceful proxy restarts; no model requests."""

import asyncio
import importlib.metadata
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headroom_kit.proxy import health
from headroom_kit.runtime import Json, privacy, terminate

REQUESTS = 23
SAVED = 100


def writer(port: int, count: int) -> None:
    import uvicorn
    from fastapi import FastAPI
    from headroom.paths import process_is_stateless
    from headroom.proxy.server import ProxyConfig, create_app
    from headroom.telemetry.beacon import is_telemetry_enabled

    native = create_app(
        ProxyConfig(stateless=process_is_stateless(), traffic_learning_enabled=False)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with native.router.lifespan_context(native):
            proxy = native.state.proxy
            assert is_telemetry_enabled() == (os.environ["HEADROOM_TELEMETRY"] == "on")
            if count:
                barrier = Path(os.environ["SMOKE_METRICS_BARRIER"])
                (barrier / str(port)).touch()
                async with asyncio.timeout(90):
                    while len(list(barrier.iterdir())) < 2:
                        await asyncio.sleep(0.05)
                for _ in range(count):
                    await proxy.metrics.record_request(
                        provider="openai",
                        model="gpt-4o-mini",
                        input_tokens=900,
                        output_tokens=10,
                        tokens_saved=SAVED,
                        latency_ms=1,
                        client=str(port),
                    )
                    await asyncio.sleep(0.01)
                if not proxy.config.stateless:
                    assert (
                        proxy.metrics.savings_tracker.lifetime_response()["persistence"][
                            "pending_records"
                        ]
                        > 0
                    )
            yield

    # Compose the real lifespan so shutdown must flush the pending synthetic batch.
    app = FastAPI(lifespan=lifespan)
    app.mount("/", native)
    uvicorn.run(app, host="127.0.0.1", port=port)


def stats(port: int) -> dict[str, Json]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/stats", timeout=5) as response:
        return json.load(response)


def start(port: int, count: int, env: dict[str, str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", str(Path(__file__).resolve()), "--writer", str(port), str(count)],
        env=env,
        cwd="/",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ready(process: subprocess.Popen[bytes], port: int) -> None:
    deadline = time.monotonic() + 120
    while not health(port).get("ready"):
        assert process.poll() is None, "Synthetic metrics proxy exited before readiness"
        assert time.monotonic() < deadline, "Synthetic metrics proxy startup timed out"
        time.sleep(0.1)


def check_ledger(env: dict[str, str]) -> None:
    ledger = Path(env["HEADROOM_SAVINGS_EVENTS_PATH"])
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(events) == 2 * REQUESTS
    assert len({event["pid"] for event in events}) == 2
    result = subprocess.run(
        [sys.executable, "-I", "-m", "headroom.cli", "savings", "--json"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert json.loads(result.stdout)["lifetime"]["tokens_saved"] == 2 * REQUESTS * SAVED


def check(root: Path) -> None:
    barrier = root / "barrier"
    barrier.mkdir()
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        ports = [first.getsockname()[1], second.getsockname()[1]]
    envs = [
        dict(
            privacy(os.environ),
            HEADROOM_WORKSPACE_DIR=str(root),
            HEADROOM_SAVINGS_PATH=str(root / "headroom-kit" / str(port) / "proxy_savings.json"),
            HEADROOM_SAVINGS_EVENTS_PATH=str(root / "savings_events.jsonl"),
            HEADROOM_STATELESS="0",
            HEADROOM_TELEMETRY="on" if port == ports[0] else "off",
            SMOKE_METRICS_BARRIER=str(barrier),
        )
        for port in ports
    ]
    processes = []
    try:
        for port, env in zip(ports, envs, strict=True):
            processes.append(start(port, REQUESTS, env))
        for process, port in zip(processes, ports, strict=True):
            ready(process, port)
            assert stats(port)["persistent_savings"]["lifetime"]["tokens_saved"] == REQUESTS * SAVED
        for process in processes:
            terminate(process)
            # Uvicorn re-raises SIGTERM after its graceful shutdown handlers run.
            assert process.returncode in (0, -signal.SIGTERM), "Proxy shutdown failed"
        for env in envs:
            persisted = json.loads(Path(env["HEADROOM_SAVINGS_PATH"]).read_text())
            assert persisted["lifetime"]["tokens_saved"] == REQUESTS * SAVED
        check_ledger(envs[0])
        for port, env in zip(ports, envs, strict=True):
            process = start(port, 0, env)
            processes.append(process)
            ready(process, port)
            assert stats(port)["persistent_savings"]["lifetime"]["tokens_saved"] == REQUESTS * SAVED
            terminate(process)
        check_ledger(envs[0])
        stateless = dict(
            envs[0],
            HEADROOM_STATELESS="1",
            HEADROOM_SAVINGS_PATH=str(root / "stateless.json"),
            HEADROOM_SAVINGS_EVENTS_PATH=str(root / "stateless.jsonl"),
        )
        process = start(ports[0], REQUESTS, stateless)
        processes.append(process)
        ready(process, ports[0])
        terminate(process)
        assert not Path(stateless["HEADROOM_SAVINGS_PATH"]).exists()
        assert not Path(stateless["HEADROOM_SAVINGS_EVENTS_PATH"]).exists()
    finally:
        for process in processes:
            terminate(process)


def main() -> None:
    assert importlib.metadata.version("headroom-ai") == "0.37.0"
    if sys.argv[1:2] == ["--writer"]:
        writer(int(sys.argv[2]), int(sys.argv[3]))
        return
    with tempfile.TemporaryDirectory(prefix="headroom-kit-metrics-") as temporary:
        check(Path(temporary).resolve())
    print("Real concurrent metrics, shared savings report, graceful flush and restart: PASS")


if __name__ == "__main__":
    main()

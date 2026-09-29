"""Opt-in live Copilot routing check. Each supplied model makes a paid model request."""

import argparse
import json
import os
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path


def request_count(port: int) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/stats", timeout=10) as response:
        return json.load(response)["requests"]["total"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "package", type=Path, help="Built headroom-kit package (for example ./result)"
    )
    parser.add_argument("models", nargs="+", help="Copilot model IDs to check, including auto")
    args = parser.parse_args()
    package = args.package.resolve()
    if not (package / "bin/headroom-kit").is_file():
        parser.error("Package is missing bin/headroom-kit")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, HEADROOM_COPILOT_PORT=str(port), COPILOT_MODEL="auto")
    # Prove that inherited BYOK settings cannot defeat native routing.
    env.update(
        COPILOT_PROVIDER_BASE_URL="http://127.0.0.1:1",
        COPILOT_PROVIDER_WIRE_API="responses",
        COPILOT_PROVIDER_MODEL_ID="fake-byok-model",
    )
    previous = 0
    started = False
    with tempfile.TemporaryDirectory(prefix="headroom-copilot-models-") as cwd:
        try:
            for model in args.models:
                result = subprocess.run(
                    [
                        str(package / "bin/headroom-kit"),
                        "run",
                        "copilot",
                        "--",
                        "--model",
                        model,
                        "--disable-builtin-mcps",
                        "--available-tools=",
                        "--log-level",
                        "none",
                        "-s",
                        "-p",
                        "Reply with the single word ok. Do not run tools.",
                    ],
                    env=env,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=240,
                )
                started = started or "Started Headroom" in result.stderr
                if result.returncode or result.stdout.strip().lower().rstrip(".") != "ok":
                    # Client diagnostics can contain credentials; do not print them.
                    raise RuntimeError(f"{model}: no successful reply (exit {result.returncode})")
                current = request_count(port)
                if current <= previous:
                    raise RuntimeError(
                        f"{model}: replied without a Headroom request counter increase"
                    )
                if previous and "Reusing Headroom" not in result.stderr:
                    raise RuntimeError(f"{model}: did not reuse the proxy")
                print(f"{model}: reply OK, {current - previous} Headroom requests", flush=True)
                previous = current
        except subprocess.TimeoutExpired as error:
            started = started or b"Started Headroom" in (error.stderr or b"")
            raise RuntimeError(f"{model}: timed out") from None
        finally:
            if started:
                result = subprocess.run(
                    [str(package / "bin/headroom-kit"), "stop", str(port)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                if result.returncode:
                    raise RuntimeError(f"Could not stop test proxy on port {port}")


if __name__ == "__main__":
    main()

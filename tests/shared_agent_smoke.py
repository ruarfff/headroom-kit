"""Opt-in macOS check: real clients, shared fake proxy, no external connections."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from test_launch import LauncherTests


def check(name: str, executable: str, mode: str) -> None:
    fixture = LauncherTests()
    fixture.setUp()
    try:
        fixture.cfg[f"{name}Executable"] = executable
        fixture.defaults.write_text(json.dumps(fixture.cfg))
        fixture.env.update(
            PATH=f"{fixture.bin}:/usr/bin:/bin",
            OPENAI_API_KEY="fake-local-key",
            ANTHROPIC_API_KEY="fake-local-key",
            PI_SKIP_VERSION_CHECK="1",
            PI_TELEMETRY="0",
            OPENCODE_DISABLE_AUTOUPDATE="true",
            OPENCODE_DISABLE_MODELS_FETCH="true",
            TERM="dumb",
            NO_COLOR="1",
            HTTP_PROXY="http://127.0.0.1:1",
            HTTPS_PROXY="http://127.0.0.1:1",
            ALL_PROXY="http://127.0.0.1:1",
        )
        if mode == "wildcard":
            fixture.env.update(NO_PROXY="example.invalid", no_proxy="*")
        if name == "opencode":
            # OpenCode's first-use database setup is outside Kit's proxy lifecycle.
            fixture.run_launcher(
                *arguments(name, "warmup"), command="opencode-headroom", mode="real-routing"
            )
        clients = [
            fixture.start(
                command=f"{name}-headroom", mode="real-routing", args=arguments(name, probe)
            )
            for probe in ("shared-probe-one", "shared-probe-two")
        ]
        outputs = [client.communicate(timeout=90) for client in clients]
        events = fixture.events()
        starts = [event for event in events if event["event"] == "proxy-start"]
        requests = [event for event in events if event["event"] == "proxy-request"]
        assert len(starts) == 1, starts
        assert {event["probe"] for event in requests} >= {"shared-probe-one", "shared-probe-two"}, (
            requests,
            outputs,
        )
        assert {event["instance"] for event in requests} == {starts[0]["pid"]}
        assert not any(event["event"] == "proxy-stop" for event in events)
        print(
            f"{name} {mode}: two real clients, one proxy, {len(requests)} requests, both prompts observed",
            flush=True,
        )
    finally:
        fixture.doCleanups()


def arguments(name: str, probe: str) -> list[str]:
    if name == "codex":
        return [
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            "gpt-4.1",
            probe,
        ]
    if name == "pi":
        return [
            "--provider",
            "openai",
            "--model",
            "gpt-4.1",
            "--no-tools",
            "--no-extensions",
            "-p",
            probe,
        ]
    return ["run", "--model", "openai/gpt-4.1", probe]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside-sandbox", action="store_true")
    for name in ("codex", "pi", "opencode"):
        parser.add_argument(f"--{name}", default=shutil.which(name))
    args = parser.parse_args()
    executables = {"codex": args.codex, "pi": args.pi, "opencode": args.opencode}
    if sys.platform != "darwin":
        parser.error("This smoke requires macOS sandbox-exec; Linux routing is unverified.")
    if not args.inside_sandbox:
        profile = '(version 1)(allow default)(deny network*)(allow network* (local ip "localhost:*") (remote ip "localhost:*") (local unix-socket) (remote unix-socket))'
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            sys.executable,
            str(Path(__file__).resolve()),
            "--inside-sandbox",
        ]
        for name in ("codex", "pi", "opencode"):
            executable = executables[name]
            if not executable:
                parser.error(f"{name} is required")
            command += [f"--{name}", executable]
        os.execv(command[0], command)
    for name in ("codex", "pi", "opencode"):
        for mode in ("ordinary", "wildcard"):
            check(name, executables[name], mode)


if __name__ == "__main__":
    main()

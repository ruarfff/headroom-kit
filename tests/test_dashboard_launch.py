"""Agent launch contracts for automatic dashboard opening."""

import json
import sys
from collections.abc import Iterator

import pytest
import test_launch


@pytest.fixture
def launcher() -> Iterator[test_launch.LauncherTests]:
    case = test_launch.LauncherTests()
    case.setUp()
    try:
        for name in ("open", "xdg-open"):
            path = case.bin / name
            path.write_text(f"#!{sys.executable}\n" + test_launch.STANDIN)
            path.chmod(0o700)
        case.env["DISPLAY"] = ":0"
        yield case
    finally:
        case.doCleanups()


def test_each_launch_opens_its_ready_dashboard_including_proxy_reuse(
    launcher: test_launch.LauncherTests,
) -> None:
    for command, key in (
        ("codex-headroom", "codexPort"),
        ("copilot-headroom", "copilotPort"),
        ("pi-headroom", "piPort"),
        ("opencode-headroom", "opencodePort"),
        ("copilot-vscode-headroom", "vscodePort"),
        ("codex-headroom", "codexPort"),
    ):
        before = len(launcher.events())
        result = launcher.run_launcher(command=command, mode="agent-failure")
        assert result.returncode == 37, result.stderr
        events = launcher.events()[before:]
        browsers = [event for event in events if event["event"] == "browser"]
        assert browsers == [
            {
                "event": "browser",
                "url": f"http://127.0.0.1:{launcher.cfg[key]}/dashboard",
                "ready": True,
            }
        ]
        client = [event for event in events if event["event"] in ("agent", "editor")][-1]
        assert events.index(browsers[0]) < events.index(client)


def test_dashboard_environment_overrides_file_and_browser_failure_keeps_agent(
    launcher: test_launch.LauncherTests,
) -> None:
    launcher.cfg["openDashboard"] = False
    launcher.defaults.write_text(json.dumps(launcher.cfg))
    result = launcher.run_launcher()
    assert result.returncode == 0, result.stderr
    assert not any(event["event"] == "browser" for event in launcher.events())
    result = launcher.run_launcher(env={"HEADROOM_OPEN_DASHBOARD": "ON"}, mode="browser-failure")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "AGENT_OUTPUT\n"
    assert "Could not open the dashboard" in result.stderr
    assert len([event for event in launcher.events() if event["event"] == "browser"]) == 1
    launcher.cfg["openDashboard"] = True
    launcher.defaults.write_text(json.dumps(launcher.cfg))
    result = launcher.run_launcher(env={"HEADROOM_OPEN_DASHBOARD": "0"})
    assert result.returncode == 0, result.stderr
    assert len([event for event in launcher.events() if event["event"] == "browser"]) == 1


@pytest.mark.parametrize(
    ("command", "args", "key"),
    [
        ("pi-headroom", ["--provider", "github-copilot", "--model", "gpt-4.1"], "copilotPort"),
        ("pi-headroom", ["--provider=github-copilot"], "copilotPort"),
        ("opencode-headroom", ["run", "--model", "github-copilot/gpt-4.1"], "copilotPort"),
        ("opencode-headroom", ["run", "-m", "github-copilot/gpt-4.1"], "copilotPort"),
        ("pi-headroom", ["--provider", "openai", "--model", "github-copilot/fake"], "piPort"),
    ],
)
def test_selected_provider_dashboard(
    launcher: test_launch.LauncherTests, command: str, args: list[str], key: str
) -> None:
    result = launcher.run_launcher(*args, command=command)
    assert result.returncode == 0, result.stderr
    events = launcher.events()
    browsers = [event for event in events if event["event"] == "browser"]
    assert browsers == [
        {
            "event": "browser",
            "url": f"http://127.0.0.1:{launcher.cfg[key]}/dashboard",
            "ready": True,
        }
    ]
    client = [event for event in events if event["event"] == "agent"][-1]
    assert events.index(browsers[0]) < events.index(client)


def test_help_and_failed_proxy_do_not_open_dashboard(launcher: test_launch.LauncherTests) -> None:
    for args, mode in ((["--help"], ""), ([], "startup-failure")):
        result = launcher.run_launcher(*args, mode=mode)
        assert result.returncode == (1 if mode else 0), result.stderr
    assert not any(event["event"] == "browser" for event in launcher.events())

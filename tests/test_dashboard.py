"""Dashboard launch policy uses desktop openers, never a terminal browser."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from headroom_kit.runtime import DEFAULTS, KitError, validate

ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = {
            "PATH": str(self.root),
            "HOME": str(self.root),
            "PYTHONPATH": str(ROOT / "src"),
        }
        for name in ("open", "xdg-open"):
            path = self.root / name
            path.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys, time\n"
                "from pathlib import Path\n"
                "Path(os.environ['HOME'], 'opened').write_text(json.dumps(sys.argv))\n"
                "print('browser output must not reach the agent')\n"
                "time.sleep(float(os.environ.get('BROWSER_DELAY', '0')))\n"
                "sys.exit(int(os.environ.get('BROWSER_EXIT', '0')))\n"
            )
            path.chmod(0o700)

    def open_dashboard(
        self, platform: str, enabled: bool = True, **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from headroom_kit.runtime import DEFAULTS; "
                "from headroom_kit.session import Desktop, open_dashboard; "
                "open_dashboard(dict(DEFAULTS, openDashboard=json.loads(sys.argv[1])), "
                "9876, Desktop(platform=sys.argv[2]))",
                json.dumps(enabled),
                platform,
            ],
            env=dict(self.env, **env),
            capture_output=True,
            text=True,
            timeout=8,
        )

    def test_default_and_boolean_values(self) -> None:
        self.assertIs(DEFAULTS["openDashboard"], True)
        for value, expected in (
            (True, True),
            (False, False),
            ("1", True),
            ("0", False),
            ("true", True),
            ("false", False),
            ("on", True),
            ("off", False),
            ("TRUE", True),
        ):
            with self.subTest(value=value):
                cfg = dict(DEFAULTS, openDashboard=value)
                validate(cfg)
                self.assertIs(cfg["openDashboard"], expected)
        for value in ("", "yes", 1, 0, None, [], {}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(KitError, "HEADROOM_OPEN_DASHBOARD"),
            ):
                validate(dict(DEFAULTS, openDashboard=value))

    def test_desktop_openers_receive_only_dashboard_url(self) -> None:
        for platform, env, executable in (
            ("darwin", {}, "open"),
            ("linux", {"DISPLAY": ":0"}, "xdg-open"),
            ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, "xdg-open"),
        ):
            with self.subTest(platform=platform, env=env):
                result = self.open_dashboard(platform, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(
                    json.loads((self.root / "opened").read_text()),
                    [str(self.root / executable), "http://127.0.0.1:9876/dashboard"],
                )
                (self.root / "opened").unlink()

    def test_disabled_headless_remote_and_unsupported_sessions_skip_browser(self) -> None:
        for platform, enabled, env in (
            ("darwin", False, {}),
            ("linux", True, {}),
            ("darwin", True, {"SSH_CONNECTION": "remote session"}),
            ("darwin", True, {"SSH_CLIENT": "remote session"}),
            ("darwin", True, {"SSH_TTY": "/dev/pts/0"}),
            ("linux", True, {"DISPLAY": ":0", "SSH_CONNECTION": "remote session"}),
            ("win32", True, {}),
        ):
            with self.subTest(platform=platform, enabled=enabled, env=env):
                result = self.open_dashboard(platform, enabled, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.root / "opened").exists())

    def test_missing_opener_is_optional(self) -> None:
        (self.root / "open").unlink()
        result = self.open_dashboard("darwin")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "opened").exists())

    def test_failed_and_stalled_openers_return_without_browser_output(self) -> None:
        for env in ({"BROWSER_EXIT": "1"}, {"BROWSER_DELAY": "30"}):
            with self.subTest(env=env):
                result = self.open_dashboard("darwin", **env)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("http://127.0.0.1:9876/dashboard", result.stderr)
                self.assertNotIn("browser output", result.stderr)


if __name__ == "__main__":
    unittest.main()

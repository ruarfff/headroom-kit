"""Run with the resolved Headroom 0.37.0 Python; no accounts or GUI processes."""

import importlib.metadata
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headroom_kit import session as kit


class PinnedWriterTests(unittest.TestCase):
    def test_real_writer_is_guarded_at_launch_and_immediately_before_write(self) -> None:
        self.assertEqual(importlib.metadata.version("headroom-ai"), "0.37.0")
        for layout in ("ordinary", "settings-link", "user-link", "late-settings-link"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                data = root / "isolated"
                data.mkdir()
                normal = root / "normal Code/User/settings.json"
                normal.parent.mkdir(parents=True)
                original = b'// Preserve normal preferences\n{"editor.fontSize": 19}\n'
                normal.write_bytes(original)
                settings = data / "User/settings.json"
                if layout == "user-link":
                    settings.parent.symlink_to(normal.parent, target_is_directory=True)
                else:
                    settings.parent.mkdir()
                    if layout == "settings-link":
                        settings.symlink_to(normal)
                    else:
                        settings.write_bytes(original)

                def proxy(
                    cfg: kit.Config,
                    version: str,
                    kind: str,
                    port: int,
                    auth: kit.CopilotAuth | None,
                ) -> str:
                    if layout == "late-settings-link":
                        settings.unlink()
                        settings.symlink_to(normal)
                    return f"http://127.0.0.1:{port}"

                cfg = {
                    "vscodeChannel": "stable",
                    "vscodeExecutable": sys.executable,
                    "vscodeUserDataDir": str(data),
                    "vscodeExtensionsDir": str(root / "extensions"),
                    "vscodePort": 18787,
                    "startupTimeout": 2,
                }
                launches = []

                def authorize() -> kit.CopilotAuth:
                    return kit.CopilotAuth("https://api.githubcopilot.com", "test-only-oauth")

                def launch(argv: Sequence[str], env: Mapping[str, str] | None) -> int:
                    launches.append(list(argv))
                    return 0

                if layout == "ordinary":
                    self.assertEqual(
                        kit.session(
                            cfg,
                            "copilot-vscode-headroom",
                            [],
                            "0.37.0",
                            authorize=authorize,
                            start_proxy=proxy,
                            launch=launch,
                        ),
                        0,
                    )
                    self.assertEqual(len(launches), 1)
                    self.assertIn(b"http://127.0.0.1:18787", settings.read_bytes())
                    self.assertIn(b'"editor.fontSize": 19', settings.read_bytes())
                else:
                    with self.assertRaisesRegex(kit.KitError, "settings path must stay inside"):
                        kit.session(
                            cfg,
                            "copilot-vscode-headroom",
                            [],
                            "0.37.0",
                            authorize=authorize,
                            start_proxy=proxy,
                            launch=launch,
                        )
                    self.assertEqual(launches, [])
                self.assertEqual(normal.read_bytes(), original)

    def test_real_writer_conflict_has_a_safe_error_and_preserves_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary).resolve()
            settings = data / "User/settings.json"
            settings.parent.mkdir()
            original = b'[{"sentinel-private-setting": "test-only"}]'
            settings.write_bytes(original)
            with self.assertRaisesRegex(
                kit.KitError, "Cannot configure isolated editor settings"
            ) as caught:
                kit.configure_editor(data, 18787)
            self.assertNotIn("sentinel-private-setting", str(caught.exception))
            self.assertEqual(settings.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

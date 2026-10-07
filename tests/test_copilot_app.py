"""Copilot app isolation and provider contracts using local processes and SQLite."""

import json
import os
import plistlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from headroom_kit import copilot_app, session
from headroom_kit.copilot import CopilotAuth
from headroom_kit.runtime import DEFAULTS, Config, Json, KitError

SCHEMA = """
PRAGMA user_version=156;
CREATE TABLE accounts(id TEXT PRIMARY KEY);
CREATE TABLE model_providers (
  id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  type TEXT NOT NULL DEFAULT 'openai', settings_json TEXT NOT NULL DEFAULT '{}',
  account_id TEXT REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE TABLE provider_models (
  id TEXT PRIMARY KEY NOT NULL,
  provider_id TEXT NOT NULL REFERENCES model_providers(id) ON DELETE CASCADE,
  model_id TEXT NOT NULL, wire_model TEXT, display_name TEXT NOT NULL,
  max_prompt_tokens INTEGER, max_output_tokens INTEGER,
  wire_api_override TEXT CHECK(wire_api_override IS NULL OR wire_api_override IN ('completions','responses')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  supported_reasoning_efforts TEXT, UNIQUE(provider_id, model_id)
);
CREATE TABLE app_state (
  key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


def model(model_id: str, endpoints: list[str], visible: bool = True) -> dict[str, Json]:
    return {
        "id": model_id,
        "name": model_id.title(),
        "model_picker_enabled": visible,
        "supported_endpoints": endpoints,
        "capabilities": {
            "limits": {"max_prompt_tokens": 128000, "max_output_tokens": 16000},
            "supports": {"reasoning_effort": ["low", "high"]},
        },
    }


class CopilotAppTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "isolated profile"
        self.home = self.root / "home"
        self.normal = self.home / ".copilot"
        self.normal.mkdir(parents=True)
        (self.normal / "data.db").write_bytes(b"normal profile must stay unchanged")
        self.bundle = self.root / "GitHub Copilot.app"
        binary = self.bundle / "Contents/MacOS/copilot"
        binary.parent.mkdir(parents=True)
        (self.bundle / "Contents/Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "test.copilot",
                    "CFBundleExecutable": "copilot",
                }
            )
        )
        self.running = self.root / "running"
        self.lookup = self.root / "lsappinfo"
        self.lookup.write_text(
            f"#!{sys.executable}\nfrom pathlib import Path\n"
            f"print('running' if Path({str(self.running)!r}).exists() else '')\n"
        )
        self.lookup.chmod(0o755)
        binary.write_text(
            f"#!{sys.executable}\nimport json, os, sqlite3, sys, time\nfrom pathlib import Path\n"
            "data = Path(sys.argv[sys.argv.index('--config-dir') + 1])\n"
            "assert os.environ['COPILOT_HOME'] == str(data)\n"
            f"with sqlite3.connect(data / 'data.db') as db: db.executescript({SCHEMA!r})\n"
            "(data / 'bootstrap.json').write_text(json.dumps({'args': sys.argv[1:], 'home': os.environ['COPILOT_HOME']}))\n"
            "while True: time.sleep(1)\n"
        )
        binary.chmod(0o755)
        self.cfg: Config = dict(
            DEFAULTS,
            openDashboard=False,
            copilotAppPath=str(self.bundle),
            copilotAppDataDir=str(self.data),
        )
        self.calls: list[str] = []
        self.launched: list[tuple[list[str], dict[str, str]]] = []
        self.catalog = {
            "data": [
                model("gpt-6.1-sol", ["/responses"]),
                model("claude-sonnet-test", ["/chat/completions", "/responses"]),
                model("hidden-test", ["/responses"], False),
                model("embedding-test", ["/embeddings"]),
                model("auto", ["/responses"]),
            ]
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.calls.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(owner.catalog).encode())

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.desktop = session.Desktop(platform="darwin", lookup=str(self.lookup), open="fake-open")

    def app(self) -> copilot_app.App:
        return copilot_app.preflight(self.cfg, "darwin", str(self.lookup), home=self.home, env={})

    def prepare(self) -> None:
        self.data.mkdir()
        (self.data / copilot_app.MARKER).write_text(copilot_app.MARKER_CONTENT)
        with sqlite3.connect(self.data / "data.db") as database:
            database.executescript(SCHEMA)

    def launch(self, argv: Sequence[str], env: Mapping[str, str] | None) -> int:
        self.launched.append((list(argv), dict(env or {})))
        return 0

    def start_proxy(
        self,
        cfg: Config,
        version: str,
        kind: str,
        port: int,
        auth: CopilotAuth | None,
    ) -> str:
        self.calls.append("proxy")
        self.assertEqual((kind, port), ("copilot", self.cfg["copilotPort"]))
        return self.endpoint

    def authorize(self) -> CopilotAuth:
        self.calls.append("auth")
        return CopilotAuth("https://api.githubcopilot.com", "fake-refresh")

    def selected(self) -> str:
        with sqlite3.connect(self.data / "data.db") as database:
            return database.execute(
                "SELECT value FROM app_state WHERE key='copilot-selected-model'"
            ).fetchone()[0]

    def test_fresh_profile_bootstrap_and_launch_leave_normal_data_unchanged(self) -> None:
        normal = (self.normal / "data.db").read_bytes()
        app = self.app()
        result = copilot_app.launch(
            app, self.endpoint, 3, "fake-open", self.launch, {"NO_PROXY": "127.0.0.1"}
        )
        self.assertEqual(result, 0)
        args, env = self.launched[0]
        self.assertEqual(
            args[-5:], ["-a", str(self.bundle), "--args", "--config-dir", str(self.data)]
        )
        self.assertIn(f"COPILOT_HOME={self.data}", args)
        self.assertEqual(env["COPILOT_HOME"], str(self.data))
        self.assertEqual((self.normal / "data.db").read_bytes(), normal)
        self.assertEqual((self.data / copilot_app.MARKER).read_text(), copilot_app.MARKER_CONTENT)
        self.assertEqual(self.data.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.selected(), f"{copilot_app.PROVIDER_ID}/gpt-6.1-sol")

    def test_session_uses_shared_copilot_auth_port_and_catalog(self) -> None:
        self.prepare()
        result = session.session(
            self.cfg,
            "copilot-app-headroom",
            [],
            "test-version",
            desktop=self.desktop,
            authorize=self.authorize,
            start_proxy=self.start_proxy,
            launch=self.launch,
        )
        self.assertEqual(result, 0)
        self.assertEqual(self.calls, ["auth", "proxy", "/v1/models"])

    def test_running_app_stops_before_auth_proxy_or_profile_writes(self) -> None:
        self.running.touch()
        with self.assertRaisesRegex(KitError, "Quit GitHub Copilot"):
            session.session(
                self.cfg,
                "copilot-app-headroom",
                [],
                "test-version",
                desktop=self.desktop,
                authorize=self.authorize,
                start_proxy=self.start_proxy,
                launch=self.launch,
            )
        self.assertEqual(self.calls, [])
        self.assertFalse(self.data.exists())

    def test_running_app_is_rechecked_after_preflight(self) -> None:
        app = self.app()
        self.running.touch()
        with self.assertRaisesRegex(KitError, "Quit GitHub Copilot"):
            copilot_app.launch(app, self.endpoint, 3, "fake-open", self.launch, {})
        self.assertFalse(self.data.exists())
        self.assertEqual(self.launched, [])

    def test_normal_profile_ancestors_descendants_and_symlinks_are_rejected(self) -> None:
        alias = self.root / "normal alias"
        alias.symlink_to(self.normal, target_is_directory=True)
        for data in (self.normal, self.normal / "child", self.home, alias):
            with self.subTest(data=data):
                self.cfg["copilotAppDataDir"] = str(data)
                with self.assertRaisesRegex(KitError, "separate from normal Copilot"):
                    self.app()
        self.assertEqual(
            (self.normal / "data.db").read_bytes(), b"normal profile must stay unchanged"
        )

    def test_inherited_copilot_home_is_protected(self) -> None:
        self.cfg["copilotAppDataDir"] = str(self.root / "custom home" / "child")
        with self.assertRaisesRegex(KitError, "separate from normal Copilot"):
            copilot_app.preflight(
                self.cfg,
                "darwin",
                str(self.lookup),
                home=self.home,
                env={"COPILOT_HOME": str(self.root / "custom home")},
            )

    def test_unknown_existing_profile_is_not_adopted(self) -> None:
        self.data.mkdir()
        (self.data / "existing-settings").write_text("leave this alone")
        with self.assertRaisesRegex(KitError, "empty directory or a Kit-owned"):
            self.app()
        self.assertFalse((self.data / copilot_app.MARKER).exists())

    def test_session_artifacts_and_shared_skills_do_not_block_relaunch(self) -> None:
        self.prepare()
        interpreter = self.root / "python"
        interpreter.write_text("external interpreter must stay unchanged")
        binary = self.data / "session-state/test/files/validation-venv/bin/python"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(interpreter)
        os.link(interpreter, binary.parent / "hardlinked-artifact")
        skills = self.root / "shared-skills"
        skills.mkdir()
        (skills / "SKILL.md").write_text("shared skill must stay unchanged")
        (self.data / "skills").symlink_to(skills, target_is_directory=True)

        result = copilot_app.launch(self.app(), self.endpoint, 3, "fake-open", self.launch, {})

        self.assertEqual(result, 0)
        self.assertEqual(self.selected(), f"{copilot_app.PROVIDER_ID}/gpt-6.1-sol")
        self.assertTrue(binary.is_symlink())
        self.assertEqual(interpreter.read_text(), "external interpreter must stay unchanged")
        self.assertEqual((skills / "SKILL.md").read_text(), "shared skill must stay unchanged")
        self.assertEqual(
            (self.normal / "data.db").read_bytes(), b"normal profile must stay unchanged"
        )

    def test_escaping_managed_file_symlinks_are_rejected(self) -> None:
        self.data.mkdir()
        marker = self.data / copilot_app.MARKER
        marker.write_text(copilot_app.MARKER_CONTENT)
        names = (
            copilot_app.MARKER,
            copilot_app.BOOTSTRAP_MARKER,
            "data.db",
            "data.db-wal",
            "data.db-shm",
            "data.db-journal",
        )
        for name in names:
            for target in (self.normal / "data.db", self.normal / "missing"):
                with self.subTest(name=name, target=target):
                    link = self.data / name
                    link.unlink(missing_ok=True)
                    link.symlink_to(target)
                    with self.assertRaisesRegex(KitError, "without links outside"):
                        self.app()
                    link.unlink()
                    marker.write_text(copilot_app.MARKER_CONTENT)

    def test_managed_file_hardlinks_are_rejected(self) -> None:
        self.data.mkdir()
        marker = self.data / copilot_app.MARKER
        marker.write_text(copilot_app.MARKER_CONTENT)
        for name in (
            copilot_app.MARKER,
            copilot_app.BOOTSTRAP_MARKER,
            "data.db",
            "data.db-wal",
            "data.db-shm",
            "data.db-journal",
        ):
            with self.subTest(name=name):
                link = self.data / name
                link.unlink(missing_ok=True)
                os.link(self.normal / "data.db", link)
                with self.assertRaisesRegex(KitError, "without links outside"):
                    self.app()
                link.unlink()
                marker.write_text(copilot_app.MARKER_CONTENT)

    def test_unsupported_schema_is_not_changed(self) -> None:
        self.prepare()
        with sqlite3.connect(self.data / "data.db") as database:
            database.execute("PRAGMA user_version=157")
        before = (self.data / "data.db").read_bytes()
        with self.assertRaisesRegex(KitError, "Unsupported Copilot app database schema"):
            copilot_app.configure(
                self.app(), self.endpoint, copilot_app.fetch_models(self.endpoint)
            )
        self.assertEqual((self.data / "data.db").read_bytes(), before)
        self.assertEqual(self.launched, [])

    def test_changed_columns_are_rejected_even_with_the_supported_version(self) -> None:
        self.prepare()
        with sqlite3.connect(self.data / "data.db") as database:
            database.execute("ALTER TABLE provider_models DROP COLUMN supported_reasoning_efforts")
        before = (self.data / "data.db").read_bytes()
        with self.assertRaisesRegex(KitError, "Unsupported Copilot app database schema"):
            copilot_app.configure(
                self.app(), self.endpoint, copilot_app.fetch_models(self.endpoint)
            )
        self.assertEqual((self.data / "data.db").read_bytes(), before)

    def test_failed_model_update_rolls_back_provider_and_selection(self) -> None:
        self.prepare()
        invalid = copilot_app.Model(
            "test-model", "Test model", "unsupported-wire", None, None, None
        )
        with self.assertRaisesRegex(KitError, "model changes were rolled back"):
            copilot_app.configure(self.app(), self.endpoint, [invalid])
        with sqlite3.connect(self.data / "data.db") as database:
            self.assertEqual(database.execute("SELECT * FROM model_providers").fetchall(), [])
            self.assertEqual(database.execute("SELECT * FROM app_state").fetchall(), [])

    def test_bootstrap_timeout_terminates_its_child_without_final_launch(self) -> None:
        app = self.app()
        app.executable.write_text(
            f"#!{sys.executable}\nimport os, time\nfrom pathlib import Path\n"
            f"Path({str(self.root / 'bootstrap-pid')!r}).write_text(str(os.getpid()))\n"
            "while True: time.sleep(1)\n"
        )
        with self.assertRaisesRegex(KitError, "startup timed out"):
            copilot_app.launch(app, self.endpoint, 1, "fake-open", self.launch, {})
        pid = int((self.root / "bootstrap-pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(self.launched, [])

    def test_interrupted_initial_migration_resumes_without_removing_existing_data(self) -> None:
        app = self.app()
        app.executable.write_text(
            f"#!{sys.executable}\nimport os, sqlite3, time\nfrom pathlib import Path\n"
            "data = Path(os.environ['COPILOT_HOME'])\n"
            "with sqlite3.connect(data / 'data.db') as db:\n"
            "    if (data / 'first-attempt').exists():\n"
            f"        db.executescript({SCHEMA!r})\n"
            "    else:\n"
            "        db.execute('CREATE TABLE retained(value TEXT)')\n"
            "        db.execute(\"INSERT INTO retained VALUES ('preserve me')\")\n"
            "        (data / 'first-attempt').touch()\n"
            "while True: time.sleep(1)\n"
        )
        with self.assertRaisesRegex(KitError, "resume profile setup"):
            copilot_app.launch(app, self.endpoint, 1, "fake-open", self.launch, {})
        self.assertTrue((self.data / copilot_app.BOOTSTRAP_MARKER).exists())
        copilot_app.launch(app, self.endpoint, 3, "fake-open", self.launch, {})
        self.assertFalse((self.data / copilot_app.BOOTSTRAP_MARKER).exists())
        self.assertEqual(len(self.launched), 1)
        with sqlite3.connect(self.data / "data.db") as database:
            self.assertEqual(
                database.execute("SELECT value FROM retained").fetchone()[0], "preserve me"
            )

    def test_catalog_sync_preserves_user_rows_and_valid_model_choice(self) -> None:
        self.prepare()
        with sqlite3.connect(self.data / "data.db") as database:
            database.execute(
                "INSERT INTO model_providers(id,name) VALUES ('user-provider','User choice')"
            )
        app = self.app()
        models = copilot_app.fetch_models(self.endpoint)
        copilot_app.configure(app, self.endpoint, models)
        with sqlite3.connect(self.data / "data.db") as database:
            rows = database.execute(
                "SELECT model_id,wire_api_override,max_prompt_tokens,max_output_tokens,supported_reasoning_efforts FROM provider_models ORDER BY model_id"
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("claude-sonnet-test", "completions", 128000, 16000, '["low", "high"]'),
                    ("gpt-6.1-sol", "responses", 128000, 16000, '["low", "high"]'),
                ],
            )
            settings = json.loads(
                database.execute(
                    "SELECT settings_json FROM model_providers WHERE id=?",
                    (copilot_app.PROVIDER_ID,),
                ).fetchone()[0]
            )
            self.assertEqual(settings["baseUrl"], self.endpoint + "/v1")
            self.assertEqual(settings["authKind"], "none")
            self.assertEqual(
                database.execute("SELECT DISTINCT wire_model FROM provider_models").fetchall(),
                [(None,)],
            )
            choice = f"{copilot_app.PROVIDER_ID}/claude-sonnet-test"
            database.execute(
                "UPDATE app_state SET value=? WHERE key='copilot-selected-model'", (choice,)
            )
        copilot_app.configure(app, "http://127.0.0.1:7777", models)
        self.assertEqual(self.selected(), choice)
        with sqlite3.connect(self.data / "data.db") as database:
            self.assertEqual(
                database.execute(
                    "SELECT name FROM model_providers WHERE id='user-provider'"
                ).fetchone()[0],
                "User choice",
            )
            self.assertEqual(
                database.execute("SELECT count(*) FROM provider_models").fetchone()[0], 2
            )

    def test_previous_wire_override_selection_is_normalized_without_changing_the_model(
        self,
    ) -> None:
        self.prepare()
        app = self.app()
        models = copilot_app.fetch_models(self.endpoint)
        copilot_app.configure(app, self.endpoint, models)
        with sqlite3.connect(self.data / "data.db") as database:
            database.execute("UPDATE provider_models SET wire_model=model_id")
            database.execute(
                "UPDATE app_state SET value=? WHERE key='copilot-selected-model'",
                (f"{copilot_app.PROVIDER_ID}#claude-sonnet-test/claude-sonnet-test",),
            )
        copilot_app.configure(app, self.endpoint, models)
        self.assertEqual(self.selected(), f"{copilot_app.PROVIDER_ID}/claude-sonnet-test")
        with sqlite3.connect(self.data / "data.db") as database:
            self.assertEqual(
                database.execute("SELECT DISTINCT wire_model FROM provider_models").fetchall(),
                [(None,)],
            )

    def test_removed_model_choice_falls_back_to_current_catalog(self) -> None:
        self.prepare()
        app = self.app()
        copilot_app.configure(app, self.endpoint, copilot_app.fetch_models(self.endpoint))
        self.catalog = {"data": [model("claude-sonnet-test", ["/chat/completions"])]}
        copilot_app.configure(app, self.endpoint, copilot_app.fetch_models(self.endpoint))
        self.assertEqual(self.selected(), f"{copilot_app.PROVIDER_ID}/claude-sonnet-test")
        with sqlite3.connect(self.data / "data.db") as database:
            self.assertEqual(
                database.execute("SELECT model_id FROM provider_models").fetchall(),
                [("claude-sonnet-test",)],
            )

    def test_empty_catalog_does_not_create_profile(self) -> None:
        self.catalog = {"data": []}
        with self.assertRaisesRegex(KitError, "supported model catalog"):
            copilot_app.launch(self.app(), self.endpoint, 3, "fake-open", self.launch, {})
        self.assertFalse(self.data.exists())

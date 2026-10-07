"""Launch Copilot with a private profile and managed Headroom model providers."""

import json
import os
import plistlib
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from headroom_kit.proxy import locked, runtime_dir
from headroom_kit.runtime import Config, Json, KitError, say, terminate

MARKER = ".headroom-kit-profile"
MARKER_CONTENT = "copilot-app-v1\n"
BOOTSTRAP_MARKER = ".headroom-kit-bootstrap"
PROVIDER_ID = "88d0af82-5be2-48c7-b3f5-9c1a95986bf7"
SCHEMA_VERSIONS = {156, 166}
SCHEMA_COLUMNS = {
    "model_providers": {
        "id",
        "name",
        "created_at",
        "updated_at",
        "type",
        "settings_json",
        "account_id",
    },
    "provider_models": {
        "id",
        "provider_id",
        "model_id",
        "wire_model",
        "display_name",
        "max_prompt_tokens",
        "max_output_tokens",
        "wire_api_override",
        "created_at",
        "updated_at",
        "supported_reasoning_efforts",
    },
    "app_state": {"key", "value", "updated_at"},
}


@dataclass(frozen=True)
class App:
    bundle: Path
    executable: Path
    bundle_id: str
    data: Path
    protected: tuple[Path, ...]
    lookup: str


@dataclass(frozen=True)
class Model:
    id: str
    name: str
    wire_api: str
    max_prompt_tokens: int | None
    max_output_tokens: int | None
    reasoning: str | None


def check_closed(app: App) -> None:
    result = subprocess.run(
        [app.lookup, "find", f"bundleID={app.bundle_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.stdout.strip():
        raise KitError("Quit GitHub Copilot first, then rerun headroom-kit run copilot-app --.")


def check_profile(data: Path, protected: tuple[Path, ...]) -> None:
    try:
        if data.resolve() != data or any(
            data == normal or data in normal.parents or normal in data.parents
            for normal in protected
        ):
            raise ValueError
        if data.exists() and not data.is_dir():
            raise ValueError
        # Session artifacts and shared skills can contain external links. Check
        # only the files Kit accesses, including SQLite's writable sidecars.
        for name in (
            MARKER,
            BOOTSTRAP_MARKER,
            "data.db",
            "data.db-wal",
            "data.db-shm",
            "data.db-journal",
        ):
            path = data / name
            if not path.resolve().is_relative_to(data):
                raise ValueError
            if path.is_file() and path.stat().st_nlink != 1:
                raise ValueError
        marker = data / MARKER
        if (
            data.exists()
            and any(data.iterdir())
            and (not marker.is_file() or marker.read_text() != MARKER_CONTENT)
        ):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise KitError(
            "HEADROOM_COPILOT_APP_DATA_DIR must be an empty directory or a Kit-owned profile, "
            "separate from normal Copilot data, without links outside the profile."
        ) from None


def preflight(
    cfg: Config,
    platform: str,
    lookup: str,
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> App:
    if platform != "darwin":
        raise KitError("copilot-app supports macOS only. Use headroom-kit run copilot on Linux.")
    home = home or Path.home()
    env = os.environ if env is None else env
    bundle = (
        Path(cfg["copilotAppPath"] or "/Applications/GitHub Copilot.app").expanduser().resolve()
    )
    try:
        with (bundle / "Contents/Info.plist").open("rb") as file:
            info = plistlib.load(file)
        binary = (bundle / "Contents/MacOS" / info["CFBundleExecutable"]).resolve()
        if not binary.is_relative_to(bundle) or not os.access(binary, os.X_OK):
            raise ValueError
        bundle_id = info["CFBundleIdentifier"]
    except (OSError, ValueError, KeyError, TypeError):
        raise KitError(
            "HEADROOM_COPILOT_APP_PATH must point to an installed macOS app bundle."
        ) from None
    base = Path(env.get("XDG_DATA_HOME", str(home / ".local/share")))
    data = (
        Path(cfg["copilotAppDataDir"] or base / "headroom-kit/copilot-app").expanduser().resolve()
    )
    protected = [(home / ".copilot").resolve()]
    if env.get("COPILOT_HOME"):
        protected.append(Path(env["COPILOT_HOME"]).expanduser().resolve())
    app = App(bundle, binary, bundle_id, data, tuple(protected), lookup)
    check_closed(app)
    check_profile(data, app.protected)
    return app


def schema_supported(connection: sqlite3.Connection) -> bool:
    if connection.execute("PRAGMA user_version").fetchone()[0] not in SCHEMA_VERSIONS:
        return False
    return all(
        {row[1] for row in connection.execute(f"PRAGMA table_info({table})")} == columns
        for table, columns in SCHEMA_COLUMNS.items()
    )


def check_database(data: Path) -> bool:
    database = data / "data.db"
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            return schema_supported(connection)
    except sqlite3.Error:
        return False


def bootstrap(app: App, timeout: int, env: Mapping[str, str]) -> None:
    pending = app.data / BOOTSTRAP_MARKER
    if check_database(app.data):
        pending.unlink(missing_ok=True)
        return
    resumable = pending.is_file() and pending.read_text() == MARKER_CONTENT
    if (app.data / "data.db").exists() and not resumable:
        raise KitError(
            "Unsupported Copilot app database schema. The private profile was not changed."
        )
    pending.write_text(MARKER_CONTENT)
    say("Creating an isolated Copilot app profile. Sign in once when the app opens.")
    process = subprocess.Popen(
        [str(app.executable), "--config-dir", str(app.data)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + timeout
        while not check_database(app.data):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise KitError(
                    "Copilot could not create a supported private profile before startup timed out. "
                    "Rerun headroom-kit run copilot-app -- to resume profile setup."
                )
            time.sleep(0.1)
    finally:
        terminate(process, group=True)
    check_closed(app)
    pending.unlink()


def positive_limit(value: Json) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def catalog_model(value: Json) -> Model | None:
    if not isinstance(value, dict) or value.get("model_picker_enabled") is not True:
        return None
    model_id = value.get("id")
    endpoints = value.get("supported_endpoints", [])
    if (
        not isinstance(model_id, str)
        or not model_id
        or model_id == "auto"
        or not isinstance(endpoints, list)
    ):
        return None
    wire = (
        "completions"
        if "/chat/completions" in endpoints
        else "responses"
        if "/responses" in endpoints
        else None
    )
    if wire is None:
        return None
    capabilities = value.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return None
    limits = capabilities.get("limits", {})
    supports = capabilities.get("supports", {})
    if not isinstance(limits, dict) or not isinstance(supports, dict):
        return None
    reasoning = supports.get("reasoning_effort")
    if not isinstance(reasoning, list) or not all(isinstance(item, str) for item in reasoning):
        reasoning = None
    name = value.get("name")
    return Model(
        model_id,
        name if isinstance(name, str) else model_id,
        wire,
        positive_limit(limits.get("max_prompt_tokens")),
        positive_limit(limits.get("max_output_tokens")),
        json.dumps(reasoning) if reasoning else None,
    )


def fetch_models(endpoint: str) -> list[Model]:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint + "/v1/models", timeout=30) as response:
            catalog = json.load(response)
        if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list):
            raise ValueError
        models = {model.id: model for value in catalog["data"] if (model := catalog_model(value))}
        if not models:
            raise ValueError
        return list(models.values())
    except (OSError, urllib.error.URLError, ValueError):
        raise KitError(
            "Could not read a supported model catalog from the local Copilot proxy."
        ) from None


def sync_models(connection: sqlite3.Connection, models: list[Model]) -> None:
    for model in models:
        connection.execute(
            """INSERT INTO provider_models
               (id, provider_id, model_id, wire_model, display_name, max_prompt_tokens,
                max_output_tokens, wire_api_override, supported_reasoning_efforts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider_id, model_id) DO UPDATE SET
                 wire_model=excluded.wire_model, display_name=excluded.display_name,
                 max_prompt_tokens=excluded.max_prompt_tokens, max_output_tokens=excluded.max_output_tokens,
                 wire_api_override=excluded.wire_api_override,
                 supported_reasoning_efforts=excluded.supported_reasoning_efforts,
                 updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
            (
                str(uuid.uuid5(uuid.UUID(PROVIDER_ID), model.id)),
                PROVIDER_ID,
                model.id,
                None,
                model.name,
                model.max_prompt_tokens,
                model.max_output_tokens,
                model.wire_api,
                model.reasoning,
            ),
        )
    placeholders = ",".join("?" for _ in models)
    connection.execute(
        f"DELETE FROM provider_models WHERE provider_id=? AND model_id NOT IN ({placeholders})",
        (PROVIDER_ID, *(model.id for model in models)),
    )
    choices = [f"{PROVIDER_ID}/{model.id}" for model in models]
    selected = connection.execute(
        "SELECT value FROM app_state WHERE key='copilot-selected-model'"
    ).fetchone()
    if selected and selected[0] in choices:
        return
    preferred = f"{PROVIDER_ID}/gpt-6.1-sol"
    default = preferred if preferred in choices else sorted(choices)[0]
    previous = {
        f"{PROVIDER_ID}#{model.id}/{model.id}": f"{PROVIDER_ID}/{model.id}" for model in models
    }
    if selected:
        default = previous.get(selected[0], default)
    connection.execute(
        """INSERT INTO app_state(key, value) VALUES ('copilot-selected-model', ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,
           updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
        (default,),
    )


def configure(app: App, endpoint: str, models: list[Model]) -> None:
    check_closed(app)
    check_profile(app.data, app.protected)
    try:
        with sqlite3.connect((app.data / "data.db").as_uri() + "?mode=rw", uri=True) as connection:
            if not schema_supported(connection):
                raise KitError(
                    "Unsupported Copilot app database schema. The private profile was not changed."
                )
            connection.execute("PRAGMA foreign_keys=ON")
            settings = json.dumps(
                {
                    "authKind": "none",
                    "baseUrl": endpoint + "/v1",
                    "headersJson": "{}",
                    "wireApi": "completions",
                }
            )
            connection.execute(
                """INSERT INTO model_providers(id, name, type, settings_json)
                   VALUES (?, 'Headroom Copilot', 'custom', ?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type,
                   settings_json=excluded.settings_json, account_id=NULL,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')""",
                (PROVIDER_ID, settings),
            )
            sync_models(connection, models)
    except sqlite3.Error:
        raise KitError(
            "Could not configure the private Copilot profile. Its model changes were rolled back."
        ) from None


def launch(
    app: App,
    endpoint: str,
    timeout: int,
    open_command: str,
    run: Callable[[Sequence[str], Mapping[str, str] | None], int],
    env: Mapping[str, str],
) -> int:
    with locked(
        runtime_dir() / "copilot-app.lock", "Timed out waiting for Copilot app startup.", timeout
    ):
        check_closed(app)
        check_profile(app.data, app.protected)
        models = fetch_models(endpoint)
        app.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = app.data / MARKER
        if not marker.exists():
            marker.write_text(MARKER_CONTENT)
        child = {
            key: value for key, value in env.items() if not key.startswith("COPILOT_PROVIDER_")
        }
        for key in ("ELECTRON_RUN_AS_NODE", "COPILOT_API_URL"):
            child.pop(key, None)
        child["COPILOT_HOME"] = str(app.data)
        bootstrap(app, timeout, child)
        configure(app, endpoint, models)
        args = [open_command, "-n", "--env", f"COPILOT_HOME={app.data}"]
        for key in ("NO_PROXY", "no_proxy"):
            if key in child:
                args += ["--env", f"{key}={child[key]}"]
        say(f"Launching Copilot with its Headroom profile: {app.data}")
        return run([*args, "-a", str(app.bundle), "--args", "--config-dir", str(app.data)], child)

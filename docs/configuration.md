# Configuration

A normal install needs no config file. The built-in defaults are below.

## Precedence

1. `HEADROOM_*` environment variables for one launch
2. `--config PATH`
3. Built-in defaults

The file is a JSON object. It holds non-secret integration defaults only: executable names, ports, and editor paths. Do not put tokens, passwords, or provider keys in it.

```json
{
  "startupTimeout": 180,
  "codexExecutable": "codex",
  "codexPort": 8788,
  "copilotPort": 8787,
  "piPort": 8790,
  "opencodePort": 8791,
  "vscodeChannel": "stable",
  "vscodePort": 8787
}
```

```sh
headroom-kit --config ./headroom-kit.json run codex --
HEADROOM_CODEX_PORT=8789 headroom-kit run codex --
```

Unset a variable to use the file or the built-in default. An empty value is an error. Unknown keys are errors. Validation happens in the CLI.

## Runtime versions

Kit requires the Headroom version pinned in `pyproject.toml`, installed with Kit. `headroom-kit --version` prints the Kit version and the installed Headroom version. It does not start a proxy, and `HEADROOM_VERSION` does not change the reported version.

These are rejected, with a message that tells you what to remove:

| Rejected setting | What to use instead |
| --- | --- |
| `HEADROOM_VERSION` | Install Kit with its pinned Headroom dependency. Do not select a version at launch. |
| config `version` | Same. The dependency pin is the version. |
| config `uv` | Not used. Kit does not download Python packages at launch. |
| config `python` | Not used. Detached processes use the interpreter that installed Kit. |

`headroom` remains Headroom's own command. Kit does not wrap or replace it. Copilot login is `headroom-kit copilot-auth login`, which runs in this install and uses Headroom's TLS handling. A separate `headroom` executable is not required and is not on `PATH` after `uv tool install`.

Headroom may download model files, such as Kompress weights, the first time a proxy starts. That is Headroom's download, not a Kit package install. Proxy readiness does not mean the model is ready.

## Options

| Environment variable | Default | Config key |
| --- | --- | --- |
| `HEADROOM_STARTUP_TIMEOUT` | 180 seconds | `startupTimeout` |
| `HEADROOM_CODEX_EXECUTABLE` | `codex` | `codexExecutable` |
| `HEADROOM_CODEX_PORT` | 8788 | `codexPort` |
| `HEADROOM_CODEX_APP_PATH` | macOS bundle `com.openai.codex` | `codexAppPath` |
| `HEADROOM_COPILOT_EXECUTABLE` | `copilot` | `copilotExecutable` |
| `HEADROOM_COPILOT_PORT` | 8787 | `copilotPort` |
| `HEADROOM_COPILOT_APP_PATH` | `/Applications/GitHub Copilot.app` | `copilotAppPath` |
| `HEADROOM_COPILOT_APP_DATA_DIR` | `$XDG_DATA_HOME/headroom-kit/copilot-app`, or `~/.local/share/headroom-kit/copilot-app` | `copilotAppDataDir` |
| `HEADROOM_PI_EXECUTABLE` | `pi` | `piExecutable` |
| `HEADROOM_PI_PORT` | 8790 | `piPort` |
| `HEADROOM_OPENCODE_EXECUTABLE` | `opencode` | `opencodeExecutable` |
| `HEADROOM_OPENCODE_PORT` | 8791 | `opencodePort` |
| `HEADROOM_VSCODE_CHANNEL` | `stable` or `insiders` | `vscodeChannel` |
| `HEADROOM_VSCODE_EXECUTABLE` | `code` or `code-insiders` | `vscodeExecutable` |
| `HEADROOM_VSCODE_PORT` | 8787 | `vscodePort` |
| `HEADROOM_VSCODE_USER_DATA_DIR` | dedicated profile | `vscodeUserDataDir` |
| `HEADROOM_VSCODE_EXTENSIONS_DIR` | channel extensions | `vscodeExtensionsDir` |

Ports are 1–65535. Codex, Pi, and OpenCode each need their own port. Copilot CLI, the Copilot app, and the editor can share a port when they use the same Headroom OAuth credential. Executables are names on `PATH` or a single path, not a shell command.

The Copilot app data directory must be empty or already managed by Kit. It must be separate from `~/.copilot` and any inherited `COPILOT_HOME`, including their ancestors and children. Kit rejects links that escape the private profile. Do not copy your normal app database into this directory; sign in once in the new profile instead.

## Compression and storage

Managed proxies use Headroom's `coding` profile unless you override it. Explicit `HEADROOM_*` compression and metrics variables pass through. Routing, auth, privacy, process settings, semantic caching, and rate limiting stay under Kit's control.

OpenAI routes stay lossless because CCR retrieval does not cover those paths, including OpenAI-wire Copilot. Setting a more aggressive profile does not bypass that.

To change a setting that is part of the proxy identity, stop the proxy and launch again:

```sh
headroom-kit stop 8787
HEADROOM_SAVINGS_PROFILE=balanced headroom-kit run copilot --
```

Stopping interrupts attached clients. Clients that share a port must use matching settings.

| Environment variable | Managed proxy default |
| --- | --- |
| `HEADROOM_STATELESS` | Unset, so persistence stays on. Set `1` to opt out. |
| `HEADROOM_TELEMETRY` | `on` for local telemetry. Set `off` to disable it. |
| `HEADROOM_WORKSPACE_DIR` | `~/.headroom` |
| `HEADROOM_SAVINGS_PATH` | `<workspace>/headroom-kit/<port>/proxy_savings.json` |
| `HEADROOM_SAVINGS_EVENTS_PATH` | `<workspace>/savings_events.jsonl` |

Kit also forces `HEADROOM_BEACON=off`, disables message and wire logs, and sets `DO_NOT_TRACK=1`. Credentials stay out of the control socket. Locks and sockets live in `/tmp/headroom-kit-<uid>`.

# Validation

These checks describe what this package covers. They do not add providers or claim to fix open provider bugs.

## What CI runs

Linux and macOS CLI lifecycle checks run in GitHub Actions on `ubuntu-latest` and `macos-14`. Those checks use stand-ins for the agents and, when `HEADROOM_KIT_INSTALL=1`, install the wheel and sdist and start a real Headroom proxy from an unrelated directory.

Live GUI checks are not part of that matrix. `codex-app` and `copilot-app` are macOS-only. Copilot app tests use a stand-in app, a local model catalog, and SQLite fixtures to check profile isolation, provider updates, schema rejection, and interrupted setup. VS Code isolation is covered by unit tests with a stand-in editor.

Manual checks with Copilot app 1.1.26 verified GPT Responses and Claude Chat Completions through Headroom, preserved the normal profile's files during isolated launches, and confirmed that a normal app launch still sends requests directly to Copilot.

For Copilot app 1.1.27, read-only inspection of schema 166 confirmed that the managed tables retain the supported columns, types, defaults, foreign keys, and unique constraints. Its app-state triggers affect a different key from Kit's model selection. Fixture tests cover schema 166 configuration without changing the schema version, unknown-version rejection, and changed-column rejection for both supported versions. Live routing with 1.1.27 has not yet been verified.

## Not verified

- Windows. It is not a target of this package.
- Live Linux agent sessions. The CLI lifecycle checks run there; the real Codex, Copilot, Pi, and OpenCode sessions were exercised on macOS.
- Other GUI chat paths, remote editor hosts, and Copilot enterprise domains.
- Every model and interactive model switch.
- Compression quality under load.

Known limits:

- Custom CA negotiation can look like an authentication failure.
- Older Copilot CLIs can ignore `COPILOT_API_URL`.
- Live routing and interactive model switching are not fully checked.

OpenAI routes stay lossless because retrieval is incomplete on those paths.

# Validation

These checks describe what this package covers. They do not add providers or claim to fix open provider bugs.

## What CI runs

Linux and macOS CLI lifecycle checks run in GitHub Actions on `ubuntu-latest` and `macos-14`. Those checks use stand-ins for the agents and, when `HEADROOM_KIT_INSTALL=1`, install the wheel and sdist and start a real Headroom proxy from an unrelated directory.

GUI checks are not part of that matrix. `codex-app` is macOS-only. VS Code isolation is covered by unit tests with a stand-in editor, not by launching a real GUI in CI.

## Not verified

- Windows. It is not a target of this package.
- Live Linux agent sessions. The CLI lifecycle checks run there; the real Codex, Copilot, Pi, and OpenCode sessions were exercised on macOS.
- GUI chat, remote editor hosts, and Copilot enterprise domains.
- Every model and interactive model switch.
- Compression quality under load.

Known limits:

- Custom CA negotiation can look like an authentication failure.
- Older Copilot CLIs can ignore `COPILOT_API_URL`.
- Live routing and interactive model switching are not fully checked.

OpenAI routes stay lossless on Headroom 0.39.1 because retrieval is incomplete on those paths.

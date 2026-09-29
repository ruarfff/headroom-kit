---
name: install-headroom-kit
description: Install and check the headroom-kit CLI for an existing coding agent. Use when a user asks to install headroom-kit, set up Headroom Kit, or run codex, copilot, pi, opencode, or VS Code through headroom-kit.
---

# Install headroom-kit

Install the standalone CLI and prove the tested Headroom release is importable. Leave agent installation, sign-in, and proxy startup to the user unless they ask for a launch.

## Procedure

1. Inspect the environment before changing it. Read agent instructions and `git status`. Preserve unrelated work. Search for an existing `headroom-kit` command.

   Complete when you know whether the CLI is already installed and whether the user named an agent.

2. Install only when the command is missing or the user asked to upgrade. Prefer:

   ```sh
   uv tool install --python 3.13 headroom-kit
   ```

   To upgrade an existing tool install, run `uv tool upgrade headroom-kit`.

   Use the project's virtual environment instead when the user wants Kit local to that project:

   ```sh
   uv venv --python 3.13
   uv pip install headroom-kit
   . .venv/bin/activate
   ```

   If `uv` is missing, say so and stop. Do not install agents, Nix, or a second `headroom` command. Kit must import `headroom-ai` from the same environment. A separate `headroom` executable is not enough. Copilot login is `headroom-kit copilot-auth login` from that install.

   Complete when the chosen install command finishes, or when you have reported that `uv` or Python 3.13 is unavailable.

3. Check versions without starting a proxy:

   ```sh
   headroom-kit --version
   ```

   Require `headroom-ai 0.39.1`. If `HEADROOM_VERSION` is set, or a config file contains `version`, `uv`, or `python`, tell the user to remove that setting. Do not continue a launch while it remains.

   Complete when both versions are printed and any rejected setting is reported.

4. Give the launch command for the named agent. If none was named, ask which one. Do not launch it unless the user asked.

   | Agent | Command | Prerequisite |
   | --- | --- | --- |
   | `codex` | `headroom-kit run codex --` | Existing `codex login`. Built-in OpenAI provider only. |
   | `copilot` | `headroom-kit run copilot --` | `headroom-kit copilot-auth login`, then a Copilot CLI that honours `COPILOT_API_URL`. |
   | `pi` | `headroom-kit run pi -- --provider <provider> --model <model-id>` | Pi installed. Copilot routing uses `headroom-kit copilot-auth login`. |
   | `opencode` | `headroom-kit run opencode --` | OpenCode v2. |
   | `copilot-vscode` | `headroom-kit run copilot-vscode -- .` | `code` or `code-insiders`. Isolated profile, not normal editor data. |
   | `codex-app` | `headroom-kit run codex-app --` | macOS only. Quit Codex first. |

   Arguments after `--` are forwarded unchanged. `headroom-kit run <agent> -- --help` must not start a proxy.

   Complete when the user has one command and its prerequisite, and no agent was started unless requested.

5. Explain proxy control without changing a running process:

   ```sh
   headroom-kit status
   headroom-kit stop <port>
   ```

   Shared proxies stay up after the client exits. `stop` interrupts every client on that port. It does not adopt or stop an unrelated listener. An incompatible managed proxy must be stopped explicitly before reuse. Do not run `stop` unless the user asks.

   Complete when the report names the installed Kit and Headroom versions, the launch command, and any rejected setting. Do not include credentials or raw provider output.

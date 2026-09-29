# Headroom Kit

[![PyPI - Version](https://img.shields.io/pypi/v/headroom-kit)](https://pypi.org/project/headroom-kit/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/headroom-kit)](https://pypi.org/project/headroom-kit/)
[![skills.sh](https://skills.sh/b/ruarfff/headroom-kit)](https://skills.sh/ruarfff/headroom-kit)
[![License: MIT](https://img.shields.io/github/license/ruarfff/headroom-kit)](LICENSE)

Run an existing coding agent through a local [Headroom](https://github.com/headroomlabs-ai/headroom) proxy. Kit owns routing, Copilot authentication, editor isolation, and the shared proxy. Headroom owns compression and metrics. This package does not install agents, replace the `headroom` command, or download Python packages when an agent launches.

```text
agent -> headroom-kit -> Headroom on 127.0.0.1 -> model provider
```

## Install

You need Python 3.13 and the agent you want to launch. Kit and Headroom 0.39.1 install into the same environment:

```sh
uv tool install --python 3.13 headroom-kit
```

A normal virtual environment works too:

```sh
uv venv --python 3.13
uv pip install headroom-kit
. .venv/bin/activate
```

Do not point Kit at a separate `headroom` executable. It imports Headroom's Python modules. Copilot login is `headroom-kit copilot-auth login` in that same install. Headroom may still download its own model files on first proxy startup. That is separate from installing this package.

Check both versions without starting a proxy:

```sh
headroom-kit --version
```

## Set up with an agent

This repository includes `install-headroom-kit`, an Agent Skill that installs the CLI and checks the Headroom version without starting a proxy.

The [`skills` CLI](https://github.com/vercel-labs/skills) can install it for the current project:

```sh
npx skills add ruarfff/headroom-kit --skill install-headroom-kit
```

Make it available to your agents:

```sh
npx skills add ruarfff/headroom-kit --skill install-headroom-kit --agent '*' --global --yes
```

See the [`skills` CLI documentation](https://github.com/vercel-labs/skills#supported-agents).

## Commands

```text
headroom-kit [--config PATH] run <agent> -- <agent arguments...>
headroom-kit copilot-auth login
headroom-kit status
headroom-kit stop <port>
```

Agents: `codex`, `codex-app`, `copilot`, `copilot-vscode`, `pi`, `opencode`.

```sh
headroom-kit copilot-auth login
headroom-kit run codex --
headroom-kit run copilot -- --model auto
headroom-kit run pi -- --provider openai --model <model-id>
headroom-kit run opencode --
headroom-kit status
headroom-kit stop 8787
```

Arguments after `--` are forwarded unchanged. `headroom-kit run codex -- --help` shows Codex help and does not start a proxy. `status` and `stop` do not need provider credentials or model downloads.

Shared proxies stay up after the client exits. Stopping one interrupts every client on that port. Kit will not adopt or stop an unrelated listener. An incompatible managed proxy must be stopped explicitly before a new one can use the port.

## Configure

Built-in defaults are enough for a normal install. `--config` accepts a JSON object of non-secret integration defaults. Precedence is `HEADROOM_*` environment variables, then the file, then the built-in defaults. See [configuration](docs/configuration.md).

`HEADROOM_VERSION`, and config keys `version`, `uv`, and `python`, are rejected. Install the tested Headroom release instead of selecting one at launch.

## Agents

Install and sign in to the agent yourself. Kit does not install it.

| Agent | Prerequisite |
| --- | --- |
| `codex` | Existing Codex sign-in. Built-in OpenAI provider only. |
| `copilot` | `headroom-kit copilot-auth login`, then a Copilot CLI that honours `COPILOT_API_URL`. |
| `pi` | Pi installed. Copilot routing uses `headroom-kit copilot-auth login`. |
| `opencode` | OpenCode v2. |
| `copilot-vscode` | `code` or `code-insiders`. Uses an isolated profile, not your normal editor data. |
| `codex-app` | macOS only. Quit Codex before launch. |

Windows is not part of this package. Linux and macOS CLI lifecycle checks run in CI. GUI behaviour is platform-specific; live Linux agent support is not verified. Details are in [usage](docs/usage.md) and [validation](docs/validation.md).

[MIT](LICENSE). See [NOTICE](NOTICE.md) for attribution.

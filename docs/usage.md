# Usage

Install Kit first. See the [README](../README.md). These examples assume `headroom-kit` is on `PATH`.

## Codex CLI

Sign in with `codex login`, then launch from the project:

```sh
headroom-kit run codex --
headroom-kit run codex -- resume --last
```

Kit keeps the sign-in, history, working directory, arguments, and terminal. Help and version skip the proxy. Only the built-in OpenAI provider is routed. Use normal `codex` for custom or local providers.

## Copilot CLI

1. Sign in with normal `copilot`, then exit it.
2. Authorize Copilot in the same install: `headroom-kit copilot-auth login`
3. Launch:

```sh
headroom-kit run copilot --
headroom-kit run copilot -- --model auto
headroom-kit run copilot -- --model <model-id>
```

Use the same account in Copilot and Headroom. Routed requests use Headroom's OAuth credential. Copilot keeps its catalog and chooses the wire API. Kit removes inherited `COPILOT_PROVIDER_*` settings so BYOK does not replace that route. The CLI must honour `COPILOT_API_URL`. 1.0.87-0 was tested. Older builds can ignore it.

`headroom-kit copilot-auth` runs Headroom's login in the installed environment. Headroom uses the configured custom CA and HTTP/1.1 for urllib requests. Check `SSL_CERT_FILE` before assuming saved credentials were rejected. Do not disable certificate verification. `headroom-kit copilot-auth status` reads the saved token and does not start a proxy. Do not install a separate `headroom` command for this.

## Pi

```sh
headroom-kit run pi -- --provider openai --model <model-id>
headroom-kit run pi -- --provider anthropic --model <model-id>
headroom-kit run pi -- --provider github-copilot --model <model-id>
```

Kit loads its installed extension for that process. The extension path comes from the installed package, not the working directory. Copilot uses Headroom's login. Other providers keep their own credentials. Tested with Pi 0.85.1.

## OpenCode v2

Install OpenCode v2 and connect accounts with normal OpenCode. Then:

```sh
headroom-kit run opencode --
headroom-kit run opencode -- run --model opencode/<model-id>
```

Kit adds its installed plugin on top of `OPENCODE_CONFIG_CONTENT`. It starts a private server and rejects `--server` and shared-server flags. Tested with OpenCode 2.0.3 and later 2.0.11 plugin behaviour. v1 is rejected.

## VS Code

```sh
headroom-kit run copilot-vscode -- .
```

This opens an isolated profile. It must not be your normal Code or Code - Insiders user-data directory. Kit configures that isolated profile to use the shared Copilot proxy. Extensions can come from the normal channel directory. Do not pass editor flags; set `HEADROOM_VSCODE_*` instead.

## Codex macOS app

Quit Codex, then:

```sh
headroom-kit run codex-app --
```

macOS only. An already running app cannot receive the launch settings. This route is experimental.

## Proxy lifetime

The first client starts a proxy and prints `Started Headroom`. Later clients on the same port print `Reusing Headroom` and leave it running when they exit.

```sh
headroom-kit status
headroom-kit stop 8788
```

`stop` interrupts every client on that port. It only stops a proxy Kit started and can still talk to. An unrelated listener is left alone. If the running proxy was started by another Kit build, or its compression and credential settings do not match, launch fails and tells you to stop it. Stop it, then launch again. Do not kill processes by port number yourself unless you have identified them.

A package upgrade does not attach to an incompatible managed proxy. Stop it explicitly before using the new install.

Installation does not start an agent or delete `~/.headroom`.

# Development

Use Python 3.13 and uv. From a checkout:

```sh
uv sync --locked --dev
uv run headroom-kit --help
uv run pytest
uv run ruff format --check .
uv run ruff check .
node --test tests/test_client_adapters.mjs
uv run pre-commit run anti-slop-python --all-files
```

`uv sync` installs Kit and `headroom-ai[proxy,code]==0.37.0` into `.venv`. The `code` extra supplies the tree-sitter parsers used for AST-aware compression. Launches use that interpreter. They do not call uv.

Unit tests use stand-ins. They do not need provider credentials. The install test builds the wheel and sdist and installs them into clean environments. It is skipped unless you set `HEADROOM_KIT_INSTALL=1`. CI sets that.

## Opt-in runtime checks

These are not part of the default test run.

`tests/runtime_smoke.py <prefix>` expects an install prefix with `bin/headroom-kit` and `bin/python`. It checks version output, proxy reuse, and explicit stop. It can download Headroom model files.

`tests/smoke_compression.py` compares compression behaviour against the installed Headroom. It uses temporary homes and local HTTP servers, not live accounts.

`tests/agent_routing_smoke.py` and `tests/shared_agent_smoke.py` need macOS and the real CLIs. `tests/qa_share.py` makes live provider calls and can cost money. `tests/copilot_models_smoke.py <prefix> <model...>` also makes paid Copilot calls.

## Releases

Pushes to `main` run checks, then the publish workflow. The first tag is `v0.1.0`. Later tags bump the patch. A rerun reuses a tag or GitHub release that already exists and skips a PyPI version that is already published.

The publish job uses PyPI Trusted Publishing. The owner configures the pending publisher and the `pypi` GitHub environment. The workflow does not use a long-lived PyPI token.

Docs, the license, and `NOTICE.md` do not cut a release. Kit tags do not change the Headroom pin.

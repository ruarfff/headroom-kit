"""Exercise pinned Headroom's Copilot refresh with fake credentials and local HTTP."""

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headroom_kit.copilot import managed_copilot_auth


def main() -> None:
    from headroom import copilot_auth

    exchanges = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            exchanges.append(
                (self.headers["Authorization"], self.headers["Copilot-Integration-Id"])
            )
            body = json.dumps(
                {
                    "token": f"tid_fake_refreshed_{len(exchanges)}",
                    "expires_at": time.time() + 3600,
                    "endpoints": {"api": "https://api.githubcopilot.com"},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: str | int) -> None:
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        os.environ.update(
            GITHUB_COPILOT_API_TOKEN="headroom-kit-expired-seed",
            GITHUB_COPILOT_API_TOKEN_EXPIRES_AT="0",
            GITHUB_COPILOT_REFRESH_OAUTH_TOKEN="fake-pinned-account",
            GITHUB_COPILOT_TOKEN_EXCHANGE_URL=f"http://127.0.0.1:{server.server_port}/token",
        )
        managed_copilot_auth()
        try:
            asyncio.run(check_auth(copilot_auth, exchanges))
        finally:
            server.shutdown()
            thread.join()
    print(
        "Real Headroom Copilot adapter: pinned account, per-client integration, refresh, and foreign-route preservation PASS"
    )


async def check_auth(adapter: ModuleType, exchanges: list[tuple[str, str]]) -> None:
    for index, (client, integration) in enumerate(
        [
            ("headroom-kit", "copilot-cli"),
            ("tid_fake_rotated_client", "copilot-cli"),
            ("gho_fake_other_editor_account", "vscode-chat"),
        ]
    ):
        for path in ("/responses", "/chat/completions", "/v1/messages", "/models"):
            result = await adapter.apply_copilot_api_auth(
                {
                    "Authorization": f"Bearer {client}",
                    "x-api-key": "fake-client-key",
                    "Copilot-Integration-Id": integration,
                },
                url="https://api.githubcopilot.com" + path,
            )
            expected = 1 if index < 2 else 2
            assert result["Authorization"] == f"Bearer tid_fake_refreshed_{expected}"
            assert result["Copilot-Integration-Id"] == integration
            assert "x-api-key" not in result
    assert exchanges == [
        ("Bearer fake-pinned-account", "copilot-cli"),
        ("Bearer fake-pinned-account", "vscode-chat"),
    ]
    provider = adapter.get_copilot_token_provider()
    provider._cached_by_integration["copilot-cli"] = replace(
        provider._cached_by_integration["copilot-cli"], expires_at=0
    )
    refreshed = await adapter.apply_copilot_api_auth(
        {"Authorization": "Bearer tid_fake_old_client", "Copilot-Integration-Id": "copilot-cli"},
        url="https://api.githubcopilot.com/responses",
    )
    assert refreshed["Authorization"] == "Bearer tid_fake_refreshed_3"
    original = {"Authorization": "Bearer fake-other-provider"}
    assert (
        await adapter.apply_copilot_api_auth(original, url="http://127.0.0.1/unrelated") == original
    )


if __name__ == "__main__":
    main()

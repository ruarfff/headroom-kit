"""Adapt Headroom's TLS and subscription auth without changing its installed files."""

import contextlib
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from http.client import HTTPException, HTTPResponse
from types import ModuleType

from headroom_kit.runtime import KitError

LOGIN_COMMAND = "headroom-kit copilot-auth login"


@dataclass(frozen=True)
class CopilotAuth:
    api_url: str
    refresh_oauth_token: str | None


def configure_urllib_tls() -> None:
    from headroom.proxy import ssl_context

    def build_context() -> ssl.SSLContext | None:
        # Headroom 0.37.0 shares httpx's ALPN list with urllib, which cannot speak h2.
        context = ssl_context.build_httpx_verify()
        if isinstance(context, ssl.SSLContext):
            context.set_alpn_protocols(["http/1.1"])
            return context
        return None

    ssl_context.build_urlopen_context = build_context


def run_copilot_auth(args: list[str]) -> int:
    """Run Headroom's Copilot auth in this interpreter."""
    configure_urllib_tls()
    try:
        from click import ClickException
        from headroom.cli.copilot_auth import copilot_auth
    except ImportError:
        raise KitError(
            "This Headroom release lacks the required Copilot subscription API. "
            "Install headroom-ai 0.37.0 in this environment."
        ) from None
    try:
        code = copilot_auth.main(
            args=args,
            prog_name="headroom-kit copilot-auth",
            standalone_mode=False,
        )
    except ClickException as exc:
        exc.show()
        return exc.exit_code
    return 0 if code is None else int(code)


@contextlib.contextmanager
def auth_failures(adapter: ModuleType) -> Iterator[set[str]]:
    upstream_urlopen = adapter._urlopen
    failures: set[str] = set()

    @contextlib.contextmanager
    def urlopen(request: urllib.request.Request, *, timeout: float) -> Iterator[HTTPResponse]:
        try:
            with upstream_urlopen(request, timeout=timeout) as response:
                yield response
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                failures.add("service")
            error.close()
            # Upstream catches and logs exchange failures, including response bodies.
            raise OSError("Copilot authorization HTTP request failed.") from None
        except (OSError, HTTPException):
            failures.add("transport")
            raise OSError("Copilot authorization transport failed.") from None

    adapter._urlopen = urlopen
    try:
        yield failures
    finally:
        adapter._urlopen = upstream_urlopen


def copilot_auth() -> CopilotAuth:
    # No upstream wrapper lifecycle: it can restart proxies or edit normal config.
    try:
        from headroom import copilot_auth as adapter

        configure_urllib_tls()
    except ImportError:
        raise KitError(
            "This Headroom release lacks the required Copilot subscription API. "
            "Install headroom-ai 0.37.0 in this environment."
        ) from None
    try:
        with (
            auth_failures(adapter) as failures,
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            resolution = adapter.resolve_subscription_bearer_token_details()
        if resolution is None:
            if failures:
                failure = "transport" if "transport" in failures else "service"
                raise KitError(
                    f"Copilot authorization {failure} failed. "
                    "Check network, proxy, CA settings, and GitHub service availability; retry. "
                    "This does not establish that your saved credentials were rejected."
                )
            raise ValueError
        parsed = urllib.parse.urlsplit(resolution.api_url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".githubcopilot.com")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        if not resolution.refresh_oauth_token:
            raise ValueError
        # Pin the reusable credential, not a rotating access token.
        return CopilotAuth(
            api_url=resolution.api_url,
            refresh_oauth_token=resolution.refresh_oauth_token,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        raise KitError(
            "Copilot subscription authorization failed or its endpoint is unsupported. "
            f"Run `{LOGIN_COMMAND}`, check your subscription, and retry. "
            "Shared proxies require reusable OAuth and HTTPS *.githubcopilot.com upstreams."
        ) from None


def managed_copilot_auth() -> None:
    from headroom import copilot_auth as adapter

    configure_urllib_tls()
    upstream_auth = adapter.apply_copilot_api_auth

    async def authenticate(headers: dict[str, str], *, url: str) -> dict[str, str]:
        # CLI placeholders and editor tokens both use this proxy's pinned account.
        if adapter.is_copilot_upstream_url(url):
            headers = {
                k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key")
            }
        return await upstream_auth(headers, url=url)

    adapter.apply_copilot_api_auth = authenticate

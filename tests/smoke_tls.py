"""Real Headroom TLS regression: local HTTPS, temporary CA, no saved credentials."""

import contextlib
import io
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from headroom_kit.copilot import auth_failures


class Handler(BaseHTTPRequestHandler):
    status = 401
    disconnect = False
    truncated = False
    protocols: list[str | None] = []

    def do_GET(self) -> None:
        protocol = self.connection.selected_alpn_protocol()
        self.protocols.append(protocol)
        if protocol == "h2" or self.disconnect:
            self.close_connection = True
            return
        self.send_response(self.status)
        if self.truncated:
            self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"fake-private-response-body")

    def log_message(self, format: str, *args: str | int) -> None:
        pass


def certificate(root: Path) -> tuple[Path, Path]:
    cert, key = root / "ca.pem", root / "key.pem"
    subprocess.run(
        [
            os.environ.get("KIT_TEST_OPENSSL", "openssl"),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,digitalSignature,keyEncipherment",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


class TLSRegression(unittest.TestCase):
    def setUp(self) -> None:
        from headroom import copilot_auth
        from headroom.proxy import ssl_context

        self.adapter, self.ssl_context = copilot_auth, ssl_context
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cert, key = certificate(Path(temporary.name))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        context.set_alpn_protocols(["h2", "http/1.1"])
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"https://localhost:{self.server.server_port}/token"
        Handler.status, Handler.disconnect, Handler.truncated = 401, False, False
        Handler.protocols.clear()
        self.environment({})

    def environment(self, values: dict[str, str]) -> None:
        openssl = os.environ.get("KIT_TEST_OPENSSL") or shutil.which("openssl")
        if openssl is None:
            raise RuntimeError("openssl is required.")
        os.environ.clear()
        os.environ.update(values, KIT_TEST_OPENSSL=openssl)
        urllib.request.install_opener(None)

    def request(self, url: str | None = None) -> None:
        with self.adapter._urlopen(urllib.request.Request(url or self.url), timeout=3):
            pass

    def test_ca_roots_verification_and_alpn(self) -> None:
        with self.assertRaises(urllib.error.URLError):
            urllib.request.urlopen(self.url, context=ssl.create_default_context(), timeout=3)
        for variable in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
            with self.subTest(variable=variable):
                self.environment({variable: str(self.cert)})
                httpx_context = self.ssl_context.build_httpx_verify()
                with self.assertRaises(RemoteDisconnected):
                    urllib.request.urlopen(self.url, context=httpx_context, timeout=3)
                self.assertEqual(Handler.protocols[-1], "h2")
                context = self.ssl_context.build_urlopen_context()
                self.assertEqual(
                    set(context.get_ca_certs(binary_form=True)),
                    set(httpx_context.get_ca_certs(binary_form=True)),
                )
                self.assertEqual(context.verify_flags, httpx_context.verify_flags)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    self.request()
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
                self.assertEqual(Handler.protocols[-1], "http/1.1")
                with self.assertRaises(urllib.error.URLError):
                    self.request(self.url.replace("localhost", "127.0.0.1"))
        self.environment({})
        self.assertIsNone(self.ssl_context.build_urlopen_context())

    def test_proxy_policy(self) -> None:
        self.environment(
            {
                "SSL_CERT_FILE": str(self.cert),
                "https_proxy": "http://127.0.0.1:1",
                "no_proxy": "localhost",
            }
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request()
        raised.exception.close()
        os.environ["no_proxy"] = ""
        urllib.request.install_opener(None)
        with self.assertRaises(urllib.error.URLError):
            self.request()

    def test_public_auth_dispatch_uses_http11_custom_ca_and_rejects_bad_certs(self) -> None:
        from headroom_kit.cli import dispatch

        self.environment({"SSL_CERT_FILE": str(self.cert)})
        self.assertEqual(self.auth_help(dispatch), 0)
        self.assert_custom_ca_http11()
        self.assert_certificate_rejected(dispatch)

    def auth_help(self, dispatch: Callable[[list[str]], int]) -> int:
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = dispatch(["copilot-auth", "--help"])
        self.assertIn("headroom-kit copilot-auth", stdout.getvalue())
        self.assertIn("login", stdout.getvalue())
        return code

    def assert_custom_ca_http11(self) -> None:
        httpx_context = self.ssl_context.build_httpx_verify()
        context = self.ssl_context.build_urlopen_context()
        self.assertEqual(
            set(context.get_ca_certs(binary_form=True)),
            set(httpx_context.get_ca_certs(binary_form=True)),
        )
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request()
        raised.exception.close()
        self.assertEqual(Handler.protocols[-1], "http/1.1")
        with self.assertRaises(urllib.error.URLError):
            self.request(self.url.replace("localhost", "127.0.0.1"))

    def assert_certificate_rejected(self, dispatch: Callable[[list[str]], int]) -> None:
        self.environment({})
        self.assertEqual(self.auth_help(dispatch), 0)
        self.assertIsNone(self.ssl_context.build_urlopen_context())
        with self.assertRaises(urllib.error.URLError):
            self.request()

    def test_upstream_swallowed_errors_are_classified_without_body_logging(self) -> None:
        self.environment(
            {
                "SSL_CERT_FILE": str(self.cert),
                "GITHUB_COPILOT_TOKEN_EXCHANGE_URL": self.url,
            }
        )
        candidate = self.adapter.CopilotTokenCandidate(
            token="gho_fake_test_only",
            source="test",
            confidence="test",
        )
        original = self.adapter._urlopen
        for status, disconnect, truncated, expected in (
            (401, False, False, set()),
            (403, False, False, set()),
            (503, False, False, {"service"}),
            (200, True, False, {"transport"}),
            (200, False, True, {"transport"}),
        ):
            Handler.status, Handler.disconnect, Handler.truncated = status, disconnect, truncated
            with (
                self.assertLogs("headroom.copilot_auth", level="DEBUG") as logs,
                auth_failures(self.adapter) as failures,
            ):
                resolution = self.adapter._subscription_resolution_from_token_exchange(candidate)
            self.assertIsNone(resolution)
            self.assertEqual(failures, expected)
            self.assertNotIn("fake", " ".join(logs.output))
            self.assertIs(self.adapter._urlopen, original)


if __name__ == "__main__":
    unittest.main()

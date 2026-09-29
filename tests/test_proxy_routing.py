"""Test client proxy policy with real curl and local endpoints only."""

import contextlib
import shutil
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_launch import kit


@contextlib.contextmanager
def endpoint(route: str) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(route.encode())

        def log_message(self, format: str, *args: str | int) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


class ProxyRoutingTests(unittest.TestCase):
    def test_wildcard_bypasses_all_proxies_and_ordinary_lists_keep_proxy_routing(self) -> None:
        curl = shutil.which("curl")
        self.assertIsNotNone(curl, "This regression requires real curl.")
        with (
            tempfile.TemporaryDirectory() as home,
            endpoint("direct") as target,
            endpoint("proxy") as forward,
        ):
            cases = (
                ({"NO_PROXY": "other.example.invalid"}, "proxy"),
                ({"NO_PROXY": "*"}, "direct"),
                ({"no_proxy": "*"}, "direct"),
                ({"NO_PROXY": "other.example.invalid", "no_proxy": " * "}, "direct"),
                (
                    {"NO_PROXY": "*,other.example.invalid", "no_proxy": "second.example.invalid"},
                    "direct",
                ),
            )
            for exclusions, expected in cases:
                with self.subTest(exclusions=exclusions):
                    inherited = dict.fromkeys(
                        (
                            "HTTP_PROXY",
                            "HTTPS_PROXY",
                            "ALL_PROXY",
                            "http_proxy",
                            "https_proxy",
                            "all_proxy",
                        ),
                        f"http://127.0.0.1:{forward}",
                    )
                    inherited.update(HOME=home, **exclusions)
                    original = dict(inherited)
                    child = kit.client_environment(inherited)
                    for host, route in (
                        ("target.example.invalid", expected),
                        ("127.0.0.1", "direct"),
                    ):
                        result = subprocess.run(
                            [
                                curl,
                                "--disable",
                                "--silent",
                                "--show-error",
                                "--max-time",
                                "3",
                                "--resolve",
                                f"target.example.invalid:{target}:127.0.0.1",
                                f"http://{host}:{target}/",
                            ],
                            env=child,
                            cwd=home,
                            capture_output=True,
                            text=True,
                            timeout=5,
                            check=True,
                        )
                        self.assertEqual(result.stdout, route)
                    self.assertEqual(inherited, original)


if __name__ == "__main__":
    unittest.main()

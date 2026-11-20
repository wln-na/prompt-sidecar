from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import prompt_sidecar_mcp as mcp


class McpEndpointTests(unittest.TestCase):
    def test_default_endpoint(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(mcp.sidecar_endpoint(), "http://127.0.0.1:8765")

    def test_allowed_loopback_origins(self) -> None:
        for value in (
            "http://localhost:9876", "http://127.0.0.1:9876",
            "http://127.2.3.4:9876", "http://[::1]:9876", "https://localhost:9876/",
        ):
            with self.subTest(value=value), patch.dict(os.environ, {"PROMPT_SIDECAR_URL": value}):
                self.assertEqual(mcp.sidecar_endpoint(), value.rstrip("/"))

    def test_unsafe_or_ambiguous_origins_are_rejected(self) -> None:
        for value in (
            "http://example.test:9876", "http://192.168.1.2:9876", "http://0.0.0.0",
            "http://localhost.example.test", "http://[::]", "http://127.1",
            "http://user:fixture@localhost:9876", "http://user@127.0.0.1", "http://@localhost",
            "file:///tmp/fixture", "ftp://localhost", "//localhost:9876",
            "http://localhost/api", "http://localhost?token=fixture", "http://localhost#fixture",
            "http://localhost:", "http://localhost:bad", "http://localhost:0",
            "http://localhost:65536", "http://[::1", "http://local\nhost", "",
        ):
            with self.subTest(value=value), patch.dict(os.environ, {"PROMPT_SIDECAR_URL": value}):
                with self.assertRaises(ValueError) as caught:
                    mcp.sidecar_endpoint()
                self.assertNotIn("fixture", str(caught.exception))

    def test_invalid_endpoint_fails_before_network_access(self) -> None:
        with patch.dict(os.environ, {"PROMPT_SIDECAR_URL": "http://example.test"}):
            with patch.object(mcp, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    mcp.call_tool("prompt_sidecar_health", {})
                opener.assert_not_called()


class McpEndpointHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[str] = []
        self.redirect = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests.append(self.path)
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", "/must-not-follow")
                    self.end_headers()
                    return
                payload = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.env = patch.dict(os.environ, {"PROMPT_SIDECAR_URL": self.url})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(2)

    def test_all_tools_use_the_configured_port(self) -> None:
        for name, arguments, expected in (
            ("prompt_sidecar_health", {}, "/api/health"),
            ("prompt_sidecar_sessions", {"source": "fixture"}, "/api/sessions?source=fixture"),
            ("prompt_sidecar_context", {"source": "fixture", "session_id": "fake session"},
             "/api/context?source=fixture&session_id=fake+session"),
        ):
            result = mcp.call_tool(name, arguments)
            self.assertEqual(result["path"], expected)
        self.assertEqual(len(self.requests), 3)

    def test_redirects_are_not_followed(self) -> None:
        self.redirect = True
        with self.assertRaises(HTTPError) as caught:
            mcp.call_tool("prompt_sidecar_health", {})
        self.assertEqual(caught.exception.code, 302)
        caught.exception.close()
        self.assertEqual(self.requests, ["/api/health"])

    def test_environment_proxy_is_not_used(self) -> None:
        with patch.dict(os.environ, {
            "http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
            "no_proxy": "", "NO_PROXY": "",
        }):
            self.assertTrue(mcp.call_tool("prompt_sidecar_health", {})["ok"])


if __name__ == "__main__":
    unittest.main()

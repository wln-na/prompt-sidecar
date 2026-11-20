from __future__ import annotations

import http.client
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class HttpAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PromptSidecarHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(2)

    def get(self, path: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            connection.request("GET", path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_ui_assets_are_available(self) -> None:
        for path in ("/", "/app.js", "/styles.css", "/favicon.svg", "/prompt-flow.svg"):
            with self.subTest(path=path):
                self.assertEqual(self.get(path)[0], 200)

    def test_repository_files_are_not_served(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("config/sidecar.config.json", ".env", ".git/config", "backend/server.py",
                         "docs/private.html", "public/private.json", "public/.hidden.js"):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("private fixture", encoding="utf-8")
            with patch.object(server, "APP_ROOT", root):
                for name in ("config/sidecar.config.json", ".env", ".git/config",
                             "backend/server.py",
                             "docs/private.html", "public/private.json", "public/.hidden.js"):
                    with self.subTest(name=name):
                        status, body = self.get("/" + name)
                        self.assertEqual(status, 404)
                        self.assertNotIn(b"private fixture", body)

    def test_traversal_and_symlinks_cannot_expose_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "app"
            frontend = root / "frontend"
            frontend.mkdir(parents=True)
            secret = root / "private.js"
            secret.write_text("private fixture", encoding="utf-8")
            (frontend / "app.js").symlink_to(secret)
            outside = Path(directory).resolve() / "outside.js"
            outside.write_text("outside fixture", encoding="utf-8")
            (frontend / "favicon.svg").symlink_to(outside)
            with patch.object(server, "APP_ROOT", root):
                for path in ("/frontend/../private.js", "/app.js", "/favicon.svg",
                             "/../outside.js", "/public/example.html"):
                    with self.subTest(path=path):
                        self.assertEqual(self.get(path)[0], 404)

    def test_get_rejects_foreign_host_and_origin_before_reading_context(self) -> None:
        with patch.object(server, "list_all_sessions", return_value=[]) as sessions:
            for headers in ({"Host": "attacker.example"},
                            {"Origin": "https://attacker.example"},
                            {"Origin": "http://127.0.0.1:1"}):
                with self.subTest(headers=headers):
                    self.assertEqual(self.get("/api/sessions", headers)[0], 403)
            sessions.assert_not_called()

    def test_same_origin_get_still_reads_sessions(self) -> None:
        with patch.object(server, "list_all_sessions", return_value=[]) as sessions:
            origin = f"http://127.0.0.1:{self.httpd.server_port}"
            self.assertEqual(self.get("/api/sessions", {"Origin": origin})[0], 200)
            sessions.assert_called_once()

    def test_configured_https_panel_origin_gets_bootstrap_and_cors(self) -> None:
        origin = "https://app.promptsidecar.com"
        headers = {
            "Host": f"127.0.0.1:{self.httpd.server_port}",
            "Origin": origin,
        }
        with patch.object(
            server, "panel_allowed_origins", return_value={origin}
        ):
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.httpd.server_port, timeout=5
            )
            try:
                connection.request("GET", "/api/bootstrap", headers=headers)
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), origin)
        self.assertEqual(response.getheader("Vary"), "Origin")
        payload = __import__("json").loads(body)
        self.assertEqual(payload["kind"], "prompt-sidecar-local")
        self.assertEqual(payload["panel_mode"], "hosted")
        self.assertTrue(payload["api_base"].startswith("http://127.0.0.1:"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StartupTests(unittest.TestCase):
    def test_server_runs_from_another_directory_with_reorganized_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            project = temporary / "project"
            for name in ("backend", "frontend", "config"):
                shutil.copytree(
                    ROOT / name, project / name,
                    ignore=shutil.ignore_patterns("__pycache__", "sidecar.config.json"),
                )
            example = project / "config" / "sidecar.config.example.json"
            config = json.loads(example.read_text(encoding="utf-8"))
            for bridge in config["bridges"].values():
                bridge["enabled"] = False
            example.write_text(json.dumps(config), encoding="utf-8")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            with tempfile.TemporaryFile(mode="w+") as log:
                process = subprocess.Popen(
                    [sys.executable, str(project / "backend" / "server.py"), "--port", str(port)],
                    cwd=temporary,
                    env={"PATH": os.environ.get("PATH", ""), "HOME": str(temporary / "home"),
                         "CODEX_HOME": str(temporary / "isolated-codex"),
                         "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                    stdout=log, stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        try:
                            status, body = self.get(port, "/api/health")
                            break
                        except OSError:
                            if process.poll() is not None:
                                break
                            time.sleep(0.05)
                    else:
                        self.fail("server did not start within eight seconds")
                    if process.poll() is not None:
                        log.seek(0)
                        self.fail(f"server exited during startup: {log.read()}")
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)["ok"])
                    for path in ("/", "/app.js", "/styles.css", "/favicon.svg", "/prompt-flow.svg"):
                        with self.subTest(path=path):
                            self.assertEqual(self.get(port, path)[0], 200)
                    for path in ("/backend/server.py", "/config/sidecar.config.json",
                                 "/.git/config", "/frontend/../config/sidecar.config.json"):
                        with self.subTest(path=path):
                            self.assertEqual(self.get(port, path)[0], 404)
                    self.assertEqual(self.get(port, "/api/config")[0], 200)
                    self.assertEqual(json.loads(self.get(port, "/api/sessions")[1])["sessions"], [])
                    self.assertTrue((project / "config" / "sidecar.config.json").is_file())
                    self.assertFalse((temporary / "sidecar.config.json").exists())
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)

    def get(self, port: int, path: str) -> tuple[int, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

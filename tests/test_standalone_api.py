"""Standalone HTTP contracts using fake bridges and an isolated config file."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class StandaloneApiTests(unittest.TestCase):
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

    def setUp(self) -> None:
        self.bridge = Mock()
        self.bridge.config = {}
        self.packet = {
            "available": True,
            "source": "codex",
            "session_id": "session-one",
            "session_title": "Fixture task",
            "cwd": "/fixture",
            "context": "USER: complete the task\nASSISTANT: tests remain",
            "messages": [{"role": "user", "text": "complete the task"}],
        }
        self.bridge.build_context_packet.return_value = self.packet
        self.bridge.capabilities.return_value = {
            "send": True,
            "send_modes": ["queue", "steer"],
            "model_selection": server.fixed_model_selection("fixture-model", "Fixture"),
        }
        self.bridge.send.return_value = {"accepted": True, "queue_id": "fixture-receipt"}
        self.resolve = self.enterContext(patch.object(
            server, "resolve_bridge", return_value=self.bridge,
        ))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config_path = Path(temporary.name) / "sidecar.config.json"
        self.config_path.write_text("{}", encoding="utf-8")
        self.enterContext(patch.object(server, "CONFIG_PATH", self.config_path))

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        try:
            payload = None if body is None else json.dumps(body)
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            content = response.read()
            return response.status, json.loads(content) if content else {}
        finally:
            connection.close()

    def test_bootstrap_describes_the_local_connector(self) -> None:
        status, result = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(result["kind"], "prompt-sidecar-local")
        self.assertEqual(result["panel_mode"], "hosted")
        self.assertTrue(result["api_base"].startswith("http://127.0.0.1:"))
        self.assertIn("sessions", result["capabilities"])

    def test_hosted_panel_preflight_returns_cors_contract(self) -> None:
        origin = "https://app.promptsidecar.com"
        with patch.object(server, "panel_allowed_origins", return_value={origin}):
            status, result = self.request(
                "OPTIONS",
                "/api/context",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "content-type",
                },
            )
        self.assertEqual(status, 204)
        self.assertEqual(result, {})

    def test_context_reads_only_the_selected_bridge_session(self) -> None:
        status, result = self.request("GET", "/api/context?source=codex&session_id=session-one")
        self.assertEqual(status, 200)
        self.assertEqual(result, self.packet)
        self.resolve.assert_called_once_with("codex")
        self.bridge.build_context_packet.assert_called_once_with("session-one")
        self.bridge.send.assert_not_called()

    def test_context_without_source_keeps_route_binding(self) -> None:
        with patch.object(server, "build_context_packet", return_value=self.packet) as context:
            status, result = self.request("GET", "/api/context?session_id=session-one")
        self.assertEqual(status, 200)
        self.assertEqual(result["session_id"], "session-one")
        context.assert_called_once_with(session_id="session-one")
        self.resolve.assert_not_called()

    def test_send_retains_queue_and_steer_with_effective_model(self) -> None:
        for mode in ("queue", "steer"):
            with self.subTest(mode=mode):
                self.bridge.send.reset_mock()
                status, result = self.request("POST", "/api/send", {
                    "source": "codex", "session_id": "session-one",
                    "message": "Continue the task", "mode": mode,
                })
                self.assertEqual(status, 200)
                self.assertTrue(result["queued"])
                self.assertEqual(result["queue_id"], "fixture-receipt")
                self.assertEqual(result["effective_model"], "fixture-model")
                self.bridge.send.assert_called_once_with({
                    "source": "codex", "session_id": "session-one",
                    "message": "Continue the task", "mode": mode, "model_id": "",
                })

    def test_invalid_send_never_resolves_or_sends(self) -> None:
        for body in (
            {"session_id": "session-one", "message": "text", "mode": "unsupported"},
            {"message": "text"},
            {"session_id": "session-one"},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/send", body)[0], 400)
        self.resolve.assert_not_called()
        self.bridge.send.assert_not_called()

    def test_next_step_uses_session_evidence_and_never_sends(self) -> None:
        analysis = {
            "status": "in_progress", "task_goal": "complete the task",
            "recommended_next_step": {"title": "Run remaining tests"},
        }
        with patch.object(server, "call_model", return_value=json.dumps(analysis)) as model:
            status, result = self.request("POST", "/api/next-step", {
                "source": "codex", "session_id": "session-one",
            })
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "in_progress")
        self.assertIn("Run remaining tests", result["final_prompt"])
        self.assertEqual(result["cwd"], "/fixture")
        self.assertIn(self.packet["context"], model.call_args.args[0])
        self.bridge.send.assert_not_called()

    def test_next_step_without_context_does_not_call_model(self) -> None:
        self.bridge.build_context_packet.return_value = {"context": "", "messages": []}
        with patch.object(server, "call_model") as model:
            status, result = self.request("POST", "/api/next-step", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "insufficient_context")
        self.assertEqual(result["final_prompt"], "")
        model.assert_not_called()
        self.bridge.send.assert_not_called()

    def test_next_step_keeps_explicit_context_override(self) -> None:
        analysis = {"status": "ready", "recommended_next_step": {"title": "Verify"}}
        with patch.object(server, "call_model", return_value=json.dumps(analysis)) as model:
            status, result = self.request("POST", "/api/next-step", {
                "context": "explicit task evidence",
            })
        self.assertEqual(status, 200)
        self.assertIn("explicit task evidence", model.call_args.args[0])
        self.assertEqual(result["source"], "codex")

    def test_config_read_and_save_use_only_bridge_settings(self) -> None:
        status, result = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"ok", "bridges"})
        status, result = self.request("POST", "/api/config", {
            "bridges": {"codex": {"enabled": False}},
        })
        self.assertEqual(status, 200)
        self.assertFalse(result["bridges"]["codex"]["enabled"])
        self.assertEqual(set(result), {"ok", "bridges"})
        self.assertFalse(json.loads(self.config_path.read_text())["bridges"]["codex"]["enabled"])
        self.bridge.send.assert_not_called()

    def test_unknown_endpoints_are_not_found(self) -> None:
        for method, path in (
            ("GET", "/api/unknown?request_id=fixture"),
            ("POST", "/api/unknown"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path, {})[0], 404)
        self.resolve.assert_not_called()
        self.bridge.send.assert_not_called()

    def test_post_rejects_foreign_or_malformed_origin_before_sending(self) -> None:
        for origin in (
            "https://attacker.example", "http://127.0.0.1:1", "null",
            "http://user@127.0.0.1", "http://127.0.0.1:invalid",
        ):
            with self.subTest(origin=origin):
                status, result = self.request("POST", "/api/send", {}, {"Origin": origin})
                self.assertEqual(status, 403)
                self.assertEqual(result["error_code"], "local_request_required")
        self.resolve.assert_not_called()
        self.bridge.send.assert_not_called()

    def test_post_rejects_non_object_body(self) -> None:
        for body in ([], "text", 1):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/send", body)[0], 400)
        self.resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()

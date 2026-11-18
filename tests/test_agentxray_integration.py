"""Integration contract tests; every delivery uses a fake bridge."""
from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


def handoff() -> dict:
    return {
        "handoff_id": "intervention-one", "attention_id": "attention-one",
        "target": {"platform": "codex", "session_id": "session-one", "agent": None,
                   "cwd": None, "title": "Fix inbox", "snapshot": {"message_count": 3}},
        "target_fingerprint": "fingerprint", "snapshot_version": "snapshot",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        "source": "agentxray", "user_request": "Original requirement", "reason": "Blocked",
        "objective": "Fix inbox", "current_state": "Waiting for user", "completed": ["read"],
        "pending": ["implement"], "blockers": ["decision"], "recommended_action": "Clarify",
        "confidence": "medium", "acceptance_criteria": [],
        "session_refs": [{"platform": "codex", "session_id": "session-one", "agent": None,
                          "cwd": None, "title": "Fix inbox", "evidence": [{"message_id": "m1"}]}],
    }


def delivery() -> dict:
    h = handoff()
    return {"handoff_id": h["handoff_id"], "attention_id": h["attention_id"],
            "platform": "codex", "agent": None, "session_id": "session-one", "cwd": None,
            "prompt": "Final edited prompt", "idempotency_key": "random-once",
            "target_fingerprint": "fingerprint", "snapshot_version": "snapshot"}


class IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        server._IMPORTED_HANDOFFS.clear()
        server._DELIVERIES.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "integration.token"
        self.token.write_text("a" * 64)
        self.token.chmod(0o600)
        env = patch.dict(os.environ, {"AXR_SIDECAR_TOKEN_FILE": str(self.token)})
        env.start()
        self.addCleanup(env.stop)
        config = patch("server.agentxray_config", return_value={"enabled": True,
                       "base_url": "http://127.0.0.1:4921", "timeout_ms": 30000})
        config.start()
        self.addCleanup(config.stop)
        self.bridge = Mock()
        self.bridge.capabilities.return_value = {"send": True, "send_modes": ["queue"]}
        self.bridge.build_context_packet.return_value = {
            "available": True, "session_id": "session-one", "cwd": "/project"}
        self.bridge.send.return_value = {"status": "queued", "queue_id": "q1"}
        resolver = patch("server.resolve_bridge", return_value=self.bridge)
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        model = patch("server.model_plan_for_send", return_value=(None, "current-thread"))
        model.start()
        self.addCleanup(model.stop)

    def import_record(self, h: dict | None = None, origin: str = "http://127.0.0.1:4921") -> dict:
        h = h or handoff()
        with patch("server.request_json_with_retry", return_value={"intervention": h}) as request:
            result = server.load_agentxray_intervention(h["handoff_id"], origin)
        self.assertEqual(request.call_args.kwargs["retry_count"], 0)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        return result

    def test_loader_unwraps_preserves_full_context_and_nullable_target(self) -> None:
        h = handoff()
        result = self.import_record(h)
        for field in ("user_request", "reason", "objective", "current_state", "completed",
                      "pending", "blockers", "recommended_action", "confidence", "session_refs"):
            self.assertEqual(result[field], h[field])
        self.assertIsNone(result["target"]["agent"])
        self.assertIsNone(result["target"]["cwd"])

    def test_only_local_origin_and_no_callback_path_or_credentials(self) -> None:
        for origin in ("http://example.com", "http://127.0.0.1.evil.test", "file:///tmp/axr",
                       "http://localhost/api/send", "http://user@localhost", "http://localhost?q=1",
                       "http://localhost/#secret", "http://localhost:invalid"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                server.agentxray_base_url(origin)
        self.assertEqual(server.agentxray_base_url("http://[::1]:4921/"), "http://[::1]:4921")

    def test_send_final_text_exact_target_same_import_origin_and_timeout(self) -> None:
        h = self.import_record()
        receipt = {"ok": True, "accepted": True, "receipt_id": "r1", "status": "awaiting_agent"}
        with patch("server.request_json_with_retry", return_value=receipt) as request:
            result = server.send_agentxray_handoff(h, "Final edited text", "stable-key")
        self.assertEqual(request.call_args.args[0],
                         "http://127.0.0.1:4921/api/attention/interventions/intervention-one/send")
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload, {"message": "Final edited text", "confirmed": True,
                         "idempotency_key": "stable-key", "target": h["target"],
                         "target_fingerprint": "fingerprint", "snapshot_version": "snapshot"})
        self.assertGreaterEqual(request.call_args.kwargs["timeout_seconds"], 185)
        self.assertEqual(result, receipt)
        self.bridge.send.assert_not_called()

    def test_import_identity_cannot_move_to_other_origin_or_modified_target(self) -> None:
        h = self.import_record()
        with self.assertRaises(ValueError):
            self.import_record(origin="http://127.0.0.1:4922")
        modified = copy.deepcopy(h)
        modified["target"]["session_id"] = "different-session"
        with self.assertRaises(server.AgentXRayTargetMismatchError):
            server.integration_request_context({"handoff": modified})

    def test_send_requires_import_and_explicit_idempotency_key(self) -> None:
        with self.assertRaises(server.AgentXRayTargetMismatchError):
            server.send_agentxray_handoff(handoff(), "prompt", "key")
        h = self.import_record()
        with self.assertRaises(ValueError):
            server.send_agentxray_handoff(h, "prompt", "")

    def test_proxy_does_not_invent_success_on_empty_ack_or_timeout(self) -> None:
        h = self.import_record()
        for value in ({}, {"ok": True}, {"ok": True, "accepted": True}):
            with patch("server.request_json_with_retry", return_value=value):
                with self.assertRaises(server.IntegrationError) as caught:
                    server.send_agentxray_handoff(h, "prompt", "key")
                self.assertEqual(caught.exception.code, "delivery_unknown")
        with patch("server.request_json_with_retry", side_effect=server.BridgeUnavailableError("timeout")):
            with self.assertRaises(server.IntegrationError) as caught:
                server.send_agentxray_handoff(h, "prompt", "key")
            self.assertEqual(caught.exception.code, "delivery_unknown")

    def test_axr_error_machine_codes_are_preserved(self) -> None:
        h = self.import_record()
        for code, status in (("target_not_found", 404), ("session_closed", 410),
                             ("stale_context", 409), ("delivery_unknown", 502)):
            error = HTTPError("http://localhost", status, "error", {},
                              io.BytesIO(json.dumps({"error_code": code, "error": code}).encode()))
            with patch("server.request_json_with_retry", side_effect=error):
                with self.assertRaises(server.IntegrationError) as caught:
                    server.send_agentxray_handoff(h, "prompt", "key")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.status, status)

    def test_deliver_calls_exact_bridge_once_with_stable_receipt(self) -> None:
        body = delivery()
        result = server.deliver_agentxray(body, "a" * 64)
        replay = server.deliver_agentxray(body, "a" * 64)
        self.assertEqual(result, replay)
        self.assertEqual(result["receipt_id"], "q1")
        self.assertEqual(result["handoff_id"], body["handoff_id"])
        self.assertEqual(result["platform"], "codex")
        self.assertEqual(result["session_id"], "session-one")
        self.assertEqual(result["status"], "queued")
        self.bridge.send.assert_called_once_with({"source": "codex", "session_id": "session-one",
                    "message": "Final edited prompt", "mode": "queue", "model_id": ""})
        with self.assertRaises(server.IntegrationError) as caught:
            server.deliver_agentxray({**body, "prompt": "Changed"}, "a" * 64)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_deliver_missing_invalid_or_unprotected_token_never_sends(self) -> None:
        for key in ("", "wrong"):
            with self.assertRaises(server.IntegrationError):
                server.deliver_agentxray(delivery(), key)
        self.token.chmod(0o644)
        with self.assertRaises(server.IntegrationError):
            server.deliver_agentxray(delivery(), "a" * 64)
        self.token.unlink()
        with self.assertRaises(server.IntegrationError):
            server.deliver_agentxray(delivery(), "a" * 64)
        self.assertFalse(self.token.exists())
        self.bridge.send.assert_not_called()

    def test_deliver_explicit_aliases_without_fallback(self) -> None:
        for platform, source in (("claude-code", "claude"), ("workbuddy", "workbuddy_local")):
            server._DELIVERIES.clear()
            server.deliver_agentxray({**delivery(), "platform": platform}, "a" * 64)
            self.resolve.assert_called_with(source)
            self.assertEqual(self.bridge.send.call_args.args[0]["source"], source)
        server._DELIVERIES.clear()
        self.resolve.side_effect = ValueError("unsupported")
        with self.assertRaises(server.IntegrationError) as caught:
            server.deliver_agentxray({**delivery(), "platform": "nonexistent"}, "a" * 64)
        self.assertEqual(caught.exception.code, "transport_not_supported")

    def test_preflight_closed_missing_cwd_mismatch_and_unsupported_never_send(self) -> None:
        cases = [({"session_id": "other"}, "target_not_found"),
                 ({"session_id": "session-one", "archived": True}, "session_closed"),
                 ({"session_id": "session-one", "cwd": "/other"}, "target_mismatch")]
        for packet, code in cases:
            with self.subTest(code=code):
                self.bridge.build_context_packet.return_value = packet
                with self.assertRaises(server.IntegrationError) as caught:
                    server.deliver_agentxray({**delivery(), "cwd": "/project"}, "a" * 64)
                self.assertEqual(caught.exception.code, code)
        self.bridge.capabilities.return_value = {"send": False, "send_modes": []}
        with self.assertRaises(server.IntegrationError):
            server.deliver_agentxray(delivery(), "a" * 64)
        self.bridge.send.assert_not_called()

    def test_bridge_exception_is_unknown_and_replay_never_retries(self) -> None:
        self.bridge.send.side_effect = RuntimeError("may already have queued")
        for _ in range(2):
            with self.assertRaises(server.IntegrationError) as caught:
                server.deliver_agentxray(delivery(), "a" * 64)
            self.assertEqual(caught.exception.code, "delivery_unknown")
        self.bridge.send.assert_called_once()

    def test_concurrent_duplicate_is_locked_before_bridge_send(self) -> None:
        entered, release = threading.Event(), threading.Event()
        def slow_send(request: dict) -> dict:
            entered.set()
            release.wait(2)
            return {"accepted": True}
        self.bridge.send.side_effect = slow_send
        errors = []
        def send() -> None:
            try:
                server.deliver_agentxray(delivery(), "a" * 64)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(server.IntegrationError) as caught:
                server.deliver_agentxray(delivery(), "a" * 64)
            self.assertEqual(caught.exception.code, "delivery_in_progress")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(errors)
        self.bridge.send.assert_called_once()

    def test_packet_legacy_and_object_refs_match_platform_not_only_id(self) -> None:
        packet = server.validate_handoff_packet({"source": "agentxray", "session_refs": [
            {"platform": "claude-code", "session_id": "same", "evidence": [{"id": "e1"}]}]})
        self.assertTrue(server.continuation_conflicts(packet, "codex", "same"))
        self.assertFalse(server.continuation_conflicts(packet, "claude", "same"))
        self.assertEqual(packet["session_refs"][0]["evidence"], [{"id": "e1"}])
        packet["session_refs"] = ["same"]
        self.assertFalse(server.continuation_conflicts(packet, "codex", "same"))

    def test_config_save_preserves_agentxray_settings(self) -> None:
        config = {**copy.deepcopy(server.DEFAULT_SIDECAR_CONFIG),
                  "agentxray": {"enabled": True, "base_url": "http://localhost:4921",
                                "timeout_ms": 200000}}
        with patch("server.load_sidecar_config", return_value=config), \
                patch("server.CONFIG_PATH", Path(self.temp.name) / "config.json"), \
                patch("server.BRIDGE_REGISTRY.create"), patch("server.validate_model_selection"):
            saved = server.save_sidecar_config({"codex": {"enabled": True}})
        self.assertEqual(saved["agentxray"], config["agentxray"])

    def test_http_routes_enforce_origin_confirmation_and_authenticated_delivery(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PromptSidecarHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        def post(path: str, body: dict, headers: dict | None = None) -> dict:
            request = Request(base + path, data=json.dumps(body).encode(), method="POST",
                              headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urlopen(request) as response:
                    return {"status": response.status, **json.load(response)}
            except HTTPError as exc:
                return {"status": exc.code, **json.load(exc)}
        try:
            self.assertEqual(post("/api/agentxray/deliver", delivery())["status"], 401)
            self.assertEqual(post("/api/agentxray/deliver", delivery(), {
                "X-AgentXRay-Integration-Key": "a" * 64,
                "Origin": "http://evil.test"})["status"], 403)
            h = self.import_record()
            result = post("/api/send", {"integration_mode": "agentxray", "handoff": h,
                          "message": "final", "mode": "handoff", "idempotency_key": "key"})
            self.assertEqual(result["status"], 400)
            result = post("/api/agentxray/deliver", delivery(), {
                          "X-AgentXRay-Integration-Key": "a" * 64})
            self.assertTrue(result["accepted"])
            self.assertEqual(result["receipt_id"], "q1")
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_frontend_integration_behaviour(self) -> None:
        result = subprocess.run(["node", str(Path(__file__).with_name("test_integration.js"))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

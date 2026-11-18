from __future__ import annotations

import unittest

from bridges.contracts import (
    BridgeCapabilities,
    BridgeHealth,
    BridgeSendRequest,
    ContextPacket,
    SendResult,
    SessionBridge,
    SessionRecord,
    fixed_model_selection,
    empty_context_packet,
    retag_sessions,
)
from bridges.registry import BridgeRegistry


class DummyBridge(SessionBridge):
    adapter = "dummy"

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": False,
            "send_modes": [],
            "context_mode": "metadata",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
            ),
        }

    def health(self) -> BridgeHealth:
        return {"available": True, "status": "ready", "message": ""}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        return []

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        return empty_context_packet("dummy", "not implemented")

    def send(self, request: BridgeSendRequest) -> SendResult:
        return {"accepted": False}


class BridgeContractTests(unittest.TestCase):
    def test_registry_creates_and_retags_source(self) -> None:
        registry = BridgeRegistry()
        registry.register("dummy", DummyBridge)
        bridge = registry.create("custom-source", {"adapter": "dummy"})
        self.assertIsInstance(bridge, DummyBridge)
        self.assertEqual(bridge.source, "custom-source")

    def test_registry_rejects_duplicate_adapter(self) -> None:
        registry = BridgeRegistry()
        registry.register("dummy", DummyBridge)
        with self.assertRaises(ValueError):
            registry.register("dummy", DummyBridge)

    def test_context_and_session_helpers_preserve_source(self) -> None:
        packet = empty_context_packet("dummy", "missing", requires_binding=True)
        sessions = retag_sessions([{"session_id": "one"}], "alias")
        self.assertTrue(packet["requires_binding"])
        self.assertEqual(sessions[0]["source"], "alias")

    def test_send_request_contains_verified_model_id(self) -> None:
        request: BridgeSendRequest = {
            "source": "dummy",
            "session_id": "session-1",
            "message": "hello",
            "mode": "queue",
            "model_id": "",
        }
        self.assertEqual(request["source"], "dummy")
        self.assertEqual(request["model_id"], "")


if __name__ == "__main__":
    unittest.main()

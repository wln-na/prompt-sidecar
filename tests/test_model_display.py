from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class ModelDisplayTests(unittest.TestCase):
    def codex_packet(self, records: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records))
            return server.build_context_packet(path=path)

    def test_codex_uses_latest_settings_and_reports_evidence(self) -> None:
        packet = self.codex_packet([
            {"type": "turn_context", "payload": {"model": "model-old"}},
            {
                "type": "event_msg",
                "timestamp": "2026-11-18T00:01:00Z",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {"model": "model-current"},
                },
            },
        ])
        self.assertEqual(packet["model_id"], "model-current")
        self.assertEqual(packet["model_source"], "thread_settings_applied")
        self.assertEqual(packet["model_timestamp"], "2026-11-18T00:01:00Z")

    def test_codex_does_not_infer_model_from_instructions_or_tools(self) -> None:
        packet = self.codex_packet([
            {
                "type": "session_meta",
                "payload": {"base_instructions": {"provenance": {"model": "stale"}}},
            },
            {
                "type": "response_item",
                "payload": {"type": "tool_result", "result": {"model": "tool-model"}},
            },
        ])
        self.assertEqual(packet["model_id"], "")

    def test_codex_new_turn_without_model_clears_old_observation(self) -> None:
        packet = self.codex_packet([
            {"type": "turn_context", "payload": {"model": "model-old"}},
            {"type": "turn_context", "payload": {}},
        ])
        self.assertEqual(packet["model_id"], "")

    def test_codex_latest_turn_overrides_previous_model_settings(self) -> None:
        packet = self.codex_packet([
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {"model": "previous-model"},
                },
            },
            {
                "type": "turn_context", "timestamp": "2026-11-18T00:05:00Z",
                "payload": {"model": "latest-model"},
            },
        ])
        self.assertEqual(packet["model_id"], "latest-model")
        self.assertEqual(packet["model_source"], "turn_context")

    def test_model_policy_and_invalid_values_remain_unknown(self) -> None:
        for value in (
            None, 1, {}, "", "session-default", "current-thread", "unknown", "<synthetic>",
        ):
            with self.subTest(value=value):
                observation = server.model_observation(value, "turn_context", "timestamp")
                self.assertEqual(observation, {
                    "model_id": "", "model_source": "", "model_timestamp": "",
                })

    def test_claude_uses_assistant_message_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "session.jsonl"
            path.write_text(json.dumps({
                "type": "assistant",
                "timestamp": "2026-11-18T00:02:00Z",
                "message": {"role": "assistant", "model": "claude-observed", "content": []},
            }))
            packet = server.build_claude_context_packet("session", projects_dir=root)
        self.assertEqual(packet["model_id"], "claude-observed")
        self.assertEqual(packet["model_source"], "assistant.message.model")

    def test_claude_ignores_sidechain_model_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "session.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "type": "assistant", "timestamp": "2026-11-18T00:02:00Z",
                    "message": {"role": "assistant", "model": "main-model", "content": []},
                }),
                json.dumps({
                    "type": "assistant", "isSidechain": True,
                    "message": {"role": "assistant", "model": "sidechain-model", "content": []},
                }),
            ]))
            packet = server.build_claude_context_packet("session", projects_dir=root)
        self.assertEqual(packet["model_id"], "main-model")

    def test_workbuddy_uses_provider_model_without_using_quota_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "session.jsonl"
            path.write_text(json.dumps({
                "type": "message",
                "role": "assistant",
                "content": "done",
                "providerData": {
                    "model": "hy4-flash-free",
                    "rawUsage": {"quota": {"model": "hy4-flash"}},
                },
            }))
            packet = server.build_workbuddy_local_context_packet({
                "home": str(root), "projects_dir": str(root),
            }, "session")
        self.assertEqual(packet["model_id"], "hy4-flash-free")
        self.assertEqual(packet["model_source"], "assistant.providerData.model")

    def test_frontend_model_display_behaviour(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node.js is required for frontend model display tests")
        path = Path(__file__).with_name("test_model_display.js")
        result = subprocess.run([node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verified_session_model_reads_context_evidence_only(self) -> None:
        class Bridge:
            def build_context_packet(self, session_id: str) -> dict[str, str]:
                return {
                    "model_id": "observed-model",
                    "model_source": "turn_context",
                    "model_timestamp": "2026-11-18T00:06:00Z",
                }

        self.assertEqual(
            server.verified_session_model(Bridge(), "session"),
            {
                "model_id": "observed-model",
                "model_source": "turn_context",
                "model_timestamp": "2026-11-18T00:06:00Z",
            },
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server


class PolishApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.PromptSidecarHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(2)

    def post(self, body: dict, headers: dict | None = None) -> dict:
        request = Request(
            f"{self.base_url}/api/polish",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urlopen(request) as response:
                return {"status": response.status, **json.load(response)}
        except HTTPError as exc:
            return {"status": exc.code, **json.load(exc)}

    def test_prompt_config_endpoint_persists_overrides_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sidecar.config.json"
            config_path.write_text("{}", encoding="utf-8")
            with patch.object(server, "CONFIG_PATH", config_path):
                with urlopen(Request(f"{self.base_url}/api/prompt-config")) as response:
                    initial = json.load(response)
                changed = {
                    **initial["effective"],
                    "modes": {
                        **initial["effective"]["modes"],
                        "quick": "接口保存后的模式规则",
                    },
                }
                request = Request(
                    f"{self.base_url}/api/prompt-config",
                    data=json.dumps(changed, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Content-Type": "application/json"},
                )
                with urlopen(request) as response:
                    saved = json.load(response)

            self.assertEqual(saved["effective"]["modes"]["quick"], "接口保存后的模式规则")
            stored = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                stored["prompt_settings"]["overrides"]["modes"],
                {"quick": "接口保存后的模式规则"},
            )

    def test_standalone_explicit_context_is_headless_and_correlated(self) -> None:
        with patch("server.call_model", return_value="polished") as call_model:
            with patch("server.resolve_bridge", side_effect=AssertionError("bridge lookup")):
                result = self.post(
                    {
                        "draft": "原始提示词",
                        "supplied_context": "supplied context",
                        "source": "codex",
                        "session_id": "session-one",
                        "request_id": "request-123",
                        "correlation_id": "correlation-456",
                    }
                )

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["request_id"], "request-123")
        self.assertEqual(result["correlation_id"], "correlation-456")
        self.assertEqual(result["mode"], "quick")
        self.assertTrue(result["context_used"])
        self.assertEqual(result["context_chars"], len("supplied context"))
        self.assertTrue(result["used_context"])
        self.assertEqual(result["polished"], "polished")
        self.assertIn("supplied context", call_model.call_args.args[0])

    def test_agent_mode_sends_codex_execution_contract_to_polisher(self) -> None:
        with patch("server.call_model", return_value="codex task prompt") as call_model:
            result = self.post(
                {
                    "draft": "完成当前事项",
                    "mode": "agent",
                    "supplied_context": "当前项目上下文",
                }
            )

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["polished"], "codex task prompt")
        prompt = call_model.call_args.args[0]
        for phrase in ("Planner", "Agent Team", "Subagent", "多会话通信", "完成条件"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, prompt)
        self.assertNotIn("如果支持多 Agent", prompt)
        self.assertNotIn("否则单 Agent", prompt)

    def test_standalone_without_context_can_return_stable_ids_from_headers(self) -> None:
        with patch("server.call_model", return_value="polished"):
            with patch("server.build_context_packet", return_value={"context": ""}):
                result = self.post(
                    {"draft": "原始提示词", "use_context": True},
                    {"X-Request-ID": "header-request", "X-Correlation-ID": "header-correlation"},
                )

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["request_id"], "header-request")
        self.assertEqual(result["correlation_id"], "header-correlation")
        self.assertEqual(result["mode"], "quick")
        self.assertFalse(result["context_used"])
        self.assertEqual(result["context_chars"], 0)

    def test_use_context_false_does_not_count_supplied_context(self) -> None:
        with patch("server.call_model", return_value="polished") as call_model:
            result = self.post(
                {
                    "draft": "原始提示词",
                    "context": "ignored context",
                    "use_context": False,
                    "request_id": "request-no-context",
                }
            )

        self.assertEqual(result["status"], 200)
        self.assertFalse(result["context_used"])
        self.assertEqual(result["context_chars"], 0)
        self.assertEqual(result["request_id"], "request-no-context")
        self.assertEqual(result["correlation_id"], "request-no-context")
        self.assertNotIn("ignored context", call_model.call_args.args[0])

    def test_invalid_polish_request_preserves_correlation_fields(self) -> None:
        result = self.post(
            {
                "request_id": "invalid-request",
                "correlation_id": "invalid-correlation",
            }
        )

        self.assertEqual(result["status"], 400)
        self.assertEqual(result["request_id"], "invalid-request")
        self.assertEqual(result["correlation_id"], "invalid-correlation")
        self.assertEqual(
            set(result), {"status", "ok", "error", "request_id", "correlation_id"},
        )



if __name__ == "__main__":
    unittest.main()

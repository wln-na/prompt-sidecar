from __future__ import annotations

import json
import io
import secrets
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from bridges import BridgeSendRequest


def bridge_request(
    source: str,
    session_id: str,
    message: str,
    mode: str = "queue",
    model_id: str = "",
) -> BridgeSendRequest:
    return {
        "source": source,
        "session_id": session_id,
        "message": message,
        "mode": mode,
        "model_id": model_id,
    }


class PromptSidecarTests(unittest.TestCase):
    def test_transcript_extraction_skips_injected_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout-test.jsonl"
            records = [
                {
                    "type": "session_meta",
                    "payload": {"session_id": "test-session", "cwd": "/tmp/project"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "<skill>internal</skill>\n请润色这句话"}],
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "可以，先说明目标。"}],
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
                encoding="utf-8",
            )
            messages, meta = server.read_transcript(path)
            self.assertEqual(meta["session_id"], "test-session")
            self.assertEqual(messages[0]["text"], "请润色这句话")
            self.assertEqual(messages[1]["role"], "assistant")

    def test_transcript_extraction_keeps_tool_and_validation_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout-evidence.jsonl"
            records = [
                {
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "pytest"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "output": "3 passed, 1 failed",
                    },
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record) for record in records),
                encoding="utf-8",
            )
            messages, _ = server.read_transcript(path)
        self.assertEqual(messages[0]["role"], "evidence")
        self.assertIn("Tool call: pytest", messages[0]["text"])
        self.assertIn("3 passed, 1 failed", messages[1]["text"])

    def test_build_request_prompt_contains_mode_and_context(self) -> None:
        prompt = server.build_request_prompt("original draft", "agent", "USER: context")
        self.assertIn("original draft", prompt)
        self.assertIn("USER: context", prompt)
        self.assertIn("coding-agent task", prompt)
        self.assertIn("Never claim the sidecar can replace the Composer", prompt)
        self.assertIn("INTERNAL_OUTPUT_CONTRACT", prompt)
        self.assertIn("style: concise", prompt)
        self.assertIn("structure: 结论 > 变更 > 验证 > 限制 > 下一步", prompt)
        self.assertIn("Do not expose internal labels", prompt)

    def test_request_path_without_query(self) -> None:
        self.assertEqual(
            server.request_path_without_query("/api/context?session_id=secret"),
            "/api/context",
        )
        self.assertEqual(server.request_path_without_query("?session_id=secret"), "/")

    def test_parse_response_text(self) -> None:
        data = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "polished prompt"}],
                }
            ]
        }
        self.assertEqual(server.parse_response_text(data), "polished prompt")

    def test_context_packet_with_temp_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "rollout-test.jsonl"
            record = {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "remember this"}],
                },
            }
            path.write_text(json.dumps(record), encoding="utf-8")
            packet = server.build_context_packet(path=path)
            self.assertTrue(packet["available"])
            self.assertIn("remember this", packet["context"])

    def _make_thread_db(self, root: Path, session_id: str, rollout_path: Path) -> Path:
        import sqlite3

        db_path = root / "state_5.sqlite"
        connection = sqlite3.connect(db_path)
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, name TEXT, title TEXT, cwd TEXT, rollout_path TEXT, "
            "updated_at INTEGER, recency_at INTEGER, archived INTEGER)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, "Displayed thread", "First user message", "/tmp/project", str(rollout_path), 100, 100, 0),
        )
        connection.commit()
        connection.close()
        return db_path

    def test_explicit_session_binding_uses_thread_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "rollout-test.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "session-bound context"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            db_path = self._make_thread_db(root, "session-a", transcript)
            packet = server.build_context_packet(
                session_id="session-a",
                db_path=db_path,
                global_state_path=root / "missing-global-state.json",
            )
            self.assertTrue(packet["available"])
            self.assertEqual(packet["session_id"], "session-a")
            self.assertEqual(packet["binding_method"], "url-explicit")
            self.assertIn("session-bound context", packet["context"])

    def test_right_panel_route_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transcript = root / "rollout-test.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "route-bound context"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            db_path = self._make_thread_db(root, "session-route", transcript)
            global_state = root / "global-state.json"
            global_state.write_text(
                json.dumps(
                    {
                        "electron-persisted-atom-state": {
                            "thread-tab-routes-v1:session-route": {
                                "routes": [
                                    {
                                        "kind": "browser",
                                        "tabId": "browser-use:test",
                                        "params": {"restoreUrl": "http://127.0.0.1:8765/"},
                                    }
                                ],
                                "topology": {
                                    "right": {
                                        "open": True,
                                        "tabIds": ["browser-use:test"],
                                    }
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            bound_id, matches = server.resolve_route_binding(global_state)
            self.assertEqual(bound_id, "session-route")
            self.assertEqual(matches, ["session-route"])
            packet = server.build_context_packet(
                db_path=db_path,
                global_state_path=global_state,
            )
            self.assertEqual(packet["binding_method"], "right-panel-route")
            self.assertEqual(packet["session_id"], "session-route")
            self.assertIn("route-bound context", packet["context"])

    def test_ambiguous_route_binding_requires_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_state = root / "global-state.json"
            global_state.write_text(
                json.dumps(
                    {
                        "electron-persisted-atom-state": {
                            "thread-tab-routes-v1:a": {
                                "routes": [
                                    {
                                        "kind": "browser",
                                        "tabId": "tab-a",
                                        "params": {"restoreUrl": "http://127.0.0.1:8765/"},
                                    }
                                ],
                                "topology": {"right": {"open": True, "tabIds": ["tab-a"]}},
                            },
                            "thread-tab-routes-v1:b": {
                                "routes": [
                                    {
                                        "kind": "browser",
                                        "tabId": "tab-b",
                                        "params": {"restoreUrl": "http://127.0.0.1:8765/"},
                                    }
                                ],
                                "topology": {"right": {"open": True, "tabIds": ["tab-b"]}},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            bound_id, matches = server.resolve_route_binding(global_state)
            self.assertIsNone(bound_id)
            self.assertEqual(matches, ["a", "b"])


    def test_frontend_auto_copy_is_opt_in(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        auto_copy_input = '<input id="auto-copy" type="checkbox">'
        self.assertIn(auto_copy_input, index_html)
        self.assertNotIn('<input id="auto-copy" type="checkbox" checked>', index_html)
        self.assertIn('const autoCopy = $("#auto-copy").checked;', app_js)
        self.assertIn('const copied = autoCopy ? await copyResult({ silent: true }) : false;', app_js)
        self.assertIn("navigator.clipboard.writeText", app_js)
        self.assertIn('copyButton.addEventListener("click"', app_js)



    def test_parse_queue_output(self) -> None:
        output = "Queued message queue-test-id for thread test."
        self.assertEqual(server.parse_queue_output(output), "queue-test-id")

    def test_send_queued_message_uses_explicit_session(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="Queued message queue-1 for thread session-a.\n",
            stderr="",
        )
        with patch("server.CODEX_BIN", "codex-test"):
            with patch("server.query_thread", return_value={"session_id": "session-a"}):
                with patch("server.subprocess.run", return_value=completed) as run:
                    result = server.send_queued_message("session-a", "polished result")
        command = run.call_args.args[0]
        self.assertEqual(command[:5], ["codex-test", "queue", "--thread", "session-a", "--message"])
        self.assertEqual(command[5], "polished result")
        self.assertEqual(result["queue_id"], "queue-1")

    def test_frontend_send_contract(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetchJson("/api/send"', app_js)
        self.assertIn('sendButton.addEventListener("click", sendToCurrentSession)', app_js)
        self.assertIn('copyButton.addEventListener("click"', app_js)

    def test_frontend_task_continuation_contract(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="continue-button"', index_html)
        self.assertIn('id="handoff-packet"', index_html)
        self.assertIn('id="continuation-panel"', index_html)
        self.assertIn('fetchJson("/api/next-step"', app_js)
        self.assertIn("handoff_packet: handoffPacket.value.trim() || null", app_js)
        self.assertIn("function renderContinuation(data)", app_js)
        self.assertIn("recommendation-tab", app_js)
        self.assertIn('continueButton.addEventListener("click", continueTask)', app_js)

    def test_frontend_empty_state_explains_prompt_flow(self) -> None:
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        project_root = Path(__file__).resolve().parents[1]
        self.assertIn('class="prompt-flow-illustration"', index_html)
        self.assertIn('src="/prompt-flow.svg"', index_html)
        styles_css = (project_root / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".prompt-flow-illustration img", styles_css)
        self.assertIn("max-width:100%", styles_css)
        self.assertIn("把草稿变成可发送的提示词", index_html)
        self.assertIn("输入左侧草稿，Sidecar 会结合当前会话上下文进行理解和润色。", index_html)
        self.assertTrue((project_root / "prompt-flow.svg").is_file())

    def test_frontend_result_panels_can_switch(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        index_html = (project_root / "index.html").read_text(encoding="utf-8")
        app_js = (project_root / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="analysis-panel"', index_html)
        self.assertIn('id="decision-panel"', index_html)
        self.assertIn('id="analysis-panel-tab"', index_html)
        self.assertIn('id="decision-panel-tab"', index_html)
        self.assertIn("Enter 换行 · ⌘ / Ctrl + Enter 润色", index_html)
        self.assertIn("function setResultPanel(panel)", app_js)
        self.assertIn("function renderDecisionPanel(data)", app_js)
        self.assertIn('analysisPanelTab.addEventListener("click"', app_js)
        self.assertIn('decisionPanelTab.addEventListener("click"', app_js)

    def test_frontend_declares_brand_favicon(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        index_html = (project_root / "index.html").read_text(encoding="utf-8")
        favicon = project_root / "favicon.svg"
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', index_html)
        self.assertTrue(favicon.is_file())
        self.assertIn("Prompt Sidecar", favicon.read_text(encoding="utf-8"))


    def test_output_contract_normalization(self) -> None:
        self.assertEqual(
            server.normalize_output_structure("结论，变更, 验证"),
            ["结论", "变更", "验证"],
        )
        self.assertEqual(
            server.normalize_output_structure([]),
            ["结论", "变更", "验证", "限制", "下一步"],
        )

    def test_handoff_packet_is_validated_and_conflicts_are_explicit(self) -> None:
        packet = server.validate_handoff_packet(
            {
                "source": "agentxray",
                "attention_id": "att-1",
                "objective": "完成筛选验证",
                "current_state": "逻辑已实现",
                "completed": ["实现筛选逻辑"],
                "session_refs": ["other-session"],
                "confidence": "high",
            }
        )
        self.assertEqual(packet["source"], "agentxray")
        self.assertEqual(packet["completed"], ["实现筛选逻辑"])
        self.assertEqual(
            server.continuation_conflicts(packet, "codex", "current-session"),
            ["handoff packet 的 session_refs 与当前绑定 session 不一致。"],
        )
        with self.assertRaisesRegex(ValueError, "agentxray"):
            server.validate_handoff_packet({"source": "other"})

    def test_next_step_reports_insufficient_context_without_guessing(self) -> None:
        result = server.generate_next_step(
            source="codex",
            session_id="session-1",
            context_packet={"context": "", "messages": []},
        )
        self.assertEqual(result["status"], "insufficient_context")
        self.assertTrue(result["missing"])
        self.assertEqual(result["final_prompt"], "")

    def test_next_step_normalizes_recommendation_and_renders_prompt(self) -> None:
        model_result = {
            "status": "in_progress",
            "task_goal": "让用户按项目和状态筛选 session",
            "completed": [{"item": "筛选逻辑已实现", "evidence": "相关单测通过"}],
            "evidence": ["前端筛选交互已接入"],
            "pending": ["验证空结果和刷新后的状态保持"],
            "blockers": [],
            "recommended_next_step": {
                "title": "完成会话筛选验证",
                "scope": ["项目筛选", "状态筛选", "空结果", "刷新后的状态保持"],
                "reason": "原任务还有明确的验证事项",
                "expected_output": ["验证报告"],
                "verification": ["运行相关测试", "手动操作四个场景"],
                "completion_criteria": ["四个场景均可操作", "测试通过"],
                "limitations": ["不要扩大功能范围"],
            },
            "alternatives": [
                {"title": "先补充筛选测试", "scope": ["补充缺失测试"]},
                {"title": "检查刷新状态实现", "scope": ["检查状态保持"]},
                {"title": "范围外建议不应出现为第三项", "scope": ["忽略"]},
            ],
            "out_of_scope_suggestions": ["考虑跨 session 任务看板"],
        }
        with patch("server.call_model", return_value=json.dumps(model_result, ensure_ascii=False)):
            result = server.generate_next_step(
                source="codex",
                session_id="session-1",
                context_packet={
                    "context": "USER: 让用户按项目和状态筛选 session\nASSISTANT: 逻辑已实现",
                    "messages": [
                        {"role": "user", "text": "让用户按项目和状态筛选 session"},
                        {"role": "assistant", "text": "逻辑已实现"},
                    ],
                },
            )
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(len(result["alternatives"]), 2)
        self.assertIn("当前目标", result["final_prompt"])
        self.assertIn("本轮工作范围", result["final_prompt"])
        self.assertIn("完成条件", result["final_prompt"])
        self.assertIn("不要扩大功能范围", result["final_prompt"])
        self.assertIn("运行相关测试", result["final_prompt"])
        self.assertEqual(len(result["recommendations"]), 3)

    def test_next_step_waits_for_user_decision_and_preserves_out_of_scope(self) -> None:
        model_result = {
            "status": "needs_user_decision",
            "task_goal": "确定迁移方案",
            "completed": [],
            "pending": ["选择迁移策略"],
            "blockers": ["两种策略的风险不同"],
            "clarifying_questions": ["请选择保守迁移还是快速迁移。"],
            "recommended_next_step": {
                "title": "先确认迁移策略",
                "scope": ["回答迁移策略问题"],
                "completion_criteria": ["用户确认策略"],
            },
            "out_of_scope_suggestions": ["建立长期任务看板"],
        }
        with patch("server.call_model", return_value=json.dumps(model_result)):
            result = server.generate_next_step(
                source="codex",
                session_id="session-1",
                context_packet={"context": "USER: 迁移数据\nASSISTANT: 需要选择方案", "messages": []},
            )
        self.assertEqual(result["status"], "needs_user_decision")
        self.assertIn("先暂停主变更", result["final_prompt"])
        self.assertIn("请选择保守迁移还是快速迁移。", result["final_prompt"])
        self.assertIn("建立长期任务看板", result["final_prompt"])

    def test_next_step_completed_task_recommends_verification(self) -> None:
        model_result = {
            "status": "completed",
            "task_goal": "完成日志表格改造",
            "completed": [{"item": "表格改造完成", "evidence": "构建通过"}],
            "evidence": ["构建通过"],
            "pending": [],
            "recommended_next_step": {
                "title": "确认交付结果",
                "scope": ["复核最终页面"],
                "verification": ["运行最终构建"],
                "completion_criteria": ["确认页面符合原需求"],
            },
        }
        with patch("server.call_model", return_value=json.dumps(model_result)):
            result = server.generate_next_step(
                source="codex",
                session_id="session-1",
                context_packet={"context": "USER: 改造日志\nASSISTANT: 已完成", "messages": []},
            )
        self.assertEqual(result["status"], "completed")
        self.assertIn("构建通过", result["final_prompt"])
        self.assertIn("运行最终构建", result["final_prompt"])

    def test_frontend_output_rule_fields(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('output_style: state.outputStyle', app_js)
        self.assertIn('output_structure: state.outputStructure', app_js)
        self.assertIn('id="output-style"', index_html)
        self.assertIn('id="output-structure"', index_html)



class DshBridgeTests(unittest.TestCase):
    def _write_dsh_fixture(self, root: Path, session_id: str, extra_events: list[dict] | None = None) -> Path:
        try:
            from compression import zstd

            compress = zstd.compress
        except ModuleNotFoundError:
            import zstandard

            compress = zstandard.ZstdCompressor().compress

        events = [
            {
                "type": "session",
                "version": 4,
                "id": session_id,
                "createdAt": 1_794_816_437_296,
                "cwd": "/tmp/project",
                "isSeeded": False,
                "delegationDepth": 0,
                "agentPreset": "standard",
            },
            {
                "type": "permission/preset",
                "seq": 0,
                "time": 1_794_816_437_311,
                "data": {"preset": "workspace-write"},
            },
            {
                "type": "user/message",
                "seq": 8,
                "time": 1_794_816_527_651,
                "data": {
                    "content": [{"type": "text", "text": "<system-reminder>injected</system-reminder>帮我把日志改成表格"}],
                    "role": "user",
                    "id": "msg-user",
                },
            },
            {
                "type": "session/title",
                "seq": 15,
                "time": 1_794_816_527_656,
                "data": {"title": "日志改成表格展示", "source": {"kind": "fallback"}},
            },
            {
                "type": "assistant/message",
                "seq": 17,
                "time": 1_794_816_554_380,
                "data": {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "reasoning", "text": "internal reasoning must not leak"},
                            {"type": "text", "text": "好的，先确认日志来源。"},
                        ],
                    },
                },
            },
        ]
        for event in extra_events or []:
            events.append(event)
        lines = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
        log_dir = root / "--tmp-project--" / session_id
        log_dir.mkdir(parents=True)
        log_path = log_dir / "session.v4.jsonl.zstd"
        log_path.write_bytes(compress(lines.encode("utf-8")))
        return log_path

    def test_dsh_log_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_dsh_fixture(root, "session-abc")
            parsed = server.parse_dsh_log(root / "--tmp-project--" / "session-abc" / "session.v4.jsonl.zstd")
            self.assertEqual(parsed["session_id"], "session-abc")
            self.assertEqual(parsed["cwd"], "/tmp/project")
            self.assertEqual(parsed["title"], "日志改成表格展示")
            self.assertEqual(len(parsed["messages"]), 2)
            self.assertEqual(parsed["messages"][0]["role"], "user")
            self.assertEqual(parsed["messages"][0]["text"], "帮我把日志改成表格")
            self.assertEqual(parsed["messages"][1]["role"], "assistant")
            self.assertNotIn("reasoning", parsed["messages"][1]["text"])

    def test_dsh_context_packet_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_dsh_fixture(root, "session-abc")
            packet = server.build_dsh_context_packet(
                "session-abc",
                sessions_dir=root,
                workspace_storage=root / "workspace.json",
            )
            self.assertTrue(packet["available"])
            self.assertEqual(packet["source"], "dsh")
            self.assertEqual(packet["binding_method"], "dsh-file-log")
            self.assertIn("帮我把日志改成表格", packet["context"])
            self.assertIn("好的，先确认日志来源。", packet["context"])
            sessions = server.list_dsh_sessions(10, sessions_dir=root, workspace_storage=root / "workspace.json")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["source"], "dsh")
            self.assertFalse(sessions[0]["archived"])

    def test_dsh_archived_sessions_are_hidden_from_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_dsh_fixture(root, "session-abc")
            storage = root / "workspace.json"
            storage.write_text(
                json.dumps({"global": {"archivedSessionIds": ["session-abc"]}}),
                encoding="utf-8",
            )
            sessions = server.list_dsh_sessions(10, sessions_dir=root, workspace_storage=storage)
            self.assertEqual(sessions, [])
            packet = server.build_dsh_context_packet(
                "session-abc",
                sessions_dir=root,
                workspace_storage=storage,
            )
            self.assertTrue(packet["available"])

    def test_dsh_title_falls_back_to_first_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_dsh_fixture(
                root,
                "session-notitle",
                extra_events=[
                    {
                        "type": "session/title",
                        "seq": 20,
                        "time": 1_794_816_530_000,
                        "data": {"title": "", "source": {"kind": "fallback"}},
                    }
                ],
            )
            parsed = server.parse_dsh_log(root / "--tmp-project--" / "session-notitle" / "session.v4.jsonl.zstd")
            self.assertEqual(parsed["title"], "帮我把日志改成表格")

    def test_dsh_send_posts_to_bridge_with_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "bridge.token"
            runtime_token = secrets.token_hex(16)
            token_file.write_text(f"{runtime_token}\n", encoding="utf-8")
            captured: dict = {}

            def fake_urlopen(request, timeout=None):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.header_items())
                captured["body"] = json.loads(request.data.decode("utf-8"))
                return _FakeResponse({"ok": True, "accepted": True, "requestId": "r1"})

            with patch("server.urlopen", side_effect=fake_urlopen):
                result = server.send_dsh_prompt(
                    "session-abc",
                    "润色结果",
                    mode="queue",
                    bridge_url="http://127.0.0.1:8766",
                    token_file=token_file,
                )
            self.assertEqual(captured["url"], "http://127.0.0.1:8766/prompt")
            self.assertEqual(captured["headers"].get("Authorization"), f"Bearer {runtime_token}")
            self.assertEqual(captured["body"]["sessionId"], "session-abc")
            self.assertEqual(captured["body"]["mode"], "queue")
            self.assertEqual(captured["body"]["message"], "润色结果")
            self.assertTrue(result["accepted"])

    def test_dsh_send_uses_configured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "bridge.token"
            token_file.write_text(secrets.token_hex(16), encoding="utf-8")
            captured: dict = {}

            def fake_urlopen(request, timeout=None):
                captured["timeout"] = timeout
                return _FakeResponse({"ok": True})

            with patch("server.urlopen", side_effect=fake_urlopen):
                server.send_dsh_prompt(
                    "session-abc",
                    "message",
                    token_file=token_file,
                    timeout_ms=4321,
                )
        self.assertEqual(captured["timeout"], 4.321)

    def test_dsh_bridge_passes_configured_timeout_to_send(self) -> None:
        bridge = server.DshBridge(
            {
                "read_only": False,
                "timeout_ms": 4321,
                "bridge_url": "http://127.0.0.1:8766",
            }
        )
        with patch.object(server, "send_dsh_prompt", return_value={"accepted": True}) as send:
            result = bridge.send(bridge_request("dsh", "session-abc", "message"))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["effective_model"], "session-default")
        self.assertEqual(send.call_args.kwargs["timeout_ms"], 4321)

    def test_dsh_send_requires_token_and_valid_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.token"
            with self.assertRaisesRegex(RuntimeError, "token"):
                server.send_dsh_prompt("session-abc", "msg", token_file=missing)
            token_file = Path(temp_dir) / "bridge.token"
            token_file.write_text(secrets.token_hex(16), encoding="utf-8")
            with self.assertRaises(ValueError):
                server.send_dsh_prompt("session-abc", "msg", mode="interrupt", token_file=token_file)

    def test_bridge_resolution_uses_configured_bridges(self) -> None:
        config = {
            "bridges": {
                "codex": {"enabled": True, "home": "/tmp/test-codex"},
                "dsh": {
                    "enabled": True,
                    "sessions_dir": "/tmp/test-dsh-sessions",
                    "workspace_storage": "/tmp/test-workspace.json",
                },
            }
        }
        with patch("server.load_sidecar_config", return_value=config):
            codex = server.resolve_bridge("codex")
            dsh = server.resolve_bridge("dsh")
            self.assertIsInstance(codex, server.CodexBridge)
            self.assertIsInstance(dsh, server.DshBridge)
            with self.assertRaises(ValueError):
                server.resolve_bridge("unknown-source")
        with self.assertRaisesRegex(ValueError, "queue"):
            codex.send(bridge_request("codex", "thread-1", "msg", mode="steer"))

    def test_list_all_sessions_uses_fake_bridges_and_tolerates_failure(self) -> None:
        class FakeBridge:
            def __init__(self, sessions: list[dict]) -> None:
                self.sessions = sessions

            def list_sessions(self, limit: int) -> list[dict]:
                return self.sessions[:limit]

        def fake_resolve(source: str) -> FakeBridge:
            if source == "codex":
                return FakeBridge([{"source": "codex", "session_id": "codex-test", "recency_at": 2}])
            if source == "dsh":
                raise RuntimeError("simulated unavailable bridge")
            raise AssertionError(f"unexpected source: {source}")

        with patch("server.resolve_bridge", side_effect=fake_resolve):
            sessions = server.list_all_sessions()
        self.assertEqual(
            sessions,
            [{"source": "codex", "session_id": "codex-test", "recency_at": 2}],
        )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class SidecarConfigTests(unittest.TestCase):
    def test_default_config_is_used_when_file_missing(self) -> None:
        missing = Path("/nonexistent/sidecar.config.json")
        with patch("server.CONFIG_PATH", missing):
            config = server.load_sidecar_config()
        self.assertTrue(config["bridges"]["codex"]["enabled"])
        self.assertEqual(config["bridges"]["dsh"]["bridge_url"], "http://127.0.0.1:8766")

    def test_config_file_overrides_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sidecar.config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "bridges": {
                            "dsh": {
                                "sessions_dir": "/tmp/my-dsh-sessions",
                                "bridge_url": "http://127.0.0.1:9999",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch("server.CONFIG_PATH", config_path):
                config = server.load_sidecar_config()
        self.assertEqual(config["bridges"]["dsh"]["sessions_dir"], "/tmp/my-dsh-sessions")
        self.assertEqual(config["bridges"]["dsh"]["bridge_url"], "http://127.0.0.1:9999")
        self.assertTrue(config["bridges"]["codex"]["enabled"])

    def test_ensure_config_file_copies_public_example(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "sidecar.config.json"
            example = root / "sidecar.config.example.json"
            example.write_text('{"bridges": {"codex": {"home": "~/.codex"}}}\n', encoding="utf-8")
            with patch("server.CONFIG_PATH", local), patch("server.CONFIG_EXAMPLE_PATH", example):
                server.ensure_config_file()
            self.assertEqual(local.read_text(encoding="utf-8"), example.read_text(encoding="utf-8"))

    def test_select_context_messages_respects_char_budget(self) -> None:
        big = "x" * 6_000
        messages = [
            {"role": "user", "text": big},
            {"role": "assistant", "text": big},
            {"role": "user", "text": big},
            {"role": "user", "text": "latest draft"},
        ]
        selected, context = server.select_context_messages(messages)
        self.assertEqual(len(selected), 3)
        self.assertEqual(selected[-1]["text"], "latest draft")
        self.assertEqual(selected[0]["role"], "assistant")
        truncated = "y" * 20_000
        selected, _ = server.select_context_messages([{"role": "user", "text": truncated}])
        self.assertEqual(selected[0]["text"].endswith("[truncated]"), True)


class FrontendBridgeContractTests(unittest.TestCase):
    def test_frontend_persists_and_displays_effective_send_model(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="send-effective-model"', index_html)
        self.assertIn("effectiveModel", app_js)
        self.assertIn("data.effective_model", app_js)
        self.assertIn("renderEffectiveModel", app_js)
        self.assertIn("实际使用模型", app_js)
        self.assertIn("effectiveModelLabel", app_js)

    def test_frontend_session_modal_and_source_aware_send(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="open-session-modal"', index_html)
        self.assertIn('id="session-modal"', index_html)
        self.assertIn('id="session-filter"', index_html)
        self.assertIn('id="send-mode-group"', index_html)
        self.assertIn('source: state.source', app_js)
        self.assertIn('mode: state.sendMode', app_js)
        self.assertIn('fetchJson("/api/sessions")', app_js)
        self.assertIn('function renderSendModes()', app_js)

    def test_frontend_session_refresh_is_in_modal_header(self) -> None:
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        modal_start = index_html.index('<div id="session-modal"')
        modal_html = index_html[modal_start:]
        header_start = modal_html.index('<div class="modal-head">')
        header_end = modal_html.index('</div>\n            <input id="session-filter"', header_start)
        modal_header = modal_html[header_start:header_end]
        context_start = index_html.index('<div class="context-head session-context-status">')
        context_end = index_html.index('</div>\n                    <details>', context_start)
        context_html = index_html[context_start:context_end]

        self.assertIn('id="refresh-sessions"', modal_header)
        self.assertIn('id="close-session-modal"', modal_header)
        self.assertNotIn('id="refresh-sessions"', context_html)
        self.assertNotIn('id="modal-refresh"', index_html)

    def test_frontend_loads_sessions_incrementally_with_canonical_state(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('state.session_id', app_js)
        self.assertIn("contextLoadGeneration", app_js)
        self.assertIn('url.searchParams.set("session_id", state.sessionId)', app_js)
        self.assertIn('async function refreshSessions()', app_js)
        self.assertIn('state.sessions = sessions.sort(', app_js)
        self.assertIn('"正在加载会话..."', app_js)
        self.assertIn('["", "undefined", "null"].includes(normalized)', app_js)

    def test_frontend_no_dsh_bridge_label(self) -> None:
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("dsh-bridge-label", index_html)
        self.assertNotIn("dsh-bridge-label", app_js)
        self.assertNotIn("DSH 桥已连接", index_html)
        self.assertNotIn("DSH 桥已连接", app_js)

    def test_frontend_separate_settings_and_grouped_sessions(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="open-settings-modal"', index_html)
        self.assertIn('id="settings-modal"', index_html)
        self.assertIn('id="settings-save"', index_html)
        self.assertIn(">选择会话</button>", index_html)
        self.assertIn('fetchJson("/api/config"', app_js)
        self.assertIn("session-group-head", app_js)
        self.assertIn("BRIDGE_FIELDS", app_js)
        self.assertIn("capabilitiesFor", app_js)
        self.assertIn('id="bridge-selector"', app_js)
        self.assertIn("function renderBridgeDetail(source)", app_js)
        self.assertIn("function captureBridgeForm()", app_js)
        self.assertIn("modelSelectionHtml", app_js)
        self.assertIn("data-model-mode", app_js)
        self.assertIn("data-model-id", app_js)
        self.assertIn("model_selection", app_js)


class SidecarConfigEndpointTests(unittest.TestCase):
    def test_sanitize_bridges_drops_empty_and_enforces_strict_types(self) -> None:
        cleaned = server._sanitize_bridges(
            {
                "codex": {"enabled": True, "home": ""},
                "dsh": {"enabled": False, "bridge_url": "http://127.0.0.1:9999"},
                "openclaw": {"enabled": True, "auth_ref": ""},
            }
        )
        self.assertEqual(cleaned["codex"], {"enabled": True})
        self.assertEqual(cleaned["dsh"]["enabled"], False)
        self.assertEqual(cleaned["dsh"]["bridge_url"], "http://127.0.0.1:9999")
        self.assertEqual(cleaned["openclaw"]["auth_ref"], "")
        with self.assertRaises(ValueError):
            server._sanitize_bridges({"": {"enabled": True}})
        with self.assertRaises(ValueError):
            server._sanitize_bridges("not-a-dict")
        with self.assertRaisesRegex(ValueError, "boolean"):
            server._sanitize_bridges({"dsh": {"enabled": "false"}})
        with self.assertRaisesRegex(ValueError, "boolean"):
            server._sanitize_bridges({"dsh": {"enabled": True, "read_only": "false"}})
        with self.assertRaisesRegex(ValueError, "boolean"):
            server._sanitize_bridges({"dsh": {"enabled": 1}})
        with self.assertRaisesRegex(ValueError, "integer"):
            server._sanitize_bridges({"dsh": {"enabled": True, "timeout_ms": False}})
        with self.assertRaisesRegex(ValueError, "integer"):
            server._sanitize_bridges({"dsh": {"enabled": True, "retry_count": False}})

    def test_model_selection_validation_and_capabilities(self) -> None:
        cleaned = server._sanitize_bridges(
            {
                "claude": {
                    "model_selection": {
                        "mode": "explicit",
                        "model_id": "sonnet",
                    }
                },
                "codex": {
                    "model_selection": {
                        "mode": "bridge_default",
                        "model_id": "ignored",
                    }
                },
            }
        )
        self.assertEqual(
            cleaned["claude"]["model_selection"],
            {"mode": "explicit", "model_id": "sonnet"},
        )
        self.assertEqual(
            cleaned["codex"]["model_selection"],
            {"mode": "bridge_default", "model_id": ""},
        )
        with self.assertRaisesRegex(ValueError, "object"):
            server._sanitize_bridges({"claude": {"model_selection": "sonnet"}})
        with self.assertRaisesRegex(ValueError, "mode"):
            server._sanitize_bridges(
                {"claude": {"model_selection": {"mode": "automatic", "model_id": ""}}}
            )
        with self.assertRaisesRegex(ValueError, "required"):
            server._sanitize_bridges(
                {"claude": {"model_selection": {"mode": "explicit", "model_id": " "}}}
            )

        codex = server.CodexBridge({"read_only": False})
        claude = server.ClaudeBridge(
            {
                "read_only": False,
                "model_selection": {"mode": "explicit", "model_id": "sonnet"},
            }
        )
        dsh = server.DshBridge(
            {
                "read_only": False,
                "model_selection": {"mode": "explicit", "model_id": "dsh-model"},
            }
        )
        self.assertEqual(codex.capabilities()["model_selection"]["mode"], "fixed")
        self.assertEqual(claude.capabilities()["model_selection"]["mode"], "dynamic")
        self.assertEqual(dsh.capabilities()["model_selection"]["mode"], "fixed")
        self.assertEqual(server.model_plan_for_send(codex, "codex"), (None, "current-thread"))
        self.assertEqual(
            server.model_plan_for_send(claude, "claude"),
            ("sonnet", "sonnet"),
        )
        with self.assertRaisesRegex(ValueError, "does not support"):
            server.model_plan_for_send(dsh, "dsh")

    def test_save_sidecar_config_enforces_model_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sidecar.config.json"
            with patch("server.CONFIG_PATH", config_path):
                with self.assertRaisesRegex(ValueError, "does not support"):
                    server.save_sidecar_config(
                        {
                            "codex": {
                                "enabled": True,
                                "model_selection": {
                                    "mode": "explicit",
                                    "model_id": "unsupported-model",
                                },
                            }
                        }
                    )
                saved = server.save_sidecar_config(
                    {
                        "claude": {
                            "enabled": True,
                            "model_selection": {
                                "mode": "explicit",
                                "model_id": "sonnet",
                            },
                        }
                    }
                )
        self.assertEqual(
            saved["bridges"]["claude"]["model_selection"]["model_id"],
            "sonnet",
        )

    def test_save_sidecar_config_merges_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sidecar.config.json"
            with patch("server.CONFIG_PATH", config_path):
                saved = server.save_sidecar_config(
                    {"dsh": {"enabled": False, "sessions_dir": ""}}
                )
            self.assertFalse(saved["bridges"]["dsh"]["enabled"])
            self.assertEqual(
                saved["bridges"]["dsh"]["sessions_dir"],
                str(Path.home() / ".dsh" / "sessions"),
            )
            self.assertTrue(saved["bridges"]["codex"]["enabled"])
            self.assertTrue(config_path.exists())

    def test_public_bridge_configs_do_not_run_health_probes(self) -> None:
        config = {"bridges": {"codex": {"enabled": True, "adapter": "codex", "home": "/tmp/codex"}}}
        with patch("server.load_sidecar_config", return_value=config):
            with patch("server.bridge_statuses", side_effect=AssertionError("health probe called")):
                entries = server.public_bridge_configs()
        self.assertEqual(entries["codex"]["adapter"], "codex")
        self.assertNotIn("health", entries["codex"])
        self.assertNotIn("capabilities", entries["codex"])

    def test_bridge_registry_contains_all_builtin_adapters(self) -> None:
        names = set(server.BRIDGE_REGISTRY.adapter_names())
        self.assertTrue({
            "codex", "dsh", "claude", "workbuddy", "openclaw", "openhands",
            "cursor", "copilot", "gemini", "devin", "doubao_mcp",
        }.issubset(names))

    def test_bridge_statuses_expose_capabilities_and_health(self) -> None:
        config = {
            "bridges": {
                "codex": {"enabled": False, "adapter": "codex", "label": "Codex"},
                "claude": {
                    "enabled": True,
                    "adapter": "claude",
                    "label": "Claude Code",
                    "read_only": True,
                    "projects_dir": "/tmp/claude-projects",
                },
            }
        }
        with patch("server.load_sidecar_config", return_value=config):
            statuses = server.bridge_statuses()
        self.assertEqual(statuses["codex"]["health"]["status"], "disabled")
        self.assertFalse(statuses["codex"]["capabilities"]["list_sessions"])
        self.assertEqual(statuses["claude"]["adapter"], "claude")
        self.assertIn("send", statuses["claude"]["capabilities"])

    def test_public_bridge_config_strips_secrets_and_url_credentials(self) -> None:
        public = server.public_bridge_config(
            {
                "enabled": True,
                "adapter": "workbuddy",
                "endpoint": "https://user:pass@example.test/path?token=secret",
                "auth_ref": "WORKBUDDY_ACCESS_TOKEN",
                "token": "secret-token",
                "headers": {"Authorization": "Bearer secret"},
            }
        )
        self.assertNotIn("token", public)
        self.assertNotIn("headers", public)
        self.assertEqual(public["endpoint"], "https://example.test/path")
        self.assertEqual(public["auth_ref"], "WORKBUDDY_ACCESS_TOKEN")

    def test_read_only_capabilities_disable_send(self) -> None:
        codex = server.CodexBridge({"read_only": True})
        dsh = server.DshBridge({"read_only": True})
        self.assertFalse(codex.capabilities()["send"])
        self.assertEqual(codex.capabilities()["send_modes"], [])
        self.assertFalse(dsh.capabilities()["send"])
        self.assertEqual(dsh.capabilities()["send_modes"], [])

    def test_resolve_bridge_uses_adapter_name(self) -> None:
        config = {
            "bridges": {
                "my-claude": {
                    "enabled": True,
                    "adapter": "claude",
                    "home": "/tmp/claude-home",
                    "projects_dir": "/tmp/claude-projects",
                    "read_only": True,
                },
                "my-workbuddy": {
                    "enabled": True,
                    "adapter": "workbuddy",
                    "surface": "cloud_task",
                    "read_only": True,
                    "auth_ref": "TEST_WORKBUDDY_TOKEN",
                },
            }
        }
        with patch("server.load_sidecar_config", return_value=config):
            claude = server.resolve_bridge("my-claude")
            workbuddy = server.resolve_bridge("my-workbuddy")
        self.assertIsInstance(claude, server.ClaudeBridge)
        self.assertIsInstance(workbuddy, server.WorkBuddyBridge)
        self.assertEqual(claude.source, "my-claude")
        self.assertEqual(workbuddy.source, "my-workbuddy")

    def test_custom_source_is_retagged_in_sessions_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "550e8400-e29b-41d4-a716-446655440002"
            transcript = root / "project" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": str(root),
                        "message": {"role": "user", "content": "alias source"},
                    }
                ),
                encoding="utf-8",
            )
            bridge = server.ClaudeBridge(
                {"home": str(root), "projects_dir": str(root), "read_only": True}
            )
            bridge.source = "my-claude"
            sessions = bridge.list_sessions(1)
            packet = bridge.build_context_packet(session_id)
        self.assertEqual(sessions[0]["source"], "my-claude")
        self.assertEqual(packet["source"], "my-claude")

    def test_claude_transcript_parser_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "550e8400-e29b-41d4-a716-446655440000"
            transcript = root / "project" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "sessionId": session_id,
                                "cwd": "/tmp/project",
                                "timestamp": "2026-11-17T10:00:00.000Z",
                                "message": {"role": "user", "content": "请润色这句话"},
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "sessionId": session_id,
                                "cwd": "/tmp/project",
                                "timestamp": "2026-11-17T10:00:01.000Z",
                                "message": {"role": "assistant", "content": [{"type": "text", "text": "已润色"}]},
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            messages, meta = server.read_claude_transcript(transcript)
            packet = server.build_claude_context_packet(session_id, projects_dir=root)
        self.assertEqual(meta["session_id"], session_id)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertTrue(packet["available"])
        self.assertIn("请润色这句话", packet["context"])

    def test_claude_send_builds_resume_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "550e8400-e29b-41d4-a716-446655440001"
            transcript = root / "project" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": str(root),
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"result":"ok"}\n',
                stderr="",
            )
            with patch("server.subprocess.run", return_value=completed) as run:
                result = server.send_claude_message(
                    session_id,
                    "请继续",
                    projects_dir=root,
                    binary="claude-test",
                )
        command = run.call_args.args[0]
        self.assertEqual(command[:6], ["claude-test", "-p", "--resume", session_id, "--output-format", "json"])
        self.assertEqual(command[6], "请继续")
        self.assertEqual(run.call_args.kwargs["cwd"], str(root))
        self.assertTrue(result["accepted"])

    def test_claude_send_includes_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            session_id = "550e8400-e29b-41d4-a716-446655440011"
            transcript = root / "project" / f"{session_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": str(root),
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"result":"ok"}\n',
                stderr="",
            )
            with patch("server.subprocess.run", return_value=completed) as run:
                result = server.send_claude_message(
                    session_id,
                    "请继续",
                    projects_dir=root,
                    binary="claude-test",
                    model_id="sonnet",
                )
        command = run.call_args.args[0]
        self.assertEqual(
            command[:8],
            [
                "claude-test",
                "-p",
                "--resume",
                session_id,
                "--model",
                "sonnet",
                "--output-format",
                "json",
            ],
        )
        self.assertEqual(command[8], "请继续")
        self.assertTrue(result["accepted"])

    def test_codex_queue_rejects_direct_model_override(self) -> None:
        bridge = server.CodexBridge({"read_only": False})
        with patch("server.send_queued_message") as send:
            with self.assertRaisesRegex(ValueError, "不支持覆盖模型"):
                bridge.send(
                    bridge_request("codex", "thread-1", "继续", model_id="gpt-5.2")
                )
        send.assert_not_called()

    def test_codex_bridge_returns_fixed_effective_model(self) -> None:
        bridge = server.CodexBridge({"read_only": False})
        with patch(
            "server.send_queued_message",
            return_value={"accepted": True, "queue_id": "queue-1"},
        ):
            result = bridge.send(bridge_request("codex", "thread-1", "继续"))
        self.assertEqual(result["effective_model"], "current-thread")

    def test_claude_bridge_returns_dynamic_effective_model(self) -> None:
        bridge = server.ClaudeBridge({"read_only": False})
        with patch(
            "server.send_claude_message",
            return_value={"accepted": True, "queue_id": "run-1"},
        ) as send:
            result = bridge.send(
                bridge_request("claude", "session-1", "继续", model_id="sonnet")
            )
        self.assertEqual(result["effective_model"], "sonnet")
        self.assertEqual(send.call_args.kwargs["model_id"], "sonnet")

    def test_claude_send_rejects_missing_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch("server.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "不存在"):
                    server.send_claude_message("missing-session", "继续", projects_dir=Path(temp_dir))
        run.assert_not_called()

    def test_workbuddy_task_list_uses_bearer_token(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            return _FakeResponse(
                {
                    "tasks": [
                        {
                            "task_id": "task-1",
                            "name": "测试任务",
                            "status": "working",
                            "updated_at": "2026-11-17T10:00:00Z",
                        }
                    ]
                }
            )

        with patch.dict("os.environ", {"TEST_WORKBUDDY_TOKEN": "dummy-token"}, clear=False):
            with patch("server.urlopen", side_effect=fake_urlopen):
                tasks = server.list_workbuddy_tasks(
                    {"endpoint": "https://example.test", "auth_ref": "TEST_WORKBUDDY_TOKEN"},
                    limit=5,
                )
        self.assertEqual(tasks[0]["session_id"], "task-1")
        self.assertIn("/openapi/v2/tasks?", captured["url"])
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer dummy-token")

    def test_workbuddy_acp_client_correlates_sse_response(self) -> None:
        class FakeStream:
            headers = {"Acp-Connection-Id": "connection-1"}

            def __init__(self):
                self.lines = [
                    b'data: {"jsonrpc":"2.0","method":"session/update","params":{"x":1}}\n',
                    b'data: {"jsonrpc":"2.0","id":99,"method":"session/request_permission","params":{}}\n',
                    b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n',
                ]

            def readline(self):
                return self.lines.pop(0) if self.lines else b""

            def close(self):
                return None

        class FakePostResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

        requests = []
        stream = FakeStream()

        def fake_urlopen(request, timeout=None):
            requests.append(request)
            return stream if request.get_method() == "GET" else FakePostResponse()

        with patch("server.urlopen", side_effect=fake_urlopen):
            with server.WorkBuddyACPClient("https://example.test/acp", "dummy-token") as client:
                result = client.request("initialize", {"protocolVersion": 1})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1].headers.get("Acp-connection-id"), "connection-1")
        permission_response = json.loads(requests[2].data.decode("utf-8"))
        self.assertEqual(permission_response["id"], 99)
        self.assertEqual(permission_response["result"], {"outcome": "cancelled"})

    def test_workbuddy_task_and_acp_message_mapping(self) -> None:
        session = server.workbuddy_task_session(
            {
                "task_id": "task-1",
                "name": "测试任务",
                "status": "working",
                "updated_at": "2026-11-17T10:00:00Z",
            }
        )
        messages = server.extract_acp_messages(
            {"result": {"messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}}
        )
        self.assertEqual(session["session_id"], "task-1")
        self.assertEqual(session["source"], "workbuddy_cloud")
        self.assertEqual(messages, [{"role": "user", "text": "你好"}])

    def test_workbuddy_local_sessions_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            db_path = home / "workbuddy.db"
            projects_dir = home / "projects" / "project"
            projects_dir.mkdir(parents=True)
            session_id = "local-session-1"
            transcript = projects_dir / f"{session_id}.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "message",
                                "role": "user",
                                "sessionId": session_id,
                                "cwd": "/tmp/workbuddy-project",
                                "content": [{"type": "input_text", "text": "本地会话"}],
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "type": "message",
                                "role": "assistant",
                                "sessionId": session_id,
                                "cwd": "/tmp/workbuddy-project",
                                "content": [{"type": "output_text", "text": "本地回复"}],
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, custom_title TEXT, "
                "cwd TEXT, status TEXT, updated_at INTEGER, last_activity_at INTEGER, "
                "deleted_at INTEGER, transport TEXT)"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, "本地标题", None, "/tmp/workbuddy-project", "completed", 1000, 2000, None, "local"),
            )
            connection.commit()
            connection.close()
            sessions = server.list_workbuddy_local_sessions(db_path, home / "projects", limit=5)
            packet = server.build_workbuddy_local_context_packet(
                {"home": str(home), "db_path": str(db_path), "projects_dir": str(home / "projects")},
                session_id,
            )
        self.assertEqual(sessions[0]["source"], "workbuddy_local")
        self.assertEqual(sessions[0]["title"], "本地标题")
        self.assertTrue(packet["available"])
        self.assertIn("本地会话", packet["context"])

    def test_workbuddy_local_send_uses_resume_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            projects_dir = home / "projects" / "project"
            projects_dir.mkdir(parents=True)
            session_id = "local-session-2"
            (projects_dir / f"{session_id}.jsonl").write_text("", encoding="utf-8")
            db_path = home / "workbuddy.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT)")
            connection.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, str(home)))
            connection.commit()
            connection.close()
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"result":"ok"}\n',
                stderr="",
            )
            config = {
                "home": str(home),
                "db_path": str(db_path),
                "projects_dir": str(home / "projects"),
                "timeout_ms": 30000,
            }
            with patch("server.workbuddy_cli_command", return_value=["node", "cli.mjs"]):
                with patch("server.subprocess.run", return_value=completed) as run:
                    result = server.send_workbuddy_local_prompt(config, session_id, "继续")
        command = run.call_args.args[0]
        self.assertEqual(command[:7], ["node", "cli.mjs", "-p", "--resume", session_id, "--output-format", "json"])
        self.assertEqual(command[7], "继续")
        self.assertEqual(run.call_args.kwargs["env"]["WORKBUDDY_CONFIG_DIR"], str(home))
        self.assertTrue(result["accepted"])

    def test_workbuddy_bridge_returns_fixed_effective_model(self) -> None:
        bridge = server.WorkBuddyBridge({"surface": "local", "read_only": False})
        with patch(
            "server.send_workbuddy_local_prompt",
            return_value={"accepted": True, "mode": "queue"},
        ):
            result = bridge.send(bridge_request("workbuddy_local", "session-1", "继续"))
        self.assertEqual(result["effective_model"], "session-default")

    def test_retry_helper_retries_transport_error(self) -> None:
        responses = [server.URLError("temporary"), _FakeResponse({"ok": True})]
        with patch("server.urlopen", side_effect=responses) as mocked:
            result = server.request_json_with_retry(
                "https://example.test/api",
                timeout_seconds=1,
                retry_count=1,
            )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mocked.call_count, 2)

    def test_retry_helper_surfaces_auth_error_without_retry(self) -> None:
        error = server.HTTPError(
            "https://example.test/api",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":"bad token"}'),
        )
        self.addCleanup(error.close)
        with patch("server.urlopen", side_effect=error) as mocked:
            with self.assertRaises(server.BridgeAuthError):
                server.request_json_with_retry("https://example.test/api", retry_count=2)
        self.assertEqual(mocked.call_count, 1)

    def test_openclaw_mapping_and_send_modes(self) -> None:
        row = server.openclaw_session_row(
            {
                "sessionKey": "agent:main:test",
                "sessionId": "native-1",
                "displayName": "OpenClaw 测试",
                "cwd": "/tmp/openclaw",
                "updatedAt": "2026-11-17T10:00:00Z",
            }
        )
        messages = server.openclaw_context_messages(
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]}
        )
        bridge = server.OpenClawBridge(
            {"endpoint": "ws://127.0.0.1:18789", "read_only": False}
        )
        with patch("server.openclaw_rpc", return_value={"runId": "run-1"}) as rpc:
            result = bridge.send(
                bridge_request("openclaw", "agent:main:test", "继续", mode="steer")
            )
        params = rpc.call_args.args[2]
        self.assertEqual(row["session_id"], "agent:main:test")
        self.assertEqual(messages, [{"role": "user", "text": "你好"}])
        self.assertEqual(params["queueMode"], "steer")
        self.assertTrue(params["idempotencyKey"])
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["effective_model"], "session-default")

    def test_openhands_mapping_and_send(self) -> None:
        row = server.openhands_session_row(
            {
                "id": "conversation-1",
                "title": "OpenHands 测试",
                "execution_status": "idle",
                "workspace": {"working_dir": "/tmp/openhands"},
                "updated_at": "2026-11-17T10:00:00Z",
            }
        )
        messages = server.openhands_context_messages(
            {
                "items": [
                    {"kind": "MessageEvent", "source": "agent", "content": "新回复"},
                    {"kind": "MessageEvent", "source": "user", "content": [{"type": "text", "text": "旧消息"}]},
                ]
            }
        )
        bridge = server.OpenHandsBridge({"endpoint": "http://127.0.0.1:3000", "read_only": False})
        with patch("server.openhands_request", return_value={"success": True}) as request:
            result = bridge.send(bridge_request("openhands", "conversation-1", "继续"))
        self.assertEqual(row["session_id"], "conversation-1")
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(request.call_args.kwargs["payload"]["role"], "user")
        self.assertEqual(request.call_args.kwargs["retry_count"], 0)
        self.assertEqual(result["mode"], "queue")
        self.assertEqual(result["effective_model"], "session-default")

    def test_cursor_mapping_and_send_modes(self) -> None:
        row = server.cursor_session_row(
            {
                "id": "bc-1",
                "name": "Cursor 测试",
                "status": "ACTIVE",
                "updatedAt": "2026-11-17T10:00:00Z",
            }
        )
        bridge = server.CursorBridge({"endpoint": "https://api.cursor.com", "read_only": False})
        with patch("server.cursor_request", return_value={"run": {"id": "run-1"}}) as request:
            result = bridge.send(bridge_request("cursor", "bc-1", "继续", mode="steer"))
        self.assertEqual(row["session_id"], "bc-1")
        self.assertEqual(request.call_args.args[1:3], ("POST", "/v1/agents/bc-1/runs"))
        self.assertEqual(request.call_args.args[3]["delivery"], "steer")
        self.assertEqual(result["mode"], "steer")
        self.assertEqual(result["effective_model"], "session-default")

    def test_copilot_task_is_metadata_only(self) -> None:
        task = server.copilot_task_session(
            {
                "id": "task-1",
                "name": "Copilot 测试",
                "state": "in_progress",
                "html_url": "https://github.com/example/repo/agents/task-1",
                "updated_at": "2026-11-17T10:00:00Z",
            }
        )
        bridge = server.CopilotBridge({"auth_ref": "GITHUB_TOKEN"})
        self.assertEqual(task["repo"], "example/repo")
        self.assertEqual(task["session_id"], "example/repo#task-1")
        self.assertFalse(bridge.capabilities()["send"])
        self.assertEqual(bridge.capabilities()["context_mode"], "metadata")

    def test_gemini_session_parser_and_send(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            chats = root / "project-hash" / "chats"
            chats.mkdir(parents=True)
            session_id = "gemini-session-1"
            (chats / f"{session_id}.json").write_text(
                json.dumps(
                    {
                        "title": "Gemini 测试",
                        "cwd": "/tmp/gemini",
                        "history": [
                            {"role": "user", "content": "你好"},
                            {"role": "model", "content": "你好，我在。"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            bridge = server.GeminiBridge(
                {
                    "bin": "gemini-test",
                    "sessions_dir": str(root),
                    "read_only": False,
                }
            )
            with patch("server.shutil.which", return_value="/usr/local/bin/gemini-test"):
                sessions = bridge.list_sessions(5)
                packet = bridge.build_context_packet(session_id)
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
            with patch("server.subprocess.run", return_value=completed) as run:
                result = bridge.send(bridge_request("gemini", session_id, "继续"))
        self.assertEqual(sessions[0]["source"], "gemini")
        self.assertTrue(packet["available"])
        command = run.call_args.args[0]
        self.assertEqual(command[:3], ["gemini-test", "--resume", session_id])
        self.assertEqual(command[3:], ["-p", "继续"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["effective_model"], "session-default")

    def test_devin_mapping_and_send(self) -> None:
        row = server.devin_session_row(
            {
                "devin_id": "devin-1",
                "title": "Devin 测试",
                "status": "running",
                "updated_at": "2026-11-17T10:00:00Z",
            }
        )
        bridge = server.DevinBridge(
            {"organization_id": "org-1", "read_only": False, "auth_ref": "DEVIN_API_KEY"}
        )
        with patch("server.devin_request", return_value={"ok": True}) as request:
            result = bridge.send(bridge_request("devin", "devin-1", "继续"))
        self.assertEqual(row["session_id"], "devin-1")
        self.assertIn("/v3/organizations/org-1/sessions/devin-1/messages", request.call_args.args[2])
        self.assertEqual(result["mode"], "queue")
        self.assertEqual(result["effective_model"], "session-default")

    def test_doubao_work_is_read_only(self) -> None:
        bridge = server.DoubaoWorkBridge({"script": str(Path(__file__).resolve().parents[1] / "scripts" / "prompt_sidecar_mcp.py")})
        self.assertFalse(bridge.capabilities()["list_sessions"])
        self.assertFalse(bridge.capabilities()["send"])
        self.assertTrue(bridge.health()["available"])
        with self.assertRaises(ValueError):
            bridge.send(bridge_request("doubao_work", "session-1", "不要发送"))

    def test_prompt_sidecar_mcp_exposes_only_read_tools(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "prompt_sidecar_mcp.py"
        messages = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            ]
        ) + "\n"
        completed = subprocess.run(
            [sys.executable, str(script)],
            input=messages,
            capture_output=True,
            text=True,
            check=False,
        )
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        tools = responses[1]["result"]["tools"]
        self.assertEqual({tool["name"] for tool in tools}, {
            "prompt_sidecar_health",
            "prompt_sidecar_sessions",
            "prompt_sidecar_context",
        })

    def test_codex_app_server_thread_mapping(self) -> None:
        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def list_threads(self, limit):
                return [
                    {
                        "id": "thread-1",
                        "name": "会话一",
                        "preview": "preview",
                        "cwd": "/tmp/project",
                        "path": "/tmp/rollout.jsonl",
                        "updatedAt": 1_700_000_000,
                        "recencyAt": 1_700_000_100,
                    }
                ]

        with patch("server.CodexAppServerClient", FakeClient):
            sessions = server.list_codex_app_server_threads(1)
        self.assertEqual(sessions[0]["session_id"], "thread-1")
        self.assertEqual(sessions[0]["source"], "codex")
        self.assertEqual(sessions[0]["cwd"], "/tmp/project")


if __name__ == "__main__":
    unittest.main()

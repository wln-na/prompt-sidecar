from __future__ import annotations

import json
import secrets
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


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
        self.assertIn('fetch("/api/send"', app_js)
        self.assertIn('sendButton.addEventListener("click", sendToCurrentSession)', app_js)
        self.assertIn('copyButton.addEventListener("click"', app_js)


    def test_output_contract_normalization(self) -> None:
        self.assertEqual(
            server.normalize_output_structure("结论，变更, 验证"),
            ["结论", "变更", "验证"],
        )
        self.assertEqual(
            server.normalize_output_structure([]),
            ["结论", "变更", "验证", "限制", "下一步"],
        )

    def test_frontend_output_rule_fields(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('output_style: state.outputStyle', app_js)
        self.assertIn('output_structure: state.outputStructure', app_js)
        self.assertIn('id="output-style"', index_html)
        self.assertIn('id="output-structure"', index_html)



class DshBridgeTests(unittest.TestCase):
    def _write_dsh_fixture(self, root: Path, session_id: str, extra_events: list[dict] | None = None) -> Path:
        from compression import zstd

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
        log_path.write_bytes(zstd.compress(lines.encode("utf-8")))
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
            codex.send("thread-1", "msg", mode="steer")

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
    def test_frontend_session_modal_and_source_aware_send(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        index_html = (Path(__file__).resolve().parents[1] / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="open-session-modal"', index_html)
        self.assertIn('id="session-modal"', index_html)
        self.assertIn('id="session-filter"', index_html)
        self.assertIn('data-send-mode="steer"', index_html)
        self.assertIn('source: state.source', app_js)
        self.assertIn('mode: state.sendMode', app_js)
        self.assertIn('/api/sessions?source=${encodeURIComponent(source)}', app_js)
        self.assertIn('dsh-file-log', app_js)

    def test_frontend_loads_sessions_incrementally_with_canonical_state(self) -> None:
        app_js = (Path(__file__).resolve().parents[1] / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('state.session_id', app_js)
        self.assertIn('url.searchParams.set("session_id", state.sessionId)', app_js)
        self.assertIn('function mergeSessionsForSource(source, sessions)', app_js)
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
        self.assertIn(">会话列表</button>", index_html)
        self.assertIn('fetch("/api/config"', app_js)
        self.assertIn("session-group-head", app_js)
        self.assertIn("BRIDGE_DEFAULTS", app_js)


class SidecarConfigEndpointTests(unittest.TestCase):
    def test_sanitize_bridges_drops_empty_and_coerces_enabled(self) -> None:
        cleaned = server._sanitize_bridges(
            {
                "codex": {"enabled": True, "home": ""},
                "dsh": {"enabled": "yes", "bridge_url": "http://127.0.0.1:9999"},
            }
        )
        self.assertEqual(cleaned["codex"], {"enabled": True})
        self.assertEqual(cleaned["dsh"]["enabled"], True)
        self.assertEqual(cleaned["dsh"]["bridge_url"], "http://127.0.0.1:9999")
        with self.assertRaises(ValueError):
            server._sanitize_bridges({"": {"enabled": True}})
        with self.assertRaises(ValueError):
            server._sanitize_bridges("not-a-dict")

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


if __name__ == "__main__":
    unittest.main()

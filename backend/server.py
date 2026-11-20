#!/usr/bin/env python3
"""Local Prompt Sidecar server with explicit Codex thread binding."""

from __future__ import annotations

import argparse
import base64
import json
import ipaddress
import os
import re
import selectors
import shutil
import sqlite3
import subprocess
import time
import tomllib
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from bridges import (
    BRIDGE_REGISTRY,
    BridgeAuthError,
    BridgeCapabilities,
    BridgeSendRequest,
    BridgeHealth,
    BridgeUnavailableError,
    ContextPacket,
    ModelSelectionConfig,
    SendResult,
    SessionBridge,
    SessionRecord,
    dynamic_model_selection,
    empty_context_packet,
    fixed_model_selection,
    retag_context,
    retag_sessions,
    unsupported_model_selection,
)


APP_ROOT = Path(__file__).resolve().parents[1]
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
STATE_DB = CODEX_HOME / "state_5.sqlite"
GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"
SIDECAR_URL_MARKER = "127.0.0.1:8765"
APP_VERSION = "0.4.0"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or ""
ALLOWED_MODES = {"quick", "structured", "agent", "system"}
ALLOWED_OUTPUT_STYLES = {"concise", "standard", "detailed"}
ALLOWED_SEND_MODES = {"queue", "steer"}
CONTINUATION_STATUSES = {
    "ready",
    "in_progress",
    "blocked",
    "needs_user_decision",
    "completed",
    "insufficient_context",
}
CONTINUATION_DECISION_TYPES = {
    "ordinary_implementation",
    "goal_conflict",
    "scope_expansion",
    "production_or_destructive",
    "permission_or_secret",
    "external_message",
    "insufficient_context",
    "explicit_human_gate",
}
HUMAN_DECISION_TYPES = {
    "goal_conflict",
    "scope_expansion",
    "production_or_destructive",
    "permission_or_secret",
    "external_message",
    "insufficient_context",
    "explicit_human_gate",
}
DEFAULT_OUTPUT_STRUCTURE = ["结论", "变更", "验证", "限制", "下一步"]
CODEX_EXECUTION_PROTOCOL = """Codex 执行协议：
1. 当前 Codex 主 Agent 是任务总指挥，负责理解目标、组织执行、统一决策和最终交付。
2. 直接使用 Codex 原生 Planner、Agent Team、Subagent 和多会话通信能力。
3. 先确认任务目标、当前现状、约束、完成条件和可验证证据，再开始执行。
4. 将任务拆分为可独立执行的子任务；对可并行的子任务分派给不同 Agent 或会话。
5. 通过多会话通信传递上下文、同步进度、收集结果、处理阻塞并避免重复工作。
6. 主 Agent 负责统一决策、合并结果、解决冲突，并对最终结果负责。
7. 普通方案选择、任务拆分、执行顺序、实现细节和验证方式由主 Agent 自主决定，
   不要因为这些事项逐项询问用户。
8. 持续执行，直到完成条件满足；验证失败时，在原任务范围内继续修复和验证。
9. 对工具、MCP、权限、审批和沙箱遵守当前运行时的实际边界；资源或权限不足时，
   明确报告阻塞原因和已经完成的部分。
10. 最后报告实际完成内容、验证结果、失败项和遗留问题。

只有以下情况才请求人类决策：
- 用户目标存在互相冲突的解释；
- 需要扩大原始任务范围；
- 需要删除、发布、部署或修改生产环境；
- 需要新增权限、使用秘密或向外部系统发送消息；
- 当前信息不足以判断业务目标或完成条件。"""
MAX_BODY_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 18_000
MAX_MESSAGES = 18
MAX_OUTPUT_TOKENS = 1_600
class RequestError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


CONFIG_PATH = APP_ROOT / "config" / "sidecar.config.json"
CONFIG_EXAMPLE_PATH = APP_ROOT / "config" / "sidecar.config.example.json"


try:  # Python 3.14+: standard-library zstd
    from compression import zstd as _zstd_stdlib

    def decompress_zstd(data: bytes) -> bytes:
        return _zstd_stdlib.decompress(data)

except ImportError:  # pragma: no cover - depends on interpreter version
    try:
        import io as _io
        import zstandard as _zstandard

        def decompress_zstd(data: bytes) -> bytes:
            reader = _zstandard.ZstdDecompressor().stream_reader(_io.BytesIO(data))
            return reader.read()

    except ImportError:
        decompress_zstd = None  # type: ignore[assignment]

DEFAULT_MODEL_SELECTION: ModelSelectionConfig = {
    "mode": "bridge_default",
    "model_id": "",
}

DEFAULT_SIDECAR_CONFIG: dict[str, Any] = {
    "panel": {
        "allowed_origins": ["https://app.promptsidecar.com"],
    },
    "bridges": {
        "codex": {
            "enabled": True,
            "adapter": "codex",
            "label": "Codex",
            "transport": "auto",
            "session_binding": "route",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 30000,
            "retry_count": 1,
            "home": str(CODEX_HOME),
        },
        "dsh": {
            "enabled": True,
            "adapter": "dsh",
            "label": "DSH",
            "transport": "http",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 30000,
            "retry_count": 1,
            "home": str(Path.home() / ".dsh"),
            "sessions_dir": str(Path.home() / ".dsh" / "sessions"),
            "workspace_storage": str(Path.home() / ".dsh" / "storages" / "workspace.json"),
            "bridge_url": "http://127.0.0.1:8766",
            "token_file": str(Path.home() / ".dsh" / "prompt-sidecar-bridge.token"),
        },
        "claude": {
            "enabled": True,
            "adapter": "claude",
            "label": "Claude Code",
            "transport": "cli",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 120000,
            "retry_count": 1,
            "home": str(Path.home() / ".claude"),
            "projects_dir": str(Path.home() / ".claude" / "projects"),
            "bin": "claude",
        },
        "workbuddy_local": {
            "enabled": True,
            "adapter": "workbuddy",
            "label": "WorkBuddy Local",
            "transport": "local",
            "surface": "local",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 120000,
            "retry_count": 1,
            "home": str(Path.home() / ".workbuddy"),
            "db_path": str(Path.home() / ".workbuddy" / "workbuddy.db"),
            "projects_dir": str(Path.home() / ".workbuddy" / "projects"),
            "script": "",
        },
        "openclaw": {
            "enabled": False,
            "adapter": "openclaw",
            "label": "OpenClaw",
            "transport": "websocket",
            "surface": "gateway",
            "endpoint": "ws://127.0.0.1:18789",
            "auth_ref": "OPENCLAW_GATEWAY_TOKEN",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 30000,
            "retry_count": 1,
        },
        "openhands": {
            "enabled": False,
            "adapter": "openhands",
            "label": "OpenHands",
            "transport": "http",
            "surface": "self_hosted",
            "endpoint": "http://127.0.0.1:3000",
            "auth_ref": "OPENHANDS_SESSION_API_KEY",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 30000,
            "retry_count": 1,
        },
        "cursor": {
            "enabled": False,
            "adapter": "cursor",
            "label": "Cursor Cloud",
            "transport": "http",
            "surface": "cloud",
            "endpoint": "https://api.cursor.com",
            "auth_ref": "CURSOR_API_KEY",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 60000,
            "retry_count": 1,
        },
        "copilot": {
            "enabled": False,
            "adapter": "copilot",
            "label": "GitHub Copilot Tasks",
            "transport": "http",
            "surface": "agent_tasks",
            "endpoint": "https://api.github.com",
            "auth_ref": "GITHUB_TOKEN",
            "session_binding": "explicit",
            "read_only": True,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 30000,
            "retry_count": 1,
        },
        "gemini": {
            "enabled": False,
            "adapter": "gemini",
            "label": "Gemini CLI",
            "transport": "cli",
            "surface": "cli",
            "home": str(Path.home() / ".gemini"),
            "sessions_dir": str(Path.home() / ".gemini" / "tmp"),
            "bin": "gemini",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 120000,
            "retry_count": 1,
        },
        "devin": {
            "enabled": False,
            "adapter": "devin",
            "label": "Devin",
            "transport": "http",
            "surface": "cloud",
            "endpoint": "https://api.devin.ai",
            "auth_ref": "DEVIN_API_KEY",
            "organization_id": "",
            "session_binding": "explicit",
            "read_only": False,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 60000,
            "retry_count": 1,
        },
        "doubao_work": {
            "enabled": False,
            "adapter": "doubao_mcp",
            "label": "豆包工作（只读 MCP）",
            "transport": "mcp",
            "surface": "mcp",
            "script": "",
            "session_binding": "explicit",
            "read_only": True,
            "model_selection": dict(DEFAULT_MODEL_SELECTION),
            "timeout_ms": 10000,
            "retry_count": 0,
        },
    }
}


def _deep_merge_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))  # cheap deep copy
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_sidecar_config() -> dict[str, Any]:
    """Read explicit settings without materializing default paths."""
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    return data if isinstance(data, dict) else {}


def _bridge_child_paths(adapter: str) -> dict[str, str]:
    return {
        "dsh": {
            "sessions_dir": "sessions",
            "workspace_storage": "storages/workspace.json",
            "token_file": "prompt-sidecar-bridge.token",
        },
        "claude": {"projects_dir": "projects"},
        "workbuddy": {"db_path": "workbuddy.db", "projects_dir": "projects"},
        "gemini": {"sessions_dir": "tmp"},
    }.get(adapter, {})


def _normalize_sidecar_paths(
    config: dict[str, Any], configured: dict[str, Any]
) -> dict[str, Any]:
    """Derive only omitted/empty child paths; explicit legacy paths always win."""
    config = json.loads(json.dumps(config))
    for source, bridge in config.get("bridges", {}).items():
        adapter = str(bridge.get("adapter") or source).strip()
        children = _bridge_child_paths(adapter)
        if children:
            defaults = next(
                value for value in DEFAULT_SIDECAR_CONFIG["bridges"].values()
                if value.get("adapter") == adapter
            )
            home = Path(bridge.get("home") or defaults["home"]).expanduser()
            bridge["home"] = str(home)
            explicit = configured.get("bridges", {}).get(source, {})
            for field, relative in children.items():
                bridge[field] = explicit.get(field) or str(home / relative)
        for key, value in list(bridge.items()):
            if key == "acp_cwd":
                workbuddy_acp_cwd(bridge)
                continue
            if isinstance(value, str) and value.startswith("~"):
                bridge[key] = str(Path(value).expanduser())
    return config


def load_sidecar_config() -> dict[str, Any]:
    data = _read_sidecar_config()
    return _normalize_sidecar_paths(_deep_merge_config(DEFAULT_SIDECAR_CONFIG, data), data)


def _normalized_origin(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    try:
        parsed.port
    except ValueError:
        return ""
    return text


def panel_allowed_origins() -> set[str]:
    config = load_sidecar_config().get("panel", {})
    configured = config.get("allowed_origins", []) if isinstance(config, dict) else []
    values = configured if isinstance(configured, list) else [configured]
    environment = os.environ.get("PROMPT_SIDECAR_ALLOWED_ORIGINS", "")
    values.extend(environment.split(","))
    return {
        origin
        for origin in (_normalized_origin(value) for value in values)
        if origin
    }


def _sidecar_config_for_storage(
    config: dict[str, Any], configured: dict[str, Any]
) -> dict[str, Any]:
    """Keep path overrides in their original form without freezing derived paths."""
    stored = json.loads(json.dumps(config))
    for source, bridge in stored.get("bridges", {}).items():
        children = _bridge_child_paths(str(bridge.get("adapter") or source).strip())
        if not children:
            continue
        explicit = configured.get("bridges", {}).get(source, {})
        for field in ("home", *children):
            if explicit.get(field):
                bridge[field] = explicit[field]
            else:
                bridge.pop(field, None)
    return stored


def request_correlation_ids(
    body: dict[str, Any],
    headers: Any,
) -> tuple[str, str]:
    """Return stable request and correlation identifiers for headless API clients."""
    request_id = str(body.get("request_id") or headers.get("X-Request-ID") or "").strip()
    request_id = request_id or uuid.uuid4().hex
    correlation_id = str(
        body.get("correlation_id") or headers.get("X-Correlation-ID") or request_id
    ).strip()
    return request_id, correlation_id or request_id


def polish_request_metadata(
    body: dict[str, Any],
    headers: Any,
) -> dict[str, str]:
    request_id, correlation_id = request_correlation_ids(body, headers)
    return {"request_id": request_id, "correlation_id": correlation_id}


def load_runtime_config() -> dict[str, str]:
    config_path = CODEX_HOME / "config.toml"
    data: dict[str, Any] = {}
    if config_path.exists():
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    provider_name = str(data.get("model_provider", "openai"))
    provider = data.get("model_providers", {}).get(provider_name, {})
    base_url = str(provider.get("base_url", "https://api.openai.com/v1"))
    model = str(data.get("model", "gpt-5.2"))
    token = str(provider.get("experimental_bearer_token", "")).strip()
    env_key = str(provider.get("env_key", "")).strip()
    if not token and env_key:
        token = os.environ.get(env_key, "").strip()
    if not token and base_url.startswith("https://api.openai.com"):
        token = os.environ.get("OPENAI_API_KEY", "").strip()
    return {"base_url": base_url.rstrip("/"), "model": model, "token": token}


def request_path_without_query(target: str) -> str:
    return urlparse(target).path or "/"


def timestamp_text(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def thread_row(row: tuple[Any, ...]) -> dict[str, Any]:
    thread_id, name, title, cwd, rollout_path, updated_at, recency_at, archived = row
    display_name = str(name or "").strip() or str(title or "").strip()
    return {
        "source": "codex",
        "session_id": str(thread_id),
        "title": display_name,
        "raw_title": str(title or ""),
        "cwd": str(cwd or ""),
        "rollout_path": str(rollout_path or ""),
        "updated_at": timestamp_text(updated_at),
        "recency_at": int(recency_at or 0),
        "archived": bool(archived),
    }


def query_thread(session_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    path = db_path or STATE_DB
    if not session_id or not path.exists():
        return None
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        row = connection.execute(
            "SELECT id, name, title, cwd, rollout_path, updated_at, recency_at, archived "
            "FROM threads WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        connection.close()
    return thread_row(row) if row else None


def list_recent_threads(limit: int = 50, db_path: Path | None = None) -> list[dict[str, Any]]:
    path = db_path or STATE_DB
    if not path.exists():
        return []
    uri = f"file:{path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT id, name, title, cwd, rollout_path, updated_at, recency_at, archived "
            "FROM threads WHERE archived = 0 "
            "ORDER BY recency_at DESC, updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [thread_row(row) for row in rows]


def select_context_messages(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, str]], str]:
    selected: list[dict[str, str]] = []
    total_chars = 0
    for message in reversed(messages[-MAX_MESSAGES * 2 :]):
        text = message["text"]
        if len(text) > 6_000:
            text = text[:6_000] + "\n[truncated]"
        role_label = {
            "user": "USER",
            "assistant": "ASSISTANT",
            "evidence": "TOOL_OR_VALIDATION",
        }.get(message["role"], message["role"].upper())
        block = f"{role_label}:\n{text}"
        if total_chars + len(block) > MAX_CONTEXT_CHARS and selected:
            break
        selected.append({"role": message["role"], "text": text})
        total_chars += len(block) + 2
    selected.reverse()
    context = "\n\n".join(
        f"{ {'user': 'USER', 'assistant': 'ASSISTANT', 'evidence': 'TOOL_OR_VALIDATION'}.get(item['role'], item['role'].upper())}:\n{item['text']}"
        for item in selected
    )
    return selected, context


def parse_dsh_log(path: Path) -> dict[str, Any]:
    """Parse one DSH session.v4.jsonl.zstd log into header/title/messages."""
    header: dict[str, Any] = {}
    title = ""
    raw_title = ""
    messages: list[dict[str, str]] = []
    last_time_ms = 0
    for line in decompress_zstd(path.read_bytes()).decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        event_type = str(record.get("type", ""))
        event_time = record.get("time")
        if isinstance(event_time, (int, float)):
            last_time_ms = max(last_time_ms, int(event_time))
        data = record.get("data")
        if event_type == "session":
            source_record = data if isinstance(data, dict) else record
            header = {
                "session_id": str(record.get("id") or source_record.get("id") or path.parent.name),
                "cwd": str(source_record.get("cwd", "")),
                "created_at": int(source_record.get("createdAt") or 0),
            }
            continue
        if event_type == "session/title" and isinstance(data, dict):
            raw_title = str(data.get("title", "")).strip()
            continue
        if event_type == "user/message" and isinstance(data, dict):
            text = strip_injected_blocks(
                message_text({"content": data.get("content", [])})
            )
            if not text:
                continue
            if messages and messages[-1] == {"role": "user", "text": text}:
                continue
            messages.append({"role": "user", "text": text})
            continue
        if event_type == "assistant/message" and isinstance(data, dict):
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            chunks = []
            for item in message.get("content", []):
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    chunks.append(item["text"].strip())
            text = strip_injected_blocks("\n".join(chunk for chunk in chunks if chunk))
            if not text:
                continue
            if messages and messages[-1] == {"role": "assistant", "text": text}:
                continue
            messages.append({"role": "assistant", "text": text})
    if not header.get("session_id"):
        header["session_id"] = path.parent.name
    if not title:
        title = raw_title
    if not title:
        first_user = next((m["text"] for m in messages if m["role"] == "user"), "")
        title = first_user[:30] or "未命名会话"
    return {
        "session_id": header.get("session_id", path.parent.name),
        "title": title,
        "raw_title": raw_title,
        "cwd": header.get("cwd", ""),
        "created_at": header.get("created_at", 0),
        "last_time_ms": last_time_ms or int(path.stat().st_mtime * 1000),
        "messages": messages,
    }


def dsh_workspace_slug(cwd: str) -> str:
    slug = cwd.replace("/", "-").strip("-")
    return f"--{slug}--"


def iter_dsh_session_dirs(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    found: list[Path] = []
    for workspace_dir in sessions_dir.iterdir():
        if not workspace_dir.is_dir():
            continue
        found.extend(child for child in workspace_dir.iterdir() if child.is_dir())
    return found


def dsh_archived_session_ids(workspace_storage: Path) -> set[str]:
    if not workspace_storage.exists():
        return set()
    try:
        data = json.loads(workspace_storage.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    archived = data.get("global", {}).get("archivedSessionIds", [])
    return {str(item) for item in archived if isinstance(item, str)}


def dsh_session_info(parsed: dict[str, Any], log_path: Path, archived: bool) -> dict[str, Any]:
    last_seconds = parsed["last_time_ms"] // 1000
    return {
        "source": "dsh",
        "session_id": parsed["session_id"],
        "title": parsed["title"],
        "raw_title": parsed["raw_title"],
        "cwd": parsed["cwd"],
        "log_path": str(log_path),
        "updated_at": timestamp_text(last_seconds),
        "recency_at": last_seconds,
        "archived": archived,
    }


def list_dsh_sessions(limit: int = 50, sessions_dir: Path | None = None, workspace_storage: Path | None = None) -> list[dict[str, Any]]:
    sessions_dir = sessions_dir or Path(DEFAULT_SIDECAR_CONFIG["bridges"]["dsh"]["sessions_dir"])
    workspace_storage = workspace_storage or Path(DEFAULT_SIDECAR_CONFIG["bridges"]["dsh"]["workspace_storage"])
    if decompress_zstd is None:
        return []
    candidates: list[tuple[float, Path]] = []
    for session_dir in iter_dsh_session_dirs(sessions_dir):
        log = session_dir / "session.v4.jsonl.zstd"
        if log.is_file():
            candidates.append((log.stat().st_mtime, log))
    candidates.sort(reverse=True)
    archived_ids = dsh_archived_session_ids(workspace_storage)
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _, log in candidates:
        if len(sessions) >= limit:
            break
        try:
            parsed = parse_dsh_log(log)
        except (OSError, ValueError, RuntimeError):
            continue
        session_id = parsed["session_id"]
        if session_id in seen or session_id in archived_ids:
            continue
        seen.add(session_id)
        sessions.append(dsh_session_info(parsed, log, session_id in archived_ids))
    sessions.sort(key=lambda item: item["recency_at"], reverse=True)
    return sessions


def find_dsh_log(session_id: str, sessions_dir: Path) -> Path | None:
    if not session_id or not sessions_dir.exists():
        return None
    for workspace_dir in sessions_dir.iterdir():
        if not workspace_dir.is_dir():
            continue
        candidate = workspace_dir / session_id / "session.v4.jsonl.zstd"
        if candidate.is_file():
            return candidate
    return None


def build_dsh_context_packet(
    session_id: str,
    sessions_dir: Path | None = None,
    workspace_storage: Path | None = None,
) -> dict[str, Any]:
    sessions_dir = sessions_dir or Path(DEFAULT_SIDECAR_CONFIG["bridges"]["dsh"]["sessions_dir"])
    workspace_storage = workspace_storage or Path(DEFAULT_SIDECAR_CONFIG["bridges"]["dsh"]["workspace_storage"])
    if decompress_zstd is None:
        return {
            "available": False,
            "requires_binding": False,
            "binding_method": "dsh-file-log",
            "source": "dsh",
            "session_id": session_id,
            "session_title": "",
            "cwd": "",
            "updated_at": "",
            "messages": [],
            "context": "",
            "char_count": 0,
            "message_count": 0,
            "transcript_path": "",
            "error": "Python 3.14+（或 zstandard 包）才能解压 DSH 会话日志；DSH 桥已禁用。",
        }
    log = find_dsh_log(session_id, sessions_dir)
    if log is None:
        return {
            "available": False,
            "requires_binding": False,
            "binding_method": "dsh-file-log",
            "source": "dsh",
            "session_id": session_id,
            "session_title": "",
            "cwd": "",
            "updated_at": "",
            "messages": [],
            "context": "",
            "char_count": 0,
            "message_count": 0,
            "transcript_path": "",
            "error": "DSH 会话日志不存在或已清理。",
        }
    parsed = parse_dsh_log(log)
    archived_ids = dsh_archived_session_ids(workspace_storage)
    info = dsh_session_info(parsed, log, parsed["session_id"] in archived_ids)
    selected, context = select_context_messages(parsed["messages"])
    return {
        "available": bool(context),
        "requires_binding": False,
        "binding_method": "dsh-file-log",
        "source": "dsh",
        "session_id": info["session_id"],
        "session_title": info["title"],
        "cwd": info["cwd"],
        "updated_at": info["updated_at"],
        "messages": selected,
        "context": context,
        "char_count": len(context),
        "message_count": len(selected),
        "transcript_path": str(log),
        "error": "",
    }


def read_bridge_token(token_file: Path) -> str:
    try:
        return token_file.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def send_dsh_prompt(
    session_id: str,
    message: str,
    mode: str = "queue",
    bridge_url: str = "http://127.0.0.1:8766",
    token_file: Path | None = None,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    token_file = token_file or Path(DEFAULT_SIDECAR_CONFIG["bridges"]["dsh"]["token_file"])
    token = read_bridge_token(token_file)
    if not token:
        raise RuntimeError(
            "未找到 DSH bridge token。请确认 prompt-sidecar-bridge 插件已安装且 DeepSeek Harness 已重启。"
        )
    if mode not in ALLOWED_SEND_MODES:
        raise ValueError("Unsupported send mode")
    payload = json.dumps(
        {"sessionId": session_id, "message": message, "mode": mode},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{bridge_url.rstrip('/')}/prompt",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=max(1, timeout_ms / 1000)) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        if exc.code == 401:
            raise RuntimeError("DSH bridge 校验 token 失败，请重启插件后重试。") from exc
        if exc.code == 404:
            raise RuntimeError(f"DSH 会话不存在：{session_id}") from exc
        raise RuntimeError(f"DSH bridge 返回 HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"无法连接 DSH bridge 插件（{bridge_url}）。请确认插件已安装且 DeepSeek Harness 正在运行。"
        ) from exc
    if not data.get("ok"):
        raise RuntimeError(str(data.get("error", "DSH bridge 拒绝了请求。")))
    return {"accepted": True, "mode": mode}


def probe_dsh_bridge(bridge_url: str = "http://127.0.0.1:8766") -> dict[str, Any]:
    try:
        with urlopen(f"{bridge_url.rstrip('/')}/health", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))
        return {"ok": bool(data.get("ok")), "version": str(data.get("version", ""))}
    except (HTTPError, URLError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


CONFIG_STRING_FIELDS = (
    "acp_cwd",
    "adapter",
    "label",
    "transport",
    "surface",
    "session_binding",
    "home",
    "sessions_dir",
    "workspace_storage",
    "projects_dir",
    "db_path",
    "script",
    "organization_id",
    "repo",
    "api_version",
    "bin",
    "endpoint",
    "auth_ref",
    "bridge_url",
    "token_file",
)
PUBLIC_BRIDGE_FIELDS = {
    "acp_cwd",
    "enabled",
    "adapter",
    "label",
    "transport",
    "surface",
    "session_binding",
    "read_only",
    "timeout_ms",
    "retry_count",
    "model_selection",
    "home",
    "sessions_dir",
    "workspace_storage",
    "projects_dir",
    "db_path",
    "script",
    "organization_id",
    "repo",
    "api_version",
    "bin",
    "endpoint",
    "auth_ref",
    "bridge_url",
    "token_file",
}


def public_bridge_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return only fields safe to expose to the local UI."""
    entry = {key: value for key, value in config.items() if key in PUBLIC_BRIDGE_FIELDS}
    endpoint = str(entry.get("endpoint") or "")
    if endpoint:
        try:
            parts = urlsplit(endpoint)
            safe_netloc = parts.hostname or ""
            if parts.port:
                safe_netloc += f":{parts.port}"
            entry["endpoint"] = urlunsplit((parts.scheme, safe_netloc, parts.path, "", ""))
        except ValueError:
            entry["endpoint"] = ""
    return entry


def bridge_timeout_seconds(config: dict[str, Any], default_ms: int = 30000) -> float:
    try:
        timeout_ms = int(config.get("timeout_ms") or default_ms)
    except (TypeError, ValueError):
        timeout_ms = default_ms
    return max(1.0, timeout_ms / 1000)


def bridge_retry_count(config: dict[str, Any], default: int = 1) -> int:
    try:
        retries = int(config.get("retry_count", default))
    except (TypeError, ValueError):
        retries = default
    return min(max(retries, 0), 5)


def bridge_token(config: dict[str, Any], default_ref: str = "") -> str:
    ref = str(config.get("auth_ref", default_ref) or "").strip()
    if not ref:
        return ""
    token = os.environ.get(ref, "").strip()
    if not token:
        raise BridgeAuthError(f"缺少环境变量 {ref}；请先配置桥接鉴权。")
    return token


def retry_sleep(attempt: int) -> None:
    time.sleep(min(0.25 * (2 ** attempt), 2.0))


class NoRequestRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        raise RequestError("redirect_rejected", "请求不允许重定向", 502)


def request_json_with_retry(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30,
    retry_count: int = 1,
    preserve_http_errors: bool = False,
    allow_redirects: bool = True,
) -> dict[str, Any]:
    """HTTP JSON helper with bounded retries and explicit auth failures."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    last_error: Exception | None = None
    for attempt in range(retry_count + 1):
        request = Request(url, data=body, method=method, headers=request_headers)
        try:
            opener = urlopen if allow_redirects else build_opener(NoRequestRedirect()).open
            with opener(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if preserve_http_errors:
                raise
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in {401, 403}:
                raise BridgeAuthError(f"桥接鉴权失败：HTTP {exc.code} {detail}") from exc
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"桥接请求失败：HTTP {exc.code} {detail}") from exc
            last_error = exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt < retry_count:
            retry_sleep(attempt)
    raise BridgeUnavailableError(f"桥接暂时不可用：{last_error}") from last_error


def classify_cli_failure(prefix: str, detail: str) -> RuntimeError:
    message = detail.strip()[:800] or "unknown error"
    if re.search(r"\b(401|403)\b|unauthori[sz]ed|authentication|not logged in|login required", message, re.I):
        return BridgeAuthError(f"{prefix}鉴权失败：{message}")
    return RuntimeError(f"{prefix}失败：{message}")


def strict_boolean(value: Any, source: str, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"bridge {source} {field} must be a boolean")
    return value


def strict_integer(value: Any, source: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"bridge {source} {field} must be an integer")
    return value


def sanitize_model_selection(value: Any, source: str) -> ModelSelectionConfig:
    if not isinstance(value, dict):
        raise ValueError(f"bridge {source} model_selection must be an object")
    mode = value.get("mode", "bridge_default")
    if mode not in {"bridge_default", "explicit"}:
        raise ValueError(
            f"bridge {source} model_selection.mode must be bridge_default or explicit"
        )
    model_id = value.get("model_id", "")
    if not isinstance(model_id, str):
        raise ValueError(f"bridge {source} model_selection.model_id must be a string")
    model_id = model_id.strip()
    if mode == "explicit" and not model_id:
        raise ValueError(f"bridge {source} model_selection.model_id is required")
    return {
        "mode": mode,
        "model_id": model_id if mode == "explicit" else "",
    }


def configured_model_selection(
    config: dict[str, Any],
    source: str,
) -> ModelSelectionConfig:
    if "model_selection" not in config:
        return dict(DEFAULT_MODEL_SELECTION)
    return sanitize_model_selection(config.get("model_selection"), source)


def validate_model_selection(source: str, bridge: SessionBridge) -> ModelSelectionConfig:
    selection = configured_model_selection(bridge.config, source)
    capability = bridge.capabilities().get("model_selection")
    if not isinstance(capability, dict):
        raise ValueError(f"bridge {source} does not declare model_selection capability")
    mode = capability.get("mode")
    if mode not in {"dynamic", "fixed", "unsupported"}:
        raise ValueError(f"bridge {source} model_selection capability is invalid")
    if selection["mode"] == "explicit":
        if mode != "dynamic":
            raise ValueError(f"bridge {source} does not support model override")
        if not capability.get("allow_custom", False):
            allowed = {
                str(option.get("id", ""))
                for option in capability.get("options", [])
                if isinstance(option, dict)
            }
            if selection["model_id"] not in allowed:
                raise ValueError(f"bridge {source} does not allow model {selection['model_id']}")
    return selection


def model_plan_for_send(bridge: SessionBridge, source: str) -> tuple[str | None, str]:
    selection = validate_model_selection(source, bridge)
    capability = bridge.capabilities()["model_selection"]
    if selection["mode"] == "explicit":
        model_id = selection["model_id"]
        return model_id, model_id
    effective = (
        str(capability.get("default_model_id") or "")
        or str(capability.get("fixed_model_id") or "")
    )
    return None, effective


def verified_session_model(bridge: SessionBridge, session_id: str) -> dict[str, str]:
    """Return only model evidence exposed by the selected session transcript."""
    if not session_id:
        return model_observation()
    try:
        packet = bridge.build_context_packet(session_id)
    except Exception:
        return model_observation()
    return model_observation(
        packet.get("model_id"),
        packet.get("model_source", ""),
        packet.get("model_timestamp", ""),
    )


def _sanitize_bridges(value: Any) -> dict[str, dict[str, Any]]:
    """Validate a client-supplied bridges mapping; empty strings mean 'use default'."""
    if not isinstance(value, dict):
        raise ValueError("bridges must be an object")
    cleaned: dict[str, dict[str, Any]] = {}
    for source, bridge in value.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError("bridge source names must be non-empty strings")
        if not isinstance(bridge, dict):
            raise ValueError(f"bridge {source} must be an object")
        entry: dict[str, Any] = {
            "enabled": strict_boolean(bridge.get("enabled", True), source, "enabled")
        }
        for field in CONFIG_STRING_FIELDS:
            if field not in bridge:
                continue
            text = str(bridge.get(field, "")).strip()
            if text or field == "auth_ref":
                entry[field] = text
        if "read_only" in bridge:
            entry["read_only"] = strict_boolean(bridge.get("read_only"), source, "read_only")
        if "model_selection" in bridge:
            entry["model_selection"] = sanitize_model_selection(
                bridge.get("model_selection"),
                source,
            )
        if "timeout_ms" in bridge:
            timeout_ms = strict_integer(bridge.get("timeout_ms"), source, "timeout_ms")
            if timeout_ms > 0:
                entry["timeout_ms"] = timeout_ms
        if "retry_count" in bridge:
            retry_count = strict_integer(bridge.get("retry_count"), source, "retry_count")
            if retry_count >= 0:
                entry["retry_count"] = retry_count
        cleaned[source.strip()] = entry
    return cleaned


def save_sidecar_config(
    bridges: Any,
) -> dict[str, Any]:
    cleaned = _sanitize_bridges(bridges)
    if not cleaned:
        raise ValueError("bridges must contain at least one entry")
    override: dict[str, Any] = {"bridges": cleaned}
    configured = _deep_merge_config(_read_sidecar_config(), override)
    merged = _deep_merge_config(load_sidecar_config(), override)
    for source, supplied in bridges.items():
        source = source.strip()
        adapter = str(merged["bridges"][source].get("adapter") or source).strip()
        for field in _bridge_child_paths(adapter):
            if isinstance(supplied.get(field), str) and not supplied[field].strip():
                configured["bridges"][source][field] = ""
    merged = _normalize_sidecar_paths(merged, configured)
    for source, bridge_config in merged["bridges"].items():
        bridge = BRIDGE_REGISTRY.create(source, bridge_config)
        validate_model_selection(source, bridge)
    try:
        CONFIG_PATH.write_text(
            json.dumps(
                _sidecar_config_for_storage(merged, configured), ensure_ascii=False, indent=4
            ) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"无法写入配置文件 {CONFIG_PATH}: {exc}") from exc
    return merged


def resolve_bridge(source: str | None = None) -> "SessionBridge":
    config = load_sidecar_config()
    bridges = config.get("bridges", {})
    wanted = (source or "codex").strip()
    bridge_config = bridges.get(wanted)
    if not isinstance(bridge_config, dict) or not bridge_config.get("enabled"):
        raise ValueError(f"未启用或未知的会话来源：{wanted or '(空)'}")
    bridge = BRIDGE_REGISTRY.create(wanted, bridge_config)
    validate_model_selection(wanted, bridge)
    return bridge


class CodexBridge(SessionBridge):
    source = "codex"
    adapter = "codex"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.home = Path(config.get("home") or CODEX_HOME)
        self.transport = str(config.get("transport") or "auto").strip().lower()

    def capabilities(self) -> BridgeCapabilities:
        can_send = not bool(self.config.get("read_only", False))
        return {
            "list_sessions": True,
            "read_context": True,
            "send": can_send,
            "send_modes": ["queue"] if can_send else [],
            "context_mode": "full",
            "model_selection": fixed_model_selection(
                "current-thread",
                "当前会话模型",
                "Codex queue 固定使用当前线程的模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        if not CODEX_BIN:
            return {
                "available": False,
                "status": "unavailable",
                "message": "Codex executable not found.",
            }
        mode = "App Server list/read + CLI queue" if self.transport != "legacy" else "CLI legacy"
        return {"available": True, "status": "ready", "message": mode}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        if self.transport != "legacy":
            try:
                return retag_sessions(
                    list_codex_app_server_threads(limit, codex_home=self.home),
                    self.source,
                )
            except (OSError, RuntimeError, ValueError):
                pass
        return retag_sessions(
            list_recent_threads(limit, db_path=self.home / "state_5.sqlite"),
            self.source,
        )

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        packet = build_context_packet(
            session_id=session_id,
            db_path=self.home / "state_5.sqlite",
            global_state_path=self.home / ".codex-global-state.json",
        )
        return retag_context(packet, self.source)

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.config.get("read_only"):
            raise ValueError("当前 Codex 桥为只读模式。")
        if mode == "steer":
            raise ValueError("Codex 桥只支持 queue 发送。")
        if model_id:
            raise ValueError("Codex queue 不支持覆盖模型；请使用当前线程模型。")
        result = send_queued_message(
            session_id,
            message,
            db_path=self.home / "state_5.sqlite",
            codex_home=self.home,
            timeout_ms=int(self.config.get("timeout_ms") or 30000),
        )
        result["effective_model"] = "current-thread"
        result["accepted"] = True
        result["mode"] = "queue"
        return result


class DshBridge(SessionBridge):
    source = "dsh"
    adapter = "dsh"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.config = config
        self.sessions_dir = Path(config.get("sessions_dir") or Path.home() / ".dsh" / "sessions")
        self.workspace_storage = Path(
            config.get("workspace_storage") or Path.home() / ".dsh" / "storages" / "workspace.json"
        )

    def capabilities(self) -> BridgeCapabilities:
        can_send = not bool(self.config.get("read_only", False))
        return {
            "list_sessions": True,
            "read_context": True,
            "send": can_send,
            "send_modes": ["queue", "steer"] if can_send else [],
            "context_mode": "full",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        probe = probe_dsh_bridge(str(self.config.get("bridge_url") or "http://127.0.0.1:8766"))
        if probe.get("ok"):
            return {"available": True, "status": "ready", "message": str(probe.get("version") or "")}
        return {
            "available": False,
            "status": "unavailable",
            "message": str(probe.get("error") or "DSH bridge plugin is not running."),
        }

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        return retag_sessions(
            list_dsh_sessions(
                limit,
                sessions_dir=self.sessions_dir,
                workspace_storage=self.workspace_storage,
            ),
            self.source,
        )

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(
                self.source,
                "请从会话列表中选择一个 DSH 会话。",
                requires_binding=True,
            )
        return retag_context(
            build_dsh_context_packet(
                session_id,
                sessions_dir=self.sessions_dir,
                workspace_storage=self.workspace_storage,
            ),
            self.source,
        )

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.config.get("read_only"):
            raise ValueError("当前 DSH 桥为只读模式。")
        result = send_dsh_prompt(
            session_id,
            message,
            mode=mode,
            bridge_url=str(self.config.get("bridge_url") or "http://127.0.0.1:8766"),
            token_file=Path(self.config.get("token_file") or Path.home() / ".dsh" / "prompt-sidecar-bridge.token"),
            timeout_ms=int(self.config.get("timeout_ms") or 30000),
        )
        result["effective_model"] = "session-default"
        return result


class ClaudeBridge(SessionBridge):
    source = "claude"
    adapter = "claude"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.home = Path(config.get("home") or Path.home() / ".claude")
        self.projects_dir = Path(config.get("projects_dir") or self.home / "projects")
        self.bin = str(config.get("bin") or "claude")
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue"],
            "context_mode": "full",
            "model_selection": dynamic_model_selection(
                default_model_id="session-default",
                options=[
                    {"id": "fable", "label": "Fable"},
                    {"id": "opus", "label": "Opus"},
                    {"id": "sonnet", "label": "Sonnet"},
                ],
                allow_custom=True,
                reason="Claude Code CLI 支持 --model；留空时跟随会话默认模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        binary = (
            shutil.which(self.bin)
            if not Path(self.bin).is_absolute()
            else Path(self.bin).exists()
        )
        if not binary:
            return {
                "available": False,
                "status": "unavailable",
                "message": "Claude executable not found.",
            }
        if not self.projects_dir.exists():
            return {
                "available": False,
                "status": "unavailable",
                "message": "Claude session directory not found.",
            }
        return {
            "available": True,
            "status": "ready",
            "message": "Local Claude Code sessions; queue send is enabled." if not self.read_only else "Local Claude Code sessions; read-only mode.",
        }

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        return retag_sessions(
            list_claude_sessions(limit, projects_dir=self.projects_dir),
            self.source,
        )

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(
                self.source,
                "请从会话列表中选择一个 Claude Code 会话。",
                requires_binding=True,
            )
        return retag_context(
            build_claude_context_packet(session_id, projects_dir=self.projects_dir),
            self.source,
        )

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 Claude Code 桥为只读模式。")
        if mode != "queue":
            raise ValueError("Claude Code 桥只支持 queue 发送。")
        result = send_claude_message(
            session_id,
            message,
            projects_dir=self.projects_dir,
            binary=self.bin,
            timeout_ms=int(self.config.get("timeout_ms") or 120000),
            model_id=model_id,
        )
        result["effective_model"] = model_id or "session-default"
        return result


class WorkBuddyBridge(SessionBridge):
    source = "workbuddy_cloud"
    adapter = "workbuddy"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.endpoint = str(config.get("endpoint") or "https://www.workbuddy.cn").rstrip("/")
        self.auth_ref = str(config.get("auth_ref") or "WORKBUDDY_ACCESS_TOKEN").strip()
        self.surface = str(config.get("surface") or "cloud_task").strip()
        self.read_only = bool(config.get("read_only", True))
        self.timeout_ms = int(config.get("timeout_ms") or 120000)
        self.home = Path(config.get("home") or Path.home() / ".workbuddy")
        self.db_path = Path(config.get("db_path") or self.home / "workbuddy.db")
        self.projects_dir = Path(config.get("projects_dir") or self.home / "projects")

    def capabilities(self) -> BridgeCapabilities:
        if self.surface == "local":
            return {
                "list_sessions": True,
                "read_context": True,
                "send": not self.read_only,
                "send_modes": [] if self.read_only else ["queue"],
                "context_mode": "full",
                "model_selection": fixed_model_selection(
                    "session-default",
                    "会话默认模型",
                    "当前桥接只能跟随会话模型。",
                ),
            }
        return {
            "list_sessions": self.surface == "cloud_task",
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue"],
            "context_mode": "best_effort",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        if self.surface == "local":
            if not self.db_path.exists():
                return {
                    "available": False,
                    "status": "unavailable",
                    "message": "WorkBuddy 本地数据库不存在。",
                }
            if not workbuddy_cli_command(self.config):
                return {
                    "available": False,
                    "status": "unavailable",
                    "message": "未找到 WorkBuddy 本地 CLI。",
                }
            return {
                "available": True,
                "status": "ready",
                "message": "WorkBuddy 本地数据库与 CLI；不需要云端 token。",
            }
        if self.surface != "cloud_task":
            return {
                "available": False,
                "status": "unverified",
                "message": "当前仅验证 WorkBuddy Cloud task 适配器。",
            }
        if not self.auth_ref or not os.environ.get(self.auth_ref, "").strip():
            return {
                "available": False,
                "status": "auth_missing",
                "message": (
                    f"请在环境变量 {self.auth_ref or '(空)'} 中提供 WorkBuddy access token。"
                ),
            }
        return {
            "available": None,
            "status": "configured",
            "message": "Token 已配置；Open API/ACP 尚未在当前视图中探测。",
        }

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        if self.surface == "local":
            return retag_sessions(
                list_workbuddy_local_sessions(self.db_path, self.projects_dir, limit=limit),
                self.source,
            )
        if self.surface != "cloud_task":
            raise ValueError("当前只启用了 WorkBuddy cloud_task 适配器。")
        return retag_sessions(
            list_workbuddy_tasks(self.config, limit=limit),
            self.source,
        )

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(
                self.source,
                "请从会话列表中选择一个 WorkBuddy 云端任务。",
                requires_binding=True,
            )
        if self.surface == "local":
            return retag_context(
                build_workbuddy_local_context_packet(self.config, session_id),
                self.source,
            )
        return retag_context(
            build_workbuddy_context_packet(self.config, session_id),
            self.source,
        )

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 WorkBuddy 桥为只读模式。")
        if mode != "queue":
            raise ValueError("WorkBuddy 桥只支持 queue 发送。")
        if self.surface == "local":
            result = send_workbuddy_local_prompt(self.config, session_id, message)
        else:
            result = send_workbuddy_prompt(self.config, session_id, message)
        result["effective_model"] = "session-default"
        return result


def parse_bridge_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        number = int(value)
        return number // 1000 if number > 10_000_000_000 else number
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def extract_message_text(value: Any) -> str:
    if isinstance(value, str):
        return strip_injected_blocks(value)
    if isinstance(value, list):
        chunks = [extract_message_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "body"):
            if key in value:
                text = extract_message_text(value[key])
                if text:
                    return text
    return ""


def openclaw_rpc(
    config: dict[str, Any],
    method: str,
    params: dict[str, Any],
    *,
    timeout_seconds: float | None = None,
    retry_count: int | None = None,
) -> dict[str, Any]:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise BridgeUnavailableError("缺少 websockets 依赖；请安装 websockets。") from exc
    endpoint = str(config.get("endpoint") or "ws://127.0.0.1:18789").strip()
    if not endpoint:
        raise BridgeUnavailableError("未配置 OpenClaw Gateway endpoint。")
    token = bridge_token(config, "OPENCLAW_GATEWAY_TOKEN")
    timeout = timeout_seconds or bridge_timeout_seconds(config)
    retries = bridge_retry_count(config) if retry_count is None else max(0, retry_count)
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with connect(endpoint, open_timeout=timeout, close_timeout=timeout, max_size=None) as ws:
                challenge = None
                try:
                    first = json.loads(ws.recv(timeout=min(0.5, timeout)))
                    if isinstance(first, dict) and first.get("event") == "connect.challenge":
                        challenge = first.get("payload") or {}
                except TimeoutError:
                    pass
                connect_id = f"connect-{uuid.uuid4().hex}"
                auth_params: dict[str, Any] = {}
                if token:
                    auth_params["token"] = token
                connect_params: dict[str, Any] = {
                    "minProtocol": 4,
                    "maxProtocol": 4,
                    "client": {
                        "id": "prompt-sidecar",
                        "version": "0.4.0",
                        "platform": "macos",
                        "mode": "operator",
                    },
                    "role": "operator",
                    "scopes": ["operator.read", "operator.write"],
                    "auth": auth_params,
                    "locale": "zh-CN",
                    "userAgent": "prompt-sidecar/0.4.0",
                }
                if isinstance(challenge, dict) and challenge.get("nonce"):
                    connect_params["challenge"] = challenge.get("nonce")
                ws.send(json.dumps({"type": "req", "id": connect_id, "method": "connect", "params": connect_params}))
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    response = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                    if response.get("type") != "res" or response.get("id") != connect_id:
                        continue
                    if not response.get("ok"):
                        error = response.get("error") or {}
                        message = str(error.get("message") or error.get("code") or "Gateway 拒绝连接")
                        if re.search(r"auth|unauthori|pair|forbidden|scope", message, re.I):
                            raise BridgeAuthError(f"OpenClaw 鉴权失败：{message}")
                        raise BridgeUnavailableError(f"OpenClaw 连接失败：{message}")
                    request_id = f"rpc-{uuid.uuid4().hex}"
                    ws.send(json.dumps({"type": "req", "id": request_id, "method": method, "params": params}))
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        payload = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                        if payload.get("type") != "res" or payload.get("id") != request_id:
                            continue
                        if not payload.get("ok"):
                            error = payload.get("error") or {}
                            message = str(error.get("message") or error.get("code") or "RPC 失败")
                            if re.search(r"auth|unauthori|forbidden|scope", message, re.I):
                                raise BridgeAuthError(f"OpenClaw 鉴权失败：{message}")
                            raise RuntimeError(f"OpenClaw RPC 失败：{message}")
                        result = payload.get("payload") or {}
                        return result if isinstance(result, dict) else {"value": result}
                raise BridgeUnavailableError("OpenClaw RPC 等待响应超时。")
        except (BridgeAuthError,):
            raise
        except Exception as exc:  # websocket libraries expose several transport exceptions
            last_error = exc
            text = str(exc)
            if re.search(r"401|403|unauthori|forbidden|auth", text, re.I):
                raise BridgeAuthError(f"OpenClaw 鉴权失败：{text[:500]}") from exc
            if attempt < retries:
                retry_sleep(attempt)
    raise BridgeUnavailableError(f"OpenClaw Gateway 不可用：{last_error}") from last_error


def openclaw_session_row(row: dict[str, Any]) -> dict[str, Any]:
    session_key = str(row.get("sessionKey") or row.get("key") or row.get("sessionId") or row.get("id") or "")
    updated = parse_bridge_timestamp(row.get("recencyAt") or row.get("updatedAt") or row.get("lastActivityAt"))
    return {
        "source": "openclaw",
        "session_id": session_key,
        "native_session_id": str(row.get("sessionId") or row.get("id") or ""),
        "session_key": session_key,
        "title": str(row.get("displayName") or row.get("name") or row.get("title") or "OpenClaw 会话"),
        "cwd": str(row.get("cwd") or row.get("workspace") or row.get("workspaceDir") or ""),
        "updated_at": timestamp_text(updated),
        "recency_at": updated,
        "archived": bool(row.get("archived")),
        "status": str(row.get("status") or ""),
    }


def openclaw_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("sessions", "items", "messages", "data", "events"):
            if isinstance(value.get(key), list):
                return [item for item in value[key] if isinstance(item, dict)]
    return []


def openclaw_context_messages(value: Any) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in openclaw_items(value):
        role = str(item.get("role") or item.get("source") or "").lower()
        if role in {"agent", "assistant", "bot"}:
            role = "assistant"
        elif role in {"human", "user"}:
            role = "user"
        else:
            continue
        text = extract_message_text(item.get("message") or item.get("content") or item.get("body") or item)
        if text:
            messages.append({"role": role, "text": text})
    return messages


class OpenClawBridge(SessionBridge):
    source = "openclaw"
    adapter = "openclaw"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue", "steer"],
            "context_mode": "full",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        try:
            bridge_token(self.config, "OPENCLAW_GATEWAY_TOKEN")
            openclaw_rpc(
                self.config,
                "sessions.list",
                {"limit": 1},
                timeout_seconds=min(2.0, bridge_timeout_seconds(self.config)),
                retry_count=0,
            )
            return {"available": True, "status": "ready", "message": "OpenClaw Gateway 已连接。"}
        except BridgeAuthError as exc:
            return {"available": False, "status": "auth_missing", "message": str(exc)}
        except Exception as exc:
            return {"available": False, "status": "unavailable", "message": str(exc)}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        result = openclaw_rpc(self.config, "sessions.list", {"limit": limit, "archived": False})
        return retag_sessions([openclaw_session_row(item) for item in openclaw_items(result)], self.source)

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 OpenClaw 会话。", requires_binding=True)
        result = openclaw_rpc(
            self.config,
            "chat.history",
            {"sessionKey": session_id, "limit": MAX_MESSAGES},
        )
        selected, context = select_context_messages(openclaw_context_messages(result))
        return {
            "available": bool(context),
            "requires_binding": False,
            "binding_method": "openclaw-chat-history",
            "source": self.source,
            "session_id": session_id,
            "session_title": session_id,
            "cwd": "",
            "updated_at": "",
            "messages": selected,
            "context": context,
            "char_count": len(context),
            "message_count": len(selected),
            "transcript_path": "",
            "error": "",
        }

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 OpenClaw 桥为只读模式。")
        if mode not in {"queue", "steer"}:
            raise ValueError("OpenClaw 只支持 queue 或 steer。")
        queue_mode = "steer" if mode == "steer" else "followup"
        result = openclaw_rpc(
            self.config,
            "chat.send",
            {
                "sessionKey": session_id,
                "message": message,
                "idempotencyKey": uuid.uuid4().hex,
                "queueMode": queue_mode,
                "timeoutMs": int(self.config.get("timeout_ms") or 30000),
            },
        )
        return {
            "accepted": True,
            "mode": mode,
            "run_id": result.get("runId", ""),
            "result": result,
            "effective_model": "session-default",
        }


def openhands_headers(config: dict[str, Any]) -> dict[str, str]:
    token = bridge_token(config, "OPENHANDS_SESSION_API_KEY")
    return {"X-Session-API-Key": token} if token else {}


def openhands_request(
    config: dict[str, Any],
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    retry_count: int | None = None,
) -> dict[str, Any]:
    endpoint = str(config.get("endpoint") or "http://127.0.0.1:3000").rstrip("/")
    return request_json_with_retry(
        f"{endpoint}{path}",
        method=method,
        headers=openhands_headers(config),
        payload=payload,
        timeout_seconds=bridge_timeout_seconds(config),
        retry_count=bridge_retry_count(config) if retry_count is None else max(0, retry_count),
    )


def openhands_session_row(row: dict[str, Any]) -> dict[str, Any]:
    workspace = row.get("workspace") if isinstance(row.get("workspace"), dict) else {}
    updated = parse_bridge_timestamp(row.get("updated_at") or row.get("updatedAt") or row.get("created_at"))
    return {
        "source": "openhands",
        "session_id": str(row.get("id") or ""),
        "title": str(row.get("title") or row.get("name") or "OpenHands 会话"),
        "cwd": str(workspace.get("working_dir") or workspace.get("root") or workspace.get("path") or ""),
        "updated_at": timestamp_text(updated),
        "recency_at": updated,
        "status": str(row.get("execution_status") or row.get("status") or ""),
        "archived": False,
    }


def openhands_context_messages(value: Any) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in openclaw_items(value):
        kind = str(item.get("kind") or item.get("type") or "")
        if "Message" not in kind and "message" not in kind.lower():
            continue
        role = str(item.get("role") or item.get("source") or "").lower()
        if role in {"agent", "assistant"}:
            role = "assistant"
        elif role in {"user", "human"}:
            role = "user"
        else:
            continue
        text = extract_message_text(item.get("content") or item.get("message") or item)
        if text:
            messages.append({"role": role, "text": text})
    messages.reverse()
    return messages


class OpenHandsBridge(SessionBridge):
    source = "openhands"
    adapter = "openhands"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue"],
            "context_mode": "full",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        try:
            result = openhands_request(self.config, "GET", "/api/conversations/search?limit=1")
            items = result.get("items", []) if isinstance(result, dict) else result
            return {"available": True, "status": "ready", "message": f"OpenHands 已连接，{len(items or [])} 个会话。"}
        except BridgeAuthError as exc:
            return {"available": False, "status": "auth_missing", "message": str(exc)}
        except Exception as exc:
            return {"available": False, "status": "unavailable", "message": str(exc)}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        result = openhands_request(self.config, "GET", f"/api/conversations/search?limit={min(max(limit, 1), 100)}&sort_order=UPDATED_AT_DESC")
        items = result.get("items", []) if isinstance(result, dict) else result
        return retag_sessions(
            [openhands_session_row(item) for item in items or [] if isinstance(item, dict)],
            self.source,
        )

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 OpenHands 会话。", requires_binding=True)
        result = openhands_request(
            self.config,
            "GET",
            f"/api/conversations/{session_id}/events/search?limit={MAX_MESSAGES}&sort_order=TIMESTAMP_DESC",
        )
        selected, context = select_context_messages(openhands_context_messages(result))
        return {
            "available": bool(context),
            "requires_binding": False,
            "binding_method": "openhands-events",
            "source": self.source,
            "session_id": session_id,
            "session_title": session_id,
            "cwd": "",
            "updated_at": "",
            "messages": selected,
            "context": context,
            "char_count": len(context),
            "message_count": len(selected),
            "transcript_path": "",
            "error": "",
        }

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 OpenHands 桥为只读模式。")
        if mode != "queue":
            raise ValueError("OpenHands 只支持 queue。")
        result = openhands_request(
            self.config,
            "POST",
            f"/api/conversations/{session_id}/events",
            payload={"role": "user", "content": [{"type": "text", "text": message}], "run": True},
            retry_count=0,
        )
        return {
            "accepted": bool(result.get("success", True)),
            "mode": "queue",
            "result": result,
            "effective_model": "session-default",
        }


def bearer_headers(config: dict[str, Any], default_ref: str) -> dict[str, str]:
    token = bridge_token(config, default_ref)
    return {"Authorization": f"Bearer {token}"} if token else {}


def cursor_request(
    config: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    retry_count: int | None = None,
) -> dict[str, Any]:
    token = bridge_token(config, "CURSOR_API_KEY")
    encoded = base64.b64encode(f"{token}:".encode("utf-8")).decode("ascii")
    endpoint = str(config.get("endpoint") or "https://api.cursor.com").rstrip("/")
    return request_json_with_retry(
        f"{endpoint}{path}",
        method=method,
        headers={"Authorization": f"Basic {encoded}"},
        payload=payload,
        timeout_seconds=bridge_timeout_seconds(config, 60000),
        retry_count=bridge_retry_count(config) if retry_count is None else max(0, retry_count),
    )


def cursor_session_row(row: dict[str, Any]) -> dict[str, Any]:
    updated = parse_bridge_timestamp(row.get("updatedAt") or row.get("createdAt"))
    return {
        "source": "cursor",
        "session_id": str(row.get("id") or ""),
        "title": str(row.get("name") or "Cursor Cloud Agent"),
        "cwd": "",
        "updated_at": timestamp_text(updated),
        "recency_at": updated,
        "status": str(row.get("status") or ""),
        "url": str(row.get("url") or ""),
        "latest_run_id": str(row.get("latestRunId") or ""),
        "archived": str(row.get("status") or "").upper() == "ARCHIVED",
    }


class CursorBridge(SessionBridge):
    source = "cursor"
    adapter = "cursor"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue", "steer"],
            "context_mode": "best_effort",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        try:
            result = cursor_request(self.config, "GET", "/v1/agents?limit=1")
            return {"available": True, "status": "ready", "message": f"Cursor Cloud Agents; {len(result.get('items', []))} 个 agent。"}
        except BridgeAuthError as exc:
            return {"available": False, "status": "auth_missing", "message": str(exc)}
        except Exception as exc:
            return {"available": False, "status": "unavailable", "message": str(exc)}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        result = cursor_request(
            self.config,
            "GET",
            f"/v1/agents?limit={min(max(limit, 1), 100)}&includeArchived=false",
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        return retag_sessions([cursor_session_row(item) for item in items if isinstance(item, dict)], self.source)

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 Cursor agent。", requires_binding=True)
        runs = cursor_request(self.config, "GET", f"/v1/agents/{session_id}/runs?limit=8")
        items = runs.get("items", []) if isinstance(runs, dict) else runs if isinstance(runs, list) else []
        messages: list[dict[str, str]] = []
        for item in reversed(items[:8]):
            run_id = item.get("id") if isinstance(item, dict) else None
            if not run_id:
                continue
            try:
                detail = cursor_request(self.config, "GET", f"/v1/agents/{session_id}/runs/{run_id}")
            except Exception:
                continue
            result_text = str(detail.get("result") or "").strip()
            if result_text:
                messages.append({"role": "assistant", "text": result_text})
        selected, context = select_context_messages(messages)
        return {
            "available": bool(context),
            "requires_binding": False,
            "binding_method": "cursor-cloud-runs",
            "source": self.source,
            "session_id": session_id,
            "session_title": session_id,
            "cwd": "",
            "updated_at": "",
            "messages": selected,
            "context": context,
            "char_count": len(context),
            "message_count": len(selected),
            "transcript_path": "",
            "error": "" if context else "Cursor API 未返回可读取的历史 run 结果。",
        }

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 Cursor 桥为只读模式。")
        if mode not in {"queue", "steer"}:
            raise ValueError("Cursor 只支持 queue 或 steer。")
        payload: dict[str, Any] = {"prompt": {"text": message}}
        if mode == "steer":
            payload["delivery"] = "steer"
        result = cursor_request(
            self.config,
            "POST",
            f"/v1/agents/{session_id}/runs",
            payload,
            retry_count=0,
        )
        run = result.get("run") if isinstance(result, dict) else {}
        return {
            "accepted": True,
            "mode": mode,
            "run_id": str((run or {}).get("id") or result.get("runId") or ""),
            "delivery": str(result.get("delivery") or ""),
            "result": result,
            "effective_model": "session-default",
        }


def copilot_repo_from_task(task: dict[str, Any]) -> str:
    repository = task.get("repository") if isinstance(task.get("repository"), dict) else {}
    full_name = repository.get("full_name")
    if full_name:
        return str(full_name)
    for key in ("html_url", "url"):
        value = str(task.get(key) or "")
        match = re.search(r"github\.com/([^/]+/[^/]+)", value)
        if match:
            return match.group(1).removesuffix(".git")
        match = re.search(r"/repos/([^/]+/[^/]+)/", value)
        if match:
            return match.group(1)
    return ""


def copilot_task_session(task: dict[str, Any]) -> dict[str, Any]:
    task_id = str(task.get("id") or "")
    repo = copilot_repo_from_task(task)
    updated = parse_bridge_timestamp(task.get("updated_at") or task.get("created_at"))
    return {
        "source": "copilot",
        "session_id": f"{repo}#{task_id}" if repo else task_id,
        "native_session_id": task_id,
        "repo": repo,
        "title": str(task.get("name") or task.get("html_url") or "Copilot Agent Task"),
        "cwd": "",
        "updated_at": timestamp_text(updated),
        "recency_at": updated,
        "status": str(task.get("state") or ""),
        "archived": bool(task.get("archived_at")),
    }


class CopilotBridge(SessionBridge):
    source = "copilot"
    adapter = "copilot"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": False,
            "send_modes": [],
            "context_mode": "metadata",
            "model_selection": unsupported_model_selection(
                "Copilot Task 当前只读，不支持模型选择。",
            ),
        }

    def health(self) -> BridgeHealth:
        try:
            endpoint = str(self.config.get("endpoint") or "https://api.github.com").rstrip("/")
            api_version = str(self.config.get("api_version") or "2026-03-10")
            data = request_json_with_retry(
                f"{endpoint}/agents/tasks?per_page=1",
                headers={**bearer_headers(self.config, "GITHUB_TOKEN"), "X-GitHub-Api-Version": api_version},
                timeout_seconds=bridge_timeout_seconds(self.config),
                retry_count=bridge_retry_count(self.config),
            )
            return {"available": True, "status": "ready", "message": f"Copilot tasks; {len(data.get('tasks', []))} 个任务。"}
        except BridgeAuthError as exc:
            return {"available": False, "status": "auth_missing", "message": str(exc)}
        except Exception as exc:
            return {"available": False, "status": "unavailable", "message": str(exc)}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        endpoint = str(self.config.get("endpoint") or "https://api.github.com").rstrip("/")
        api_version = str(self.config.get("api_version") or "2026-03-10")
        result = request_json_with_retry(
            f"{endpoint}/agents/tasks?per_page={min(max(limit, 1), 100)}&sort=updated_at&direction=desc",
            headers={**bearer_headers(self.config, "GITHUB_TOKEN"), "X-GitHub-Api-Version": api_version},
            timeout_seconds=bridge_timeout_seconds(self.config),
            retry_count=bridge_retry_count(self.config),
        )
        tasks = result.get("tasks", []) if isinstance(result, dict) else []
        return retag_sessions([copilot_task_session(item) for item in tasks if isinstance(item, dict)], self.source)

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 Copilot task。", requires_binding=True)
        repo, _, task_id = session_id.rpartition("#")
        if not repo or not task_id:
            return empty_context_packet(self.source, "Copilot task 缺少 repository 信息。")
        endpoint = str(self.config.get("endpoint") or "https://api.github.com").rstrip("/")
        api_version = str(self.config.get("api_version") or "2026-03-10")
        task = request_json_with_retry(
            f"{endpoint}/agents/repos/{repo}/tasks/{task_id}",
            headers={**bearer_headers(self.config, "GITHUB_TOKEN"), "X-GitHub-Api-Version": api_version},
            timeout_seconds=bridge_timeout_seconds(self.config),
            retry_count=bridge_retry_count(self.config),
        )
        text = (
            f"TASK: {task.get('name') or task_id}\n"
            f"STATE: {task.get('state') or ''}\n"
            f"REPO: {repo}\n"
            f"SESSION_COUNT: {task.get('session_count') or 0}"
        )
        return {
            "available": True,
            "requires_binding": False,
            "binding_method": "copilot-task-metadata",
            "source": self.source,
            "session_id": session_id,
            "session_title": str(task.get("name") or session_id),
            "cwd": "",
            "updated_at": str(task.get("updated_at") or ""),
            "messages": [{"role": "assistant", "text": text}],
            "context": text,
            "char_count": len(text),
            "message_count": 1,
            "transcript_path": "",
            "error": "Copilot task API 未公开会话消息历史；当前仅提供任务元数据。",
        }

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        raise ValueError("Copilot task API 不提供向已有任务注入消息的公开能力。")


def gemini_session_files(sessions_dir: Path) -> list[Path]:
    if not sessions_dir.exists():
        return []
    return [
        path for path in sessions_dir.rglob("*.json")
        if path.is_file() and "chats" in path.parts
    ]


def read_gemini_session(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    records: list[Any]
    try:
        loaded = json.loads(raw)
        records = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        records = []
        for line in raw.splitlines():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    messages: list[dict[str, str]] = []
    title = ""
    cwd = ""
    for record in records:
        if not isinstance(record, dict):
            continue
        title = str(record.get("title") or title)
        cwd = str(record.get("cwd") or cwd)
        nested = record.get("messages") or record.get("history") or record.get("conversation")
        if isinstance(nested, list):
            for item in nested:
                if isinstance(item, dict):
                    role = str(item.get("role") or item.get("type") or "").lower()
                    if role in {"model", "assistant", "gemini"}:
                        role = "assistant"
                    elif role in {"user", "human"}:
                        role = "user"
                    else:
                        continue
                    text = extract_message_text(item.get("content") or item.get("text") or item)
                    if text:
                        messages.append({"role": role, "text": text})
    return messages, {"session_id": path.stem, "title": title, "cwd": cwd}


class GeminiBridge(SessionBridge):
    source = "gemini"
    adapter = "gemini"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.home = Path(config.get("home") or Path.home() / ".gemini")
        self.sessions_dir = Path(config.get("sessions_dir") or self.home / "tmp")
        self.bin = str(config.get("bin") or "gemini")
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": True,
            "send": not self.read_only,
            "send_modes": [] if self.read_only else ["queue"],
            "context_mode": "full",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        if not shutil.which(self.bin):
            return {"available": False, "status": "unavailable", "message": "Gemini CLI 未安装。"}
        if not self.sessions_dir.exists():
            return {"available": False, "status": "unavailable", "message": "Gemini 会话目录不存在。"}
        return {"available": True, "status": "ready", "message": "Gemini CLI sessions ready."}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        files = sorted(gemini_session_files(self.sessions_dir), key=lambda path: path.stat().st_mtime, reverse=True)
        sessions: list[dict[str, Any]] = []
        for path in files:
            try:
                messages, meta = read_gemini_session(path)
            except OSError:
                continue
            updated = int(path.stat().st_mtime)
            title = str(meta.get("title") or next((m["text"] for m in messages if m["role"] == "user"), ""))
            sessions.append(
                {
                    "source": "gemini",
                    "session_id": path.stem,
                    "title": (title[:80] or "Gemini 会话"),
                    "cwd": str(meta.get("cwd") or ""),
                    "updated_at": timestamp_text(updated),
                    "recency_at": updated,
                    "transcript_path": str(path),
                    "archived": False,
                }
            )
            if len(sessions) >= limit:
                break
        return retag_sessions(sessions, self.source)

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 Gemini 会话。", requires_binding=True)
        for path in gemini_session_files(self.sessions_dir):
            if path.stem == session_id:
                messages, meta = read_gemini_session(path)
                selected, context = select_context_messages(messages)
                return {
                    "available": bool(context),
                    "requires_binding": False,
                    "binding_method": "gemini-session-file",
                    "source": self.source,
                    "session_id": session_id,
                    "session_title": str(meta.get("title") or session_id),
                    "cwd": str(meta.get("cwd") or ""),
                    "updated_at": timestamp_text(int(path.stat().st_mtime)),
                    "messages": selected,
                    "context": context,
                    "char_count": len(context),
                    "message_count": len(selected),
                    "transcript_path": str(path),
                    "error": "" if context else "Gemini 会话文件没有可读取的消息。",
                }
        return empty_context_packet(self.source, "Gemini 会话文件不存在。")

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 Gemini 桥为只读模式。")
        if mode != "queue":
            raise ValueError("Gemini CLI 只支持 queue。")
        command = [self.bin, "--resume", session_id, "-p", message]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=bridge_timeout_seconds(self.config, 120000),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"无法运行 Gemini CLI：{exc}") from exc
        if result.returncode != 0:
            raise classify_cli_failure("Gemini CLI 发送", result.stderr or result.stdout)
        return {
            "accepted": True,
            "mode": "queue",
            "result": result.stdout.strip(),
            "effective_model": "session-default",
        }


def devin_request(
    config: dict[str, Any],
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    retry_count: int | None = None,
) -> dict[str, Any]:
    endpoint = str(config.get("endpoint") or "https://api.devin.ai").rstrip("/")
    return request_json_with_retry(
        f"{endpoint}{path}",
        method=method,
        headers=bearer_headers(config, "DEVIN_API_KEY"),
        payload=payload,
        timeout_seconds=bridge_timeout_seconds(config, 60000),
        retry_count=bridge_retry_count(config) if retry_count is None else max(0, retry_count),
    )


def devin_session_row(row: dict[str, Any]) -> dict[str, Any]:
    session_id = str(row.get("devin_id") or row.get("session_id") or row.get("id") or "")
    updated = parse_bridge_timestamp(row.get("updated_at") or row.get("created_at"))
    return {
        "source": "devin",
        "session_id": session_id,
        "title": str(row.get("title") or row.get("name") or "Devin 会话"),
        "cwd": "",
        "updated_at": timestamp_text(updated),
        "recency_at": updated,
        "status": str(row.get("status") or row.get("state") or ""),
        "url": str(row.get("url") or ""),
        "archived": bool(row.get("is_archived") or row.get("archived")),
    }


class DevinBridge(SessionBridge):
    source = "devin"
    adapter = "devin"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.organization_id = str(config.get("organization_id") or "").strip()
        self.read_only = bool(config.get("read_only", True))

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": True,
            "read_context": bool(self.organization_id),
            "send": bool(self.organization_id and not self.read_only),
            "send_modes": ["queue"] if self.organization_id and not self.read_only else [],
            "context_mode": "full" if self.organization_id else "metadata",
            "model_selection": fixed_model_selection(
                "session-default",
                "会话默认模型",
                "当前桥接只能跟随会话模型。",
            ),
        }

    def health(self) -> BridgeHealth:
        try:
            self.list_sessions(1)
            return {"available": True, "status": "ready", "message": "Devin API ready."}
        except BridgeAuthError as exc:
            return {"available": False, "status": "auth_missing", "message": str(exc)}
        except Exception as exc:
            return {"available": False, "status": "unavailable", "message": str(exc)}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        if self.organization_id:
            query = urlencode({"qs": json.dumps({"first": min(max(limit, 1), 200)})})
            result = devin_request(self.config, "GET", f"/v3/organizations/{self.organization_id}/sessions?{query}")
        else:
            result = devin_request(self.config, "GET", f"/v1/sessions?limit={min(max(limit, 1), 100)}")
        if isinstance(result, list):
            items = result
        else:
            items = result.get("items") or result.get("sessions") or result.get("data") or []
        return retag_sessions([devin_session_row(item) for item in items if isinstance(item, dict)], self.source)

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        if not session_id:
            return empty_context_packet(self.source, "请从会话列表中选择一个 Devin 会话。", requires_binding=True)
        if not self.organization_id:
            return empty_context_packet(self.source, "Devin v3 上下文需要配置 organization_id。")
        result = devin_request(
            self.config,
            "GET",
            f"/v3/organizations/{self.organization_id}/sessions/{session_id}/messages?first={MAX_MESSAGES}",
        )
        items = result.get("items", []) if isinstance(result, dict) else []
        messages: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("sender") or item.get("type") or "").lower()
            if role in {"devin", "assistant", "agent"}:
                role = "assistant"
            elif role in {"user", "human"}:
                role = "user"
            else:
                continue
            text = extract_message_text(item.get("message") or item.get("content") or item)
            if text:
                messages.append({"role": role, "text": text})
        selected, context = select_context_messages(messages)
        return {
            "available": bool(context),
            "requires_binding": False,
            "binding_method": "devin-v3-messages",
            "source": self.source,
            "session_id": session_id,
            "session_title": session_id,
            "cwd": "",
            "updated_at": "",
            "messages": selected,
            "context": context,
            "char_count": len(context),
            "message_count": len(selected),
            "transcript_path": "",
            "error": "" if context else "Devin 会话没有可读取的消息。",
        }

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        if self.read_only:
            raise ValueError("当前 Devin 桥为只读模式。")
        if mode != "queue":
            raise ValueError("Devin 只支持 queue。")
        if not self.organization_id:
            raise ValueError("Devin 发送需要配置 organization_id。")
        result = devin_request(
            self.config,
            "POST",
            f"/v3/organizations/{self.organization_id}/sessions/{session_id}/messages",
            {"message": message},
            retry_count=0,
        )
        return {
            "accepted": True,
            "mode": "queue",
            "result": result,
            "effective_model": "session-default",
        }


class DoubaoWorkBridge(SessionBridge):
    source = "doubao_work"
    adapter = "doubao_mcp"

    def capabilities(self) -> BridgeCapabilities:
        return {
            "list_sessions": False,
            "read_context": False,
            "send": False,
            "send_modes": [],
            "context_mode": "none",
            "model_selection": unsupported_model_selection(
                "豆包工作当前仅提供只读 MCP，不支持模型选择。",
            ),
        }

    def health(self) -> BridgeHealth:
        script = Path(self.config.get("script") or APP_ROOT / "scripts" / "prompt_sidecar_mcp.py").expanduser()
        if script.is_file():
            return {"available": True, "status": "tool_only", "message": "只读 MCP 工具可用；不桥接豆包现有会话。"}
        return {"available": False, "status": "unavailable", "message": "只读 MCP 工具脚本不存在。"}

    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        return []

    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        return empty_context_packet(self.source, "豆包工作当前仅提供只读 MCP 工具，不读取现有会话。")

    def send(self, request: BridgeSendRequest) -> SendResult:
        session_id = request["session_id"]
        message = request["message"]
        mode = request["mode"]
        model_id = request.get("model_id") or None
        raise ValueError("豆包工作当前不开放会话发送。")


def register_builtin_bridges() -> None:
    factories = {
        "codex": CodexBridge,
        "dsh": DshBridge,
        "claude": ClaudeBridge,
        "workbuddy": WorkBuddyBridge,
        "openclaw": OpenClawBridge,
        "openhands": OpenHandsBridge,
        "cursor": CursorBridge,
        "copilot": CopilotBridge,
        "gemini": GeminiBridge,
        "devin": DevinBridge,
        "doubao_mcp": DoubaoWorkBridge,
    }
    registered = set(BRIDGE_REGISTRY.adapter_names())
    for adapter, factory in factories.items():
        if adapter not in registered:
            BRIDGE_REGISTRY.register(adapter, factory)


register_builtin_bridges()


def list_all_sessions(source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    wanted = (source or "all").strip()
    sessions: list[dict[str, Any]] = []
    config = load_sidecar_config()
    for bridge_source, bridge_config in config.get("bridges", {}).items():
        if wanted not in {"all", bridge_source}:
            continue
        if not isinstance(bridge_config, dict) or not bridge_config.get("enabled"):
            continue
        try:
            sessions.extend(resolve_bridge(bridge_source).list_sessions(limit))
        except Exception:
            # One broken bridge must not hide the other bridge's sessions.
            continue
    sessions.sort(key=lambda item: item.get("recency_at", 0), reverse=True)
    return sessions


def bridge_statuses() -> dict[str, dict[str, Any]]:
    """Return public bridge settings plus runtime capability/health metadata."""
    config = load_sidecar_config()
    statuses: dict[str, dict[str, Any]] = {}
    for source, bridge_config in config.get("bridges", {}).items():
        if not isinstance(bridge_config, dict):
            continue
        entry = public_bridge_config(bridge_config)
        entry["source"] = source
        entry["adapter"] = str(entry.get("adapter") or source)
        entry["label"] = str(entry.get("label") or source)
        if not entry.get("enabled"):
            entry["capabilities"] = {
                "list_sessions": False,
                "read_context": False,
                "send": False,
                "send_modes": [],
                "context_mode": "none",
                "model_selection": unsupported_model_selection("桥接已禁用。"),
            }
            entry["health"] = {
                "available": False,
                "status": "disabled",
                "message": "桥接已禁用。",
            }
            statuses[source] = entry
            continue
        try:
            bridge = resolve_bridge(source)
            entry["capabilities"] = bridge.capabilities()
            entry["health"] = bridge.health()
        except Exception as exc:
            entry["capabilities"] = {
                "list_sessions": False,
                "read_context": False,
                "send": False,
                "send_modes": [],
                "context_mode": "none",
                "model_selection": unsupported_model_selection("桥接已禁用。"),
            }
            entry["health"] = {
                "available": False,
                "status": "error",
                "message": str(exc),
            }
        statuses[source] = entry
    return statuses


def public_bridge_configs() -> dict[str, dict[str, Any]]:
    """Return editable path overrides without freezing runtime-derived child paths."""
    configured = _read_sidecar_config()
    config = _normalize_sidecar_paths(
        _deep_merge_config(DEFAULT_SIDECAR_CONFIG, configured), configured
    )
    entries: dict[str, dict[str, Any]] = {}
    for source, bridge_config in config.get("bridges", {}).items():
        if not isinstance(bridge_config, dict):
            continue
        entry = public_bridge_config(bridge_config)
        entry["source"] = source
        entry["adapter"] = str(entry.get("adapter") or source)
        entry["label"] = str(entry.get("label") or source)
        explicit = configured.get("bridges", {}).get(source, {})
        if explicit.get("home"):
            entry["home"] = explicit["home"]
        for field in _bridge_child_paths(entry["adapter"].strip()):
            entry[field] = explicit.get(field) or ""
        entries[source] = entry
    return entries


def resolve_route_binding(global_state_path: Path | None = None) -> tuple[str | None, list[str]]:
    path = global_state_path or GLOBAL_STATE
    if not path.exists():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, []

    state = data.get("electron-persisted-atom-state", {})
    matches: list[str] = []
    for key, value in state.items():
        if not key.startswith("thread-tab-routes-v1:") or not isinstance(value, dict):
            continue
        thread_id = key.split(":", 1)[1]
        routes = value.get("routes", [])
        topology = value.get("topology", {})
        right = topology.get("right", {})
        right_tabs = right.get("tabIds", []) if isinstance(right, dict) else []
        for route in routes:
            if not isinstance(route, dict) or route.get("kind") != "browser":
                continue
            params = route.get("params", {})
            restore_url = str(params.get("restoreUrl", ""))
            tab_id = str(route.get("tabId", ""))
            if SIDECAR_URL_MARKER in restore_url:
                if right.get("open") or tab_id in right_tabs:
                    matches.append(thread_id)
                    break
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0], unique
    return None, unique


def resolve_session(
    session_id: str | None = None,
    db_path: Path | None = None,
    global_state_path: Path | None = None,
) -> dict[str, Any]:
    if session_id:
        thread = query_thread(session_id, db_path)
        if thread is None:
            return {
                "ok": False,
                "binding_method": "url-explicit",
                "error": "Session not found.",
                "session_id": session_id,
            }
        return {"ok": True, "binding_method": "url-explicit", "thread": thread}

    bound_id, matches = resolve_route_binding(global_state_path)
    if bound_id:
        thread = query_thread(bound_id, db_path)
        if thread is not None:
            return {
                "ok": True,
                "binding_method": "right-panel-route",
                "thread": thread,
            }
    return {
        "ok": False,
        "binding_method": "unbound",
        "requires_binding": True,
        "candidates": matches,
        "error": "Select a Codex session explicitly.",
    }


def strip_injected_blocks(text: str) -> str:
    text = re.sub(r"<skill>.*?</skill>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<skill_content\b.*?</skill_content>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<system_warning>.*?</system_warning>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<task-director.*?</task-director>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(
        r"<codex_apps_client_time_context>.*?</codex_apps_client_time_context>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return text.strip()


def message_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("content", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"].strip())
    return strip_injected_blocks("\n".join(chunk for chunk in chunks if chunk))


def model_observation(
    model_id: Any = "",
    source: str = "",
    timestamp: Any = "",
) -> dict[str, str]:
    """Keep an explicit log observation separate from model-selection policy."""
    candidate = model_id.strip() if isinstance(model_id, str) else ""
    if candidate in {
        "current-thread", "session-default", "session", "default", "unknown", "<synthetic>",
    }:
        candidate = ""
    return {
        "model_id": candidate,
        "model_source": source if candidate else "",
        "model_timestamp": str(timestamp or "") if candidate else "",
    }


def transcript_evidence(payload: dict[str, Any]) -> str:
    """Summarize tool/error records without copying arguments or secrets into context."""
    payload_type = str(payload.get("type", "")).strip()
    if payload_type in {"function_call", "custom_tool_call", "tool_call"}:
        name = str(payload.get("name") or payload.get("tool_name") or "tool").strip()
        return f"Tool call: {name}"
    if payload_type in {"function_call_output", "custom_tool_call_output", "tool_result"}:
        raw = payload.get("output", payload.get("content", payload.get("result", "")))
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw, ensure_ascii=False)
        text = strip_injected_blocks(str(raw or "")).strip()
        if not text:
            return "Tool result: (empty)"
        return f"Tool result: {text[:1_200]}"
    if payload_type in {"error", "tool_error", "verification", "verification_result"}:
        raw = payload.get("message", payload.get("error", payload.get("result", "")))
        text = strip_injected_blocks(str(raw or "")).strip()
        return f"{payload_type}: {text[:1_200]}".strip()
    return ""


def read_transcript(path: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    messages: list[dict[str, str]] = []
    meta = {
        "session_id": path.stem.split("-")[-1],
        "cwd": "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
        **model_observation(),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload", {})
        if isinstance(payload, dict):
            if record.get("type") == "turn_context":
                meta.update(model_observation(
                    payload.get("model"), "turn_context", record.get("timestamp"),
                ))
            elif (record.get("type") == "event_msg"
                    and payload.get("type") == "thread_settings_applied"):
                settings = payload.get("thread_settings")
                meta.update(model_observation(
                    settings.get("model") if isinstance(settings, dict) else "",
                    "thread_settings_applied",
                    record.get("timestamp"),
                ))
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            meta["session_id"] = str(payload.get("session_id", meta["session_id"]))
            meta["cwd"] = str(payload.get("cwd", ""))
            continue
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        evidence = transcript_evidence(payload)
        if evidence:
            messages.append({"role": "evidence", "text": evidence})
            continue
        if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
            continue
        text = message_text(payload)
        if not text or text.startswith("You have "):
            continue
        if messages and messages[-1] == {"role": payload["role"], "text": text}:
            continue
        messages.append({"role": payload["role"], "text": text})
    return messages, meta


def build_context_packet(
    session_id: str | None = None,
    path: Path | None = None,
    db_path: Path | None = None,
    global_state_path: Path | None = None,
) -> dict[str, Any]:
    binding: dict[str, Any]
    thread: dict[str, Any] | None = None
    transcript: Path | None = path
    if transcript is None:
        binding = resolve_session(session_id, db_path, global_state_path)
        if not binding.get("ok"):
            return {
                "available": False,
                "requires_binding": bool(binding.get("requires_binding")),
                "binding_method": binding.get("binding_method", "unbound"),
                "source": "codex",
                "session_id": session_id or "",
                "session_title": "",
                "cwd": "",
                "updated_at": "",
                "messages": [],
                "context": "",
                "char_count": 0,
                "message_count": 0,
                "transcript_path": "",
                **model_observation(),
                "error": binding.get("error", "Select a Codex session."),
            }
        thread = binding["thread"]
        transcript = Path(thread["rollout_path"])
        if not transcript.exists():
            return {
                "available": False,
                "requires_binding": False,
                "binding_method": binding["binding_method"],
                "source": "codex",
                "session_id": thread["session_id"],
                "session_title": thread["title"],
                "cwd": thread["cwd"],
                "updated_at": thread["updated_at"],
                "messages": [],
                "context": "",
                "char_count": 0,
                "message_count": 0,
                "transcript_path": str(transcript),
                **model_observation(),
                "error": "Transcript file was not found.",
            }
    else:
        binding = {"binding_method": "path-explicit"}

    messages, meta = read_transcript(transcript)
    selected, context = select_context_messages(messages)
    return {
        "available": bool(context),
        "requires_binding": False,
        "binding_method": binding["binding_method"],
        "source": "codex",
        "session_id": (thread or meta).get("session_id", ""),
        "session_title": thread.get("title", "") if thread else "",
        "cwd": thread.get("cwd", "") if thread else meta.get("cwd", ""),
        "updated_at": thread.get("updated_at", "") if thread else meta.get("updated_at", ""),
        "messages": selected,
        "context": context,
        "char_count": len(context),
        "message_count": len(selected),
        "transcript_path": str(transcript),
        **model_observation(meta["model_id"], meta["model_source"], meta["model_timestamp"]),
        "error": "",
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _recommendation(value: Any, fallback_title: str = "继续当前任务") -> dict[str, Any]:
    if isinstance(value, str):
        value = {"title": value}
    if not isinstance(value, dict):
        value = {}
    return {
        "title": str(value.get("title", "")).strip() or fallback_title,
        "scope": _string_list(value.get("scope")),
        "reason": str(value.get("reason", "")).strip(),
        "expected_output": _string_list(value.get("expected_output")),
        "verification": _string_list(value.get("verification")),
        "completion_criteria": _string_list(value.get("completion_criteria")),
        "limitations": _string_list(value.get("limitations")),
        "unresolved_questions": _string_list(value.get("unresolved_questions")),
        "needs_user_confirmation": _string_list(value.get("needs_user_confirmation")),
        "out_of_scope_suggestions": _string_list(value.get("out_of_scope_suggestions")),
    }


def normalize_continuation_analysis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模型返回的下一步分析不是 JSON 对象")
    status = str(value.get("status", "")).strip().lower()
    if status not in CONTINUATION_STATUSES:
        status = "ready"
    objective = str(value.get("task_goal", value.get("objective", ""))).strip()
    completed = []
    for item in value.get("completed", []):
        if isinstance(item, dict):
            completed.append(
                {
                    "item": str(item.get("item", item.get("title", ""))).strip(),
                    "evidence": str(item.get("evidence", "")).strip(),
                }
            )
        elif str(item).strip():
            completed.append({"item": str(item).strip(), "evidence": ""})
    completed = [item for item in completed if item["item"]]
    primary = _recommendation(
        value.get("recommended_next_step", value.get("primary_recommendation")),
        "完成原任务范围内的下一项工作",
    )
    alternatives = [
        _recommendation(item, "可选下一步")
        for item in value.get("alternatives", [])
        if isinstance(item, (dict, str))
    ][:2]
    missing = _string_list(value.get("missing"))
    questions = _string_list(value.get("clarifying_questions"))
    confirmations = _string_list(value.get("needs_user_confirmation"))
    if status == "needs_user_decision" and not questions:
        questions = confirmations or primary["unresolved_questions"]
    if status == "insufficient_context" and not missing:
        missing = ["当前任务目标或可验证的 session 上下文"]
    if status == "needs_user_decision" and not confirmations:
        confirmations = questions
    decision_type = str(value.get("decision_type", "")).strip().lower()
    if decision_type not in CONTINUATION_DECISION_TYPES:
        decision_type = "explicit_human_gate" if status == "needs_user_decision" else ""
    conflicts = _string_list(value.get("conflicts"))
    evidence = _string_list(value.get("evidence"))
    return {
        "status": status,
        "decision_type": decision_type,
        "task_goal": objective,
        "completed": completed,
        "evidence": evidence,
        "pending": _string_list(value.get("pending")),
        "blockers": _string_list(value.get("blockers")),
        "recommended_next_step": primary,
        "alternatives": alternatives,
        "recommendation_reason": str(
            value.get("recommendation_reason", primary["reason"])
        ).strip(),
        "expected_output": _string_list(value.get("expected_output")) or primary["expected_output"],
        "completion_criteria": _string_list(value.get("completion_criteria"))
        or primary["completion_criteria"],
        "needs_user_confirmation": confirmations,
        "clarifying_questions": questions,
        "out_of_scope_suggestions": _string_list(value.get("out_of_scope_suggestions"))
        or primary["out_of_scope_suggestions"],
        "missing": missing,
        "conflicts": conflicts,
    }


def render_continuation_prompt(
    analysis: dict[str, Any],
    recommendation: dict[str, Any] | None = None,
) -> str:
    recommendation = recommendation or analysis["recommended_next_step"]
    status = analysis.get("status", "ready")
    title = recommendation.get("title") or analysis.get("task_goal") or "继续当前任务"
    scope = recommendation.get("scope") or analysis.get("pending") or [
        "先根据当前 session 上下文确认剩余工作，再执行原任务范围内的下一项工作。"
    ]
    criteria = recommendation.get("completion_criteria") or analysis.get("completion_criteria") or [
        "实际完成本轮范围并报告结果、失败项和遗留问题。"
    ]
    verification = recommendation.get("verification") or [
        "运行项目定义的相关测试、构建或可复现验证，并报告实际结果。"
    ]
    limitations = recommendation.get("limitations") or [
        "优先完成原任务范围，不扩大为新的需求。",
        "不要修改无关模块，不要自动发送消息；除非明确授权，不提交、推送或发布。",
    ]
    unresolved = (
        analysis.get("clarifying_questions")
        or recommendation.get("unresolved_questions")
        or analysis.get("needs_user_confirmation")
        or analysis.get("blockers")
    )
    decision_type = analysis.get("decision_type", "")
    requires_human_decision = (
        status == "needs_user_decision" and decision_type in HUMAN_DECISION_TYPES
    )
    if status == "needs_user_decision" and not requires_human_decision:
        status = "in_progress"
        unresolved = []
        scope = [
            *scope,
            "普通实现决策由 Codex 主 Agent 自主选择，不要暂停等待用户逐项确认。",
        ]
    if requires_human_decision:
        scope = ["先暂停主变更，回答以下澄清问题；在获得确认前不要擅自选择方案。"]
    if status == "insufficient_context":
        scope = ["先补充缺失的任务目标、当前状态或验证证据，不要猜测用户意图。"]
    completed_lines = [
        f"- {item['item']}" + (f"（证据：{item['evidence']}）" if item["evidence"] else "")
        for item in analysis.get("completed", [])
    ] or ["- 暂无可确认的已完成事项。"]
    evidence_lines = [f"- {item}" for item in analysis.get("evidence", [])] or [
        "- 仅使用当前 session 中明确提供的证据。"
    ]
    lines = [
        f"继续当前任务：{title}",
        "",
        f"当前目标：\n{analysis.get('task_goal') or '当前任务目标尚未明确，请先补充。'}",
        "",
        f"当前状态：\n{status}",
        "",
        "已完成内容：\n" + "\n".join(completed_lines),
        "",
        "已有完成证据：\n" + "\n".join(evidence_lines),
        "",
        "本轮工作范围：\n" + "\n".join(f"- {item}" for item in scope),
        "",
        "Codex 执行协议：\n" + CODEX_EXECUTION_PROTOCOL,
        "",
        "完成条件：\n" + "\n".join(
            f"{index}. {item}" for index, item in enumerate(criteria, 1)
        ),
        "",
        "验证方式：\n" + "\n".join(f"- {item}" for item in verification),
        "",
        "限制：\n" + "\n".join(f"- {item}" for item in limitations),
        "",
        "未解决的问题：\n"
        + ("\n".join(f"- {item}" for item in unresolved) if unresolved else "- 无。"),
        "",
        "范围外建议：\n"
        + (
            "\n".join(f"- {item}" for item in analysis.get("out_of_scope_suggestions", []))
            if analysis.get("out_of_scope_suggestions")
            else "- 无。"
        ),
        "",
        "期望 Agent 返回的结果：\n- 报告实际完成内容、验证结果、失败项和遗留问题。",
    ]
    return "\n".join(lines).strip()


def build_continuation_model_prompt(
    context: str,
    messages: list[dict[str, str]],
) -> str:
    recent_text = json.dumps(messages[-6:], ensure_ascii=False, indent=2)
    return f"""You are Prompt Sidecar's task-continuation analyzer for a Codex session.
Analyze the supplied session evidence.
Do not invent the user's goal, completed work, evidence, permissions, or acceptance criteria.
Prefer unfinished work inside the original scope. If the evidence shows the user must decide,
return status needs_user_decision and ask concise clarifying questions instead of selecting a plan.
Do not use needs_user_decision for ordinary implementation choices, task decomposition, execution order,
tool choice, or verification strategy. The Codex main Agent decides those autonomously.
Use decision_type=ordinary_implementation for a non-blocking implementation choice. Use one of
goal_conflict, scope_expansion, production_or_destructive, permission_or_secret, external_message,
or insufficient_context only when a human decision is required.
If the task is complete, say so and recommend verification or a bounded follow-up without waiting for
user confirmation unless a human decision gate applies.
Potential new needs must be returned as out_of_scope_suggestions and must not be added to scope.
Return JSON only with these fields:
status (ready|in_progress|blocked|needs_user_decision|completed|insufficient_context), decision_type,
task_goal, completed (objects with item and evidence), evidence, pending, blockers,
recommended_next_step (title, scope, reason, expected_output, verification,
completion_criteria, limitations, unresolved_questions, needs_user_confirmation,
out_of_scope_suggestions), alternatives (0-2 objects of the same shape),
recommendation_reason, expected_output, completion_criteria, needs_user_confirmation,
clarifying_questions, out_of_scope_suggestions, missing, conflicts.

SESSION_CONTEXT:
{context or "[missing]"}

RECENT_MESSAGES:
{recent_text}

CODEX_EXECUTION_PROTOCOL:
{CODEX_EXECUTION_PROTOCOL}
""".strip()


def parse_json_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型未返回有效的下一步 JSON")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("模型未返回有效的下一步 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("模型返回的下一步分析不是 JSON 对象")
    return value


def generate_next_step(
    *,
    source: str,
    session_id: str,
    context_packet: dict[str, Any],
) -> dict[str, Any]:
    context = str(context_packet.get("context", "")).strip()
    messages = context_packet.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    messages = [item for item in messages if isinstance(item, dict)]
    if not context:
        analysis = normalize_continuation_analysis(
            {
                "status": "insufficient_context",
                "missing": [
                    "当前绑定 session 的可读上下文",
                    "明确的任务目标或当前状态",
                ],
            }
        )
        analysis["final_prompt"] = ""
        return analysis
    raw = call_model(build_continuation_model_prompt(context, messages))
    analysis = normalize_continuation_analysis(parse_json_object(raw))
    recommendations = [analysis["recommended_next_step"], *analysis["alternatives"]]
    for recommendation in recommendations:
        recommendation["prompt"] = render_continuation_prompt(analysis, recommendation)
    analysis["recommendations"] = recommendations
    analysis["final_prompt"] = recommendations[0]["prompt"]
    return analysis


def normalize_output_structure(value: Any) -> list[str]:
    if isinstance(value, list):
        parts = [str(item) for item in value]
    else:
        parts = re.split(r"[,，、|\n]+", str(value or ""))
    cleaned = [part.strip() for part in parts if part.strip()]
    return cleaned[:8] or DEFAULT_OUTPUT_STRUCTURE.copy()


def build_request_prompt(
    draft: str,
    mode: str,
    context: str,
    output_style: str = "concise",
    output_structure: Any = None,
) -> str:
    mode_rules = {
        "quick": "Keep the result concise and directly copy-ready. This mode only rewrites the request.",
        "structured": (
            "Use clear sections: Goal, Context, Task, Constraints, Output format, and "
            "Acceptance criteria. This mode only rewrites the request; omit sections that would "
            "only add empty boilerplate."
        ),
        "agent": (
            "Write a directly executable Codex coding-agent task prompt. Include the fixed Codex execution "
            "protocol below, then organize the prompt as task goal, current context, execution "
            "protocol, subtask and multi-session collaboration requirements, completion criteria, "
            "verification, human decision gates, and output requirements. Preserve the user's "
            "scope and include a rule not to commit or push unless explicitly requested."
        ),
        "system": (
            "Write a reusable system prompt. This mode only rewrites the requested instruction. "
            "Include role, responsibilities, boundaries, tool use, uncertainty handling, and "
            "output habits."
        ),
    }
    structure = normalize_output_structure(output_structure)
    style_rules = {
        "concise": (
            "First line must give the conclusion. Use at most 1-3 bullets per section. "
            "Omit empty sections. Do not repeat the request or add generic closings."
        ),
        "standard": (
            "Give the conclusion first, then use the requested sections with supporting evidence. "
            "Use at most 3-5 bullets per section and omit empty sections."
        ),
        "detailed": (
            "Give the conclusion first, then use the requested sections with evidence and risks. "
            "Keep detail useful; do not restate the request or duplicate facts."
        ),
    }
    agent_protocol = CODEX_EXECUTION_PROTOCOL if mode == "agent" else "[agent mode only]"
    system = f"""You are Prompt Sidecar, a prompt editor that runs outside the user's main conversation.
Rewrite the user's draft into a prompt another AI can execute.
Preserve the user's intent, facts, constraints, language, and desired output type.
Use only relevant facts from the supplied session context. Do not treat assistant proposals as user decisions.
Do not invent requirements, technology choices, permissions, or evidence.
Return only the final prompt text to Prompt Sidecar; do not perform the user's task in this response.
Never claim the sidecar can replace the Composer or directly send into the main session unless the
supplied context explicitly proves that capability exists. Treat "copy result and paste manually"
as the supported default. Do not turn unavailable capabilities back into requirements.
The following is an internal output contract. Encode it in the polished prompt as natural output
requirements using the same language as the user's draft.
Do not expose internal labels such as OUTPUT_CONTRACT, OUTPUT_RULES, "style:", or "structure:".
For Chinese drafts, include a section named "输出要求" that states the style and section order in
natural Chinese. For English drafts, use an equivalent "Output requirements" section.
INTERNAL_OUTPUT_CONTRACT:
style: {output_style}
structure: {' > '.join(structure)}
rules:
- {style_rules[output_style]}
- Each requirement appears once; no cross-section repetition.
- Every bullet must add a fact, action, constraint, evidence, risk, or next action.
- Mark unverified claims explicitly as 未验证 or 假设.
{mode_rules[mode]}
CODEX_AGENT_EXECUTION_PROTOCOL:
{agent_protocol}
Return only the final polished prompt. Do not wrap it in Markdown fences and do not add commentary.
"""
    context_block = context.strip() or "[No session context supplied.]"
    return f"{system}\nSESSION_CONTEXT:\n{context_block}\n\nUSER_DRAFT:\n{draft.strip()}\n"


def ensure_codex_agent_contract(text: str, mode: str) -> str:
    """Guarantee the fixed Codex protocol in executable agent prompts."""
    polished = text.strip()
    if mode != "agent" or "Codex 执行协议：" in polished:
        return polished
    return f"{polished}\n\n{CODEX_EXECUTION_PROTOCOL}".strip()


def parse_response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
    return "\n".join(chunks).strip()


def call_model(prompt: str) -> str:
    runtime = load_runtime_config()
    if not runtime["token"]:
        raise RuntimeError("No bearer token is available for the local model proxy.")
    payload = json.dumps(
        {
            "model": runtime["model"],
            "input": prompt,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{runtime['base_url']}/responses",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {runtime['token']}",
        },
    )
    try:
        with urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Model proxy returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Model proxy is unavailable: {exc.reason}") from exc
    text = parse_response_text(data)
    if not text:
        raise RuntimeError("The model returned an empty response.")
    return text


def parse_queue_output(output: str) -> str:
    match = re.search(r"Queued message\s+(\S+)\s+for thread", output)
    return match.group(1) if match else ""


def send_queued_message(
    session_id: str,
    message: str,
    db_path: Path | None = None,
    codex_home: Path | None = None,
    timeout_ms: int = 30000,
) -> dict[str, str]:
    if not CODEX_BIN:
        raise RuntimeError("Codex executable not found. Set CODEX_BIN or add codex to PATH.")
    thread = query_thread(session_id, db_path=db_path)
    if thread is None:
        raise ValueError("Session not found.")
    command = [
        CODEX_BIN,
        "queue",
        "--thread",
        session_id,
        "--message",
        message,
    ]
    try:
        env = os.environ.copy()
        if codex_home is not None:
            env["CODEX_HOME"] = str(codex_home)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_ms / 1000),
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Unable to run codex queue: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise classify_cli_failure("codex queue ", detail)
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return {
        "queue_id": parse_queue_output(stdout),
        "stdout": stdout,
        "stderr": stderr,
    }


class CodexAppServerClient:
    """Small stdio JSON-RPC client for the public Codex app-server protocol."""

    def __init__(
        self,
        binary: str = "",
        timeout_ms: int = 10000,
        codex_home: Path | None = None,
    ) -> None:
        self.binary = binary or CODEX_BIN
        self.timeout_seconds = max(1.0, timeout_ms / 1000)
        self.codex_home = codex_home
        self.process: subprocess.Popen[str] | None = None
        self.next_id = 1

    def __enter__(self) -> "CodexAppServerClient":
        if not self.binary:
            raise RuntimeError("Codex executable not found.")
        try:
            env = os.environ.copy()
            if self.codex_home is not None:
                env["CODEX_HOME"] = str(self.codex_home)
            self.process = subprocess.Popen(
                [self.binary, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise RuntimeError(f"Unable to start Codex app-server: {exc}") from exc
        self._send({"id": 0, "method": "initialize", "params": {
            "clientInfo": {"name": "prompt-sidecar", "title": "Prompt Sidecar", "version": "0.4.0"}
        }})
        self._wait_for_response(0)
        self._send({"method": "initialized", "params": {}})
        return self

    def __exit__(self, *args: Any) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except OSError:
                pass
        self.process = None

    def _send(self, payload: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Codex app-server is not running.")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _wait_for_response(self, request_id: int) -> dict[str, Any]:
        if self.process is None or self.process.stdout is None:
            raise RuntimeError("Codex app-server is not running.")
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while time.monotonic() < deadline:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                line = self.process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if message.get("error"):
                    raise RuntimeError(str(message["error"].get("message") or message["error"]))
                return message.get("result") or {}
        finally:
            selector.close()
        raise RuntimeError("Timed out waiting for Codex app-server.")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        return self._wait_for_response(request_id)

    def list_threads(self, limit: int) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {
                "limit": limit,
                "archived": False,
                "useStateDbOnly": True,
                "sortKey": "recency_at",
                "sortDirection": "desc",
            },
        )
        data = result.get("data", [])
        return data if isinstance(data, list) else []


def list_codex_app_server_threads(
    limit: int = 50,
    timeout_ms: int = 10000,
    codex_home: Path | None = None,
) -> list[dict[str, Any]]:
    with CodexAppServerClient(timeout_ms=timeout_ms, codex_home=codex_home) as client:
        rows = client.list_threads(limit)
    sessions: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        updated_at = int(row.get("updatedAt") or row.get("recencyAt") or 0)
        sessions.append(
            {
                "source": "codex",
                "session_id": str(row.get("id")),
                "title": str(row.get("name") or row.get("preview") or "未命名会话"),
                "raw_title": str(row.get("preview") or ""),
                "cwd": str(row.get("cwd") or ""),
                "rollout_path": str(row.get("path") or ""),
                "updated_at": timestamp_text(updated_at),
                "recency_at": int(row.get("recencyAt") or updated_at or 0),
                "archived": False,
                "originator": str(row.get("originator") or ""),
            }
        )
    return sessions[:limit]


def claude_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return strip_injected_blocks(content)
    if isinstance(content, list):
        return message_text({"content": content})
    return ""


def read_claude_transcript(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Read Claude Code's local JSONL transcript without depending on display fields."""
    messages: list[dict[str, str]] = []
    meta: dict[str, Any] = {
        "session_id": path.stem,
        "cwd": "",
        "title": "",
        "updated_at_ms": int(path.stat().st_mtime * 1000),
        **model_observation(),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        session_id = record.get("sessionId")
        if session_id:
            meta["session_id"] = str(session_id)
        if record.get("cwd"):
            meta["cwd"] = str(record["cwd"])
        if record.get("lastPrompt"):
            meta["title"] = strip_injected_blocks(str(record["lastPrompt"]))[:80]
        timestamp = record.get("timestamp")
        if isinstance(timestamp, str):
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                meta["updated_at_ms"] = max(meta["updated_at_ms"], int(parsed.timestamp() * 1000))
            except ValueError:
                pass
        if record.get("type") not in {"user", "assistant"}:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or record.get("type"))
        if role not in {"user", "assistant"}:
            continue
        if role == "assistant":
            is_sidechain = record.get("isSidechain") is True or message.get("isSidechain") is True
            is_meta = record.get("isMeta") is True or message.get("isMeta") is True
            if not is_sidechain and not is_meta:
                meta.update(model_observation(
                    message.get("model"), "assistant.message.model", record.get("timestamp"),
                ))
        text = claude_text_from_content(message.get("content"))
        if not text or text.startswith("You have "):
            continue
        item = {"role": role, "text": text}
        if messages and messages[-1] == item:
            continue
        messages.append(item)
    if not meta["title"]:
        first_user = next((item["text"] for item in messages if item["role"] == "user"), "")
        meta["title"] = first_user[:80] or "未命名 Claude 会话"
    return messages, meta


def find_claude_transcript(session_id: str, projects_dir: Path) -> Path | None:
    if not session_id or not projects_dir.exists():
        return None
    for path in projects_dir.glob("**/*.jsonl"):
        if path.stem == session_id:
            return path
    return None


def list_claude_sessions(limit: int = 50, projects_dir: Path | None = None) -> list[dict[str, Any]]:
    root = projects_dir or Path.home() / ".claude" / "projects"
    if not root.exists():
        return []
    candidates = sorted(
        root.glob("**/*.jsonl"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    sessions: list[dict[str, Any]] = []
    for path in candidates[: max(limit * 3, limit)]:
        try:
            _, meta = read_claude_transcript(path)
        except OSError:
            continue
        updated_seconds = int(meta["updated_at_ms"] // 1000)
        sessions.append(
            {
                "source": "claude",
                "session_id": str(meta["session_id"]),
                "title": str(meta["title"]),
                "raw_title": str(meta["title"]),
                "cwd": str(meta["cwd"]),
                "transcript_path": str(path),
                "updated_at": timestamp_text(updated_seconds),
                "recency_at": updated_seconds,
                "archived": False,
            }
        )
        if len(sessions) >= limit:
            break
    sessions.sort(key=lambda item: item["recency_at"], reverse=True)
    return sessions


def build_claude_context_packet(
    session_id: str,
    projects_dir: Path | None = None,
) -> dict[str, Any]:
    root = projects_dir or Path.home() / ".claude" / "projects"
    path = find_claude_transcript(session_id, root)
    if path is None:
        return empty_context_packet("claude", "Claude Code 会话 transcript 不存在。")
    try:
        messages, meta = read_claude_transcript(path)
    except OSError as exc:
        return empty_context_packet("claude", f"无法读取 Claude Code transcript：{exc}")
    selected, context = select_context_messages(messages)
    return {
        "available": bool(context),
        "requires_binding": False,
        "binding_method": "claude-jsonl",
        "source": "claude",
        "session_id": str(meta["session_id"]),
        "session_title": str(meta["title"]),
        "cwd": str(meta["cwd"]),
        "updated_at": timestamp_text(int(meta["updated_at_ms"] // 1000)),
        "messages": selected,
        "context": context,
        "char_count": len(context),
        "message_count": len(selected),
        "transcript_path": str(path),
        **model_observation(meta["model_id"], meta["model_source"], meta["model_timestamp"]),
        "error": "",
    }


def send_claude_message(
    session_id: str,
    message: str,
    projects_dir: Path | None = None,
    binary: str = "claude",
    timeout_ms: int = 120000,
    model_id: str | None = None,
) -> dict[str, Any]:
    path = find_claude_transcript(session_id, projects_dir or Path.home() / ".claude" / "projects")
    if path is None:
        raise ValueError("Claude Code 会话 transcript 不存在。")
    cwd = ""
    try:
        _, meta = read_claude_transcript(path)
        cwd = str(meta.get("cwd") or "")
    except OSError:
        pass
    command = [binary, "-p", "--resume", session_id]
    if model_id:
        command.extend(["--model", model_id])
    command.extend(["--output-format", "json", message])
    try:
        result = subprocess.run(
            command,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_ms / 1000),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法运行 Claude Code：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise classify_cli_failure("Claude Code 发送", detail)
    text = result.stdout.strip()
    try:
        payload = json.loads(text)
        response_text = payload.get("result") or payload.get("text") or text
    except json.JSONDecodeError:
        response_text = text
    return {
        "accepted": True,
        "result": response_text,
        "stdout": text,
        "stderr": result.stderr.strip(),
        "effective_model": "session-default",
    }


def workbuddy_cli_command(config: dict[str, Any]) -> list[str]:
    """Resolve the local WorkBuddy CLI without requiring a cloud token."""
    script = str(config.get("script") or "").strip()
    if script:
        path = Path(script).expanduser()
        if path.is_file():
            node = shutil.which("node")
            return [node, str(path)] if node else []
    candidates = (
        Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs"),
        Path("/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/cli/dist/codebuddy.js"),
    )
    node = shutil.which("node")
    if node:
        for candidate in candidates:
            if candidate.is_file():
                return [node, str(candidate)]
    binary = str(config.get("bin") or "codebuddy").strip()
    resolved = shutil.which(binary) if binary else None
    return [resolved] if resolved else []


def find_workbuddy_local_transcript(session_id: str, projects_dir: Path) -> Path | None:
    if not session_id or Path(session_id).name != session_id or not projects_dir.exists():
        return None
    for candidate in projects_dir.rglob(f"{session_id}.jsonl"):
        if candidate.is_file():
            return candidate
    return None


def workbuddy_local_session(
    row: tuple[Any, ...],
    projects_dir: Path,
    transcript_map: dict[str, Path] | None = None,
) -> dict[str, Any]:
    session_id, title, cwd, status, updated_at, last_activity_at = row
    updated_ms = int(last_activity_at or updated_at or 0)
    transcript = (
        transcript_map.get(str(session_id))
        if transcript_map is not None
        else find_workbuddy_local_transcript(str(session_id), projects_dir)
    )
    return {
        "source": "workbuddy_local",
        "session_id": str(session_id),
        "title": str(title or "").strip() or "WorkBuddy 会话",
        "raw_title": str(title or ""),
        "cwd": str(cwd or ""),
        "status": str(status or ""),
        "updated_at": timestamp_text(updated_ms // 1000),
        "recency_at": updated_ms // 1000,
        "transcript_path": str(transcript) if transcript else "",
        "archived": str(status or "").lower() == "archived",
    }


def list_workbuddy_local_sessions(
    db_path: Path,
    projects_dir: Path,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id, COALESCE(custom_title, title, ''), cwd, status, updated_at, "
            "last_activity_at FROM sessions "
            "WHERE transport = 'local' AND (deleted_at IS NULL OR deleted_at = -1) "
            "ORDER BY COALESCE(last_activity_at, updated_at) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    transcript_map = {
        path.stem: path for path in projects_dir.rglob("*.jsonl") if path.is_file()
    } if projects_dir.exists() else {}
    return [workbuddy_local_session(row, projects_dir, transcript_map) for row in rows]


def read_workbuddy_local_transcript(path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    messages: list[dict[str, str]] = []
    meta: dict[str, Any] = {
        "session_id": path.stem, "cwd": "", "title": "", **model_observation(),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("sessionId"):
            meta["session_id"] = str(record["sessionId"])
        if record.get("cwd"):
            meta["cwd"] = str(record["cwd"])
        if record.get("type") != "message" or record.get("role") not in {"user", "assistant"}:
            continue
        if record["role"] == "assistant":
            provider = record.get("providerData")
            meta.update(model_observation(
                provider.get("model") if isinstance(provider, dict) else "",
                "assistant.providerData.model",
                record.get("timestamp"),
            ))
        text = claude_text_from_content(record.get("content"))
        if not text:
            continue
        item = {"role": str(record["role"]), "text": text}
        if messages and messages[-1] == item:
            continue
        messages.append(item)
    if not meta["title"]:
        meta["title"] = next((item["text"] for item in messages if item["role"] == "user"), "")
        meta["title"] = meta["title"][:80] or "WorkBuddy 会话"
    return messages, meta


def build_workbuddy_local_context_packet(
    config: dict[str, Any],
    session_id: str,
) -> dict[str, Any]:
    home = Path(config.get("home") or Path.home() / ".workbuddy")
    db_path = Path(config.get("db_path") or home / "workbuddy.db")
    projects_dir = Path(config.get("projects_dir") or home / "projects")
    transcript = find_workbuddy_local_transcript(session_id, projects_dir)
    if transcript is None:
        return empty_context_packet("workbuddy_local", "WorkBuddy 本地会话 transcript 不存在。")
    try:
        messages, meta = read_workbuddy_local_transcript(transcript)
        row = None
        if db_path.exists():
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = connection.execute(
                    "SELECT id, COALESCE(custom_title, title, ''), cwd, status, "
                    "updated_at, last_activity_at FROM sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()
            finally:
                connection.close()
    except (OSError, sqlite3.Error) as exc:
        return empty_context_packet("workbuddy_local", f"读取 WorkBuddy 本地会话失败：{exc}")
    title = str(row[1] or meta.get("title") or "WorkBuddy 会话") if row else meta["title"]
    cwd = str(row[2] or meta.get("cwd") or "") if row else str(meta.get("cwd") or "")
    selected, context = select_context_messages(messages)
    return {
        "available": bool(context),
        "requires_binding": False,
        "binding_method": "workbuddy-local-jsonl",
        "source": "workbuddy_local",
        "session_id": session_id,
        "session_title": title,
        "cwd": cwd,
        "updated_at": "",
        "messages": selected,
        "context": context,
        "char_count": len(context),
        "message_count": len(selected),
        "transcript_path": str(transcript),
        **model_observation(meta["model_id"], meta["model_source"], meta["model_timestamp"]),
        "error": "",
    }


def send_workbuddy_local_prompt(
    config: dict[str, Any],
    session_id: str,
    message: str,
) -> dict[str, Any]:
    home = Path(config.get("home") or Path.home() / ".workbuddy")
    db_path = Path(config.get("db_path") or home / "workbuddy.db")
    projects_dir = Path(config.get("projects_dir") or home / "projects")
    transcript = find_workbuddy_local_transcript(session_id, projects_dir)
    if transcript is None:
        raise ValueError("WorkBuddy 本地会话不存在。")
    cwd = ""
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT cwd FROM sessions WHERE id = ?", (session_id,)).fetchone()
            cwd = str(row[0] or "") if row else ""
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    command = workbuddy_cli_command(config)
    if not command:
        raise RuntimeError("未找到 WorkBuddy 本地 CLI。")
    command.extend(["-p", "--resume", session_id, "--output-format", "json", message])
    env = os.environ.copy()
    env["WORKBUDDY_CONFIG_DIR"] = str(home)
    env["WORKBUDDY_DATA_DIR"] = str(home)
    try:
        result = subprocess.run(
            command,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=max(1, int(config.get("timeout_ms") or 120000) / 1000),
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法运行 WorkBuddy 本地 CLI：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise classify_cli_failure("WorkBuddy 本地发送", detail)
    text = result.stdout.strip()
    try:
        payload = json.loads(text)
        response_text = payload.get("result") or payload.get("text") or text
    except json.JSONDecodeError:
        response_text = text
    return {
        "accepted": True,
        "mode": "queue",
        "result": response_text,
        "stdout": text,
        "stderr": result.stderr.strip(),
    }


def workbuddy_access_token(config: dict[str, Any]) -> str:
    ref = str(config.get("auth_ref") or "WORKBUDDY_ACCESS_TOKEN").strip()
    token = os.environ.get(ref, "").strip() if ref else ""
    if not token:
        raise RuntimeError(
            f"未找到 WorkBuddy access token；请设置环境变量 {ref or '(空)'}。"
        )
    return token


def workbuddy_request_json(
    config: dict[str, Any],
    path: str,
    query: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint = str(config.get("endpoint") or "https://www.workbuddy.cn").rstrip("/")
    url = f"{endpoint}{path}"
    if query:
        url += "?" + urlencode(query)
    request = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {workbuddy_access_token(config)}",
            "Accept": "application/json",
        },
    )
    try:
        timeout = max(2, int(config.get("timeout_ms") or 30000) / 1000)
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"WorkBuddy API HTTP {exc.code}: {detail}") from exc
    except (URLError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"WorkBuddy API 不可用：{exc}") from exc
    if data.get("code", 0) not in {0, "0", None}:
        raise RuntimeError(str(data.get("msg") or "WorkBuddy API 返回错误。"))
    return data


def workbuddy_task_session(task: dict[str, Any]) -> dict[str, Any]:
    timestamp = str(task.get("updated_at") or task.get("created_at") or "")
    recency = 0
    if timestamp:
        try:
            recency = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
        except ValueError:
            recency = 0
    return {
        "source": "workbuddy_cloud",
        "session_id": str(task.get("task_id") or ""),
        "title": str(task.get("name") or "WorkBuddy 云端任务"),
        "raw_title": str(task.get("name") or ""),
        "cwd": "",
        "updated_at": timestamp_text(recency) if recency else timestamp,
        "recency_at": recency,
        "status": str(task.get("status") or ""),
        "archived": str(task.get("status") or "") == "archived",
    }


def list_workbuddy_tasks(config: dict[str, Any], limit: int = 50) -> list[dict[str, Any]]:
    data = workbuddy_request_json(
        config,
        "/openapi/v2/tasks",
        {"page": 1, "size": min(max(limit, 1), 100)},
    )
    tasks = data.get("data", {}).get("tasks", data.get("tasks", []))
    if not isinstance(tasks, list):
        return []
    return [
        workbuddy_task_session(item)
        for item in tasks
        if isinstance(item, dict) and item.get("task_id")
    ]


def get_workbuddy_task(config: dict[str, Any], session_id: str) -> dict[str, Any]:
    data = workbuddy_request_json(config, f"/openapi/v2/tasks/{session_id}")
    task = data.get("data", data)
    if not isinstance(task, dict):
        raise RuntimeError("WorkBuddy 任务详情格式无效。")
    return task


def extract_acp_messages(value: Any) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            role = node.get("role")
            content = node.get("content")
            if role in {"user", "assistant"}:
                text = claude_text_from_content(content)
                if text:
                    item = {"role": str(role), "text": text}
                    if not found or found[-1] != item:
                        found.append(item)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


class WorkBuddyACPClient:
    """Minimal ACP client for WorkBuddy Cloud task sessions."""

    def __init__(self, link: str, token: str, timeout_ms: int = 120000) -> None:
        self.link = link
        self.token = token
        self.timeout_seconds = max(2, timeout_ms / 1000)
        self.stream = None
        self.connection_id = ""
        self.next_id = 1
        self.notifications: list[dict[str, Any]] = []

    def __enter__(self) -> "WorkBuddyACPClient":
        request = Request(
            self.link,
            method="GET",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "text/event-stream"},
        )
        try:
            self.stream = urlopen(request, timeout=self.timeout_seconds)
        except (HTTPError, URLError, OSError) as exc:
            raise RuntimeError(f"WorkBuddy ACP 连接失败：{exc}") from exc
        self.connection_id = str(self.stream.headers.get("Acp-Connection-Id") or "")
        return self

    def __exit__(self, *args: Any) -> None:
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
        self.stream = None

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.stream is None:
            raise RuntimeError("WorkBuddy ACP 尚未连接。")
        request_id = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._post_json(payload)
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            line = self.stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text.startswith("data:"):
                continue
            data = text[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if message.get("method") and message.get("id") is not None:
                self._respond_to_server_request(message)
                continue
            if message.get("method") and "id" not in message:
                self.notifications.append(message)
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError(str(message["error"].get("message") or message["error"]))
            return message.get("result") or {}
        raise RuntimeError(f"WorkBuddy ACP 等待 {method} 响应超时。")

    def _post_json(self, payload: dict[str, Any]) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Acp-Connection-Id": self.connection_id,
        }
        try:
            request = Request(
                self.link,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers=headers,
            )
            with urlopen(request, timeout=self.timeout_seconds):
                pass
        except (HTTPError, URLError, OSError) as exc:
            raise RuntimeError(f"WorkBuddy ACP 请求失败：{exc}") from exc

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        """Reject unsupported interactive requests instead of leaving the agent blocked."""
        method = str(message.get("method") or "")
        if method == "session/request_permission":
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "result": {"outcome": "cancelled"},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32601, "message": f"Unsupported ACP client method: {method}"},
            }
        self._post_json(response)

    def initialize(self) -> dict[str, Any]:
        return self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False}
                },
            },
        )


def workbuddy_acp_cwd(config: dict[str, Any]) -> str:
    """The working directory belongs to the remote ACP host, not this machine."""
    cwd = str(config.get("acp_cwd") or "/workspace").strip()
    if not cwd.startswith("/") or "\x00" in cwd:
        raise ValueError("WorkBuddy acp_cwd must be an absolute path on the remote host")
    return cwd


def workbuddy_acp_load(
    config: dict[str, Any],
    session_id: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    task = get_workbuddy_task(config, session_id)
    link = str(task.get("link") or "")
    token = str(task.get("token") or workbuddy_access_token(config))
    if not link:
        raise RuntimeError("WorkBuddy 任务没有可用的 ACP link。")
    with WorkBuddyACPClient(link, token, int(config.get("timeout_ms") or 120000)) as client:
        client.initialize()
        loaded = client.request(
            "session/load",
            {"sessionId": session_id, "cwd": workbuddy_acp_cwd(config), "mcpServers": []},
        )
        messages = extract_acp_messages(loaded)
        if not messages:
            messages = extract_acp_messages(client.notifications)
    return task, messages


def build_workbuddy_context_packet(config: dict[str, Any], session_id: str) -> dict[str, Any]:
    try:
        task, messages = workbuddy_acp_load(config, session_id)
    except (RuntimeError, ValueError) as exc:
        return empty_context_packet("workbuddy_cloud", str(exc))
    selected, context = select_context_messages(messages)
    base = workbuddy_task_session(task)
    return {
        "available": bool(context),
        "requires_binding": False,
        "binding_method": "workbuddy-acp",
        "source": "workbuddy_cloud",
        "session_id": session_id,
        "session_title": base["title"],
        "cwd": base["cwd"],
        "updated_at": base["updated_at"],
        "messages": selected,
        "context": context,
        "char_count": len(context),
        "message_count": len(selected),
        "transcript_path": "",
        "error": "" if context else "ACP 未返回历史消息；只能使用任务元数据。",
    }


def send_workbuddy_prompt(config: dict[str, Any], session_id: str, message: str) -> dict[str, Any]:
    task = get_workbuddy_task(config, session_id)
    link = str(task.get("link") or "")
    token = str(task.get("token") or workbuddy_access_token(config))
    if not link:
        raise RuntimeError("WorkBuddy 任务没有可用的 ACP link。")
    with WorkBuddyACPClient(link, token, int(config.get("timeout_ms") or 120000)) as client:
        client.initialize()
        client.request(
            "session/load",
            {"sessionId": session_id, "cwd": workbuddy_acp_cwd(config), "mcpServers": []},
        )
        result = client.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": message}]},
        )
    return {
        "accepted": True,
        "mode": "queue",
        "stop_reason": str(result.get("stopReason") or ""),
        "result": result,
    }


class PromptSidecarHandler(BaseHTTPRequestHandler):
    server_version = f"PromptSidecar/{APP_VERSION}"

    def do_GET(self) -> None:
        try:
            self.require_local_request()
        except RequestError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc), "error_code": exc.code}, exc.status
            )
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/bootstrap":
            port = self.server.server_address[1]
            self.respond_json(
                {
                    "ok": True,
                    "kind": "prompt-sidecar-local",
                    "version": APP_VERSION,
                    "panel_mode": "hosted",
                    "api_base": f"http://127.0.0.1:{port}",
                    "local_only": True,
                    "capabilities": [
                        "sessions", "context", "polish", "next_step", "send"
                    ],
                }
            )
            return
        if parsed.path == "/api/health":
            runtime = load_runtime_config()
            self.respond_json(
                {
                    "ok": True,
                    "model": runtime["model"],
                    "proxy": runtime["base_url"],
                    "bridges": bridge_statuses(),
                }
            )
            return
        if parsed.path == "/api/context":
            query = parse_qs(parsed.query)
            source = query.get("source", [""])[0].strip() or None
            session_id = query.get("session_id", [""])[0].strip() or None
            if source:
                try:
                    packet = resolve_bridge(source).build_context_packet(session_id)
                except ValueError as exc:
                    self.respond_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
            else:
                packet = build_context_packet(session_id=session_id)
            packet.setdefault("source", source or "codex")
            self.respond_json(packet)
            return
        if parsed.path == "/api/sessions":
            query = parse_qs(parsed.query)
            source = query.get("source", [""])[0].strip() or None
            self.respond_json({"ok": True, "sessions": list_all_sessions(source)})
            return
        if parsed.path == "/api/config":
            self.respond_json({
                "ok": True,
                "bridges": public_bridge_configs(),
            })
            return
        self.serve_static(parsed.path)

    def do_OPTIONS(self) -> None:
        if not urlparse(self.path).path.startswith("/api/"):
            self.respond_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            self.require_local_request()
        except RequestError as exc:
            self.respond_json(
                {"ok": False, "error": str(exc), "error_code": exc.code}, exc.status
            )
            return
        origin = self.headers.get("Origin", "")
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            self.headers.get("Access-Control-Request-Headers", "Content-Type"),
        )
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/polish", "/api/next-step", "/api/send", "/api/config"}:
            self.respond_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        body: dict[str, Any] = {}
        polish_meta: dict[str, str] = {}
        try:
            self.require_local_request()
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Request body must be an object")
            if path == "/api/polish":
                polish_meta = polish_request_metadata(body, self.headers)
            if path == "/api/config":
                save_sidecar_config(body.get("bridges"))
                self.respond_json({
                    "ok": True,
                    "bridges": public_bridge_configs(),
                })
                return
            if path == "/api/next-step":
                source = str(body.get("source", "")).strip() or "codex"
                session_id = str(body.get("session_id", "")).strip()
                supplied_context = str(body.get("context", "")).strip()
                context_packet = resolve_bridge(source).build_context_packet(session_id or None)
                if supplied_context:
                    context_packet = {**context_packet, "context": supplied_context}
                result = generate_next_step(
                    source=source,
                    session_id=session_id,
                    context_packet=context_packet,
                )
                self.respond_json(
                    {
                        "ok": True,
                        "source": source,
                        "session_id": session_id,
                        "cwd": context_packet.get("cwd", ""),
                        "session_title": context_packet.get("session_title", ""),
                        **result,
                    }
                )
                return
            if path == "/api/send":
                source = str(body.get("source", "")).strip() or "codex"
                session_id = str(body.get("session_id", "")).strip()
                message = str(body.get("message", "")).strip()
                mode = str(body.get("mode", "queue")).strip() or "queue"
                if mode not in ALLOWED_SEND_MODES:
                    raise ValueError("Unsupported send mode")
                if not session_id:
                    raise ValueError("session_id is required")
                if not message:
                    raise ValueError("Message is required")
                bridge = resolve_bridge(source)
                model_id, effective_model = model_plan_for_send(bridge, source)
                send_request: BridgeSendRequest = {
                    "source": source,
                    "session_id": session_id,
                    "message": message,
                    "mode": mode,
                    "model_id": model_id or "",
                }
                sent = bridge.send(send_request)
                sent.setdefault("effective_model", effective_model)
                sent.update(verified_session_model(bridge, session_id))
                self.respond_json(
                    {
                        "ok": True,
                        "queued": True,
                        "source": source,
                        "mode": mode,
                        "session_id": session_id,
                        **sent,
                    }
                )
                return

            source = str(body.get("source", "")).strip() or None
            session_id = str(body.get("session_id", "")).strip() or None
            draft = str(body.get("draft", "")).strip()
            mode = str(body.get("mode", "quick"))
            use_context = bool(body.get("use_context", True))
            supplied_context = str(
                body.get("supplied_context", body.get("context", ""))
            ).strip()
            output_style = str(body.get("output_style", "concise")).strip() or "concise"
            output_structure = normalize_output_structure(body.get("output_structure"))
            if output_style not in ALLOWED_OUTPUT_STYLES:
                raise ValueError("Unsupported output_style")
            if not draft:
                raise ValueError("Draft is required")
            if mode not in ALLOWED_MODES:
                raise ValueError("Unsupported mode")
            context = supplied_context if use_context else ""
            if use_context and not context:
                if source:
                    context = resolve_bridge(source).build_context_packet(session_id)["context"]
                else:
                    context = build_context_packet(session_id=session_id)["context"]
            started = time.perf_counter()
            polished = ensure_codex_agent_contract(
                call_model(build_request_prompt(draft, mode, context, output_style, output_structure)),
                mode,
            )
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.respond_json(
                {
                    "ok": True,
                    "polished": polished,
                    "mode": mode,
                    **polish_meta,
                    "source": source or "codex",
                    "session_id": session_id,
                    "cwd": "",
                    "used_context": use_context,
                    "context_used": bool(context),
                    "context_chars": len(context),
                    "output_style": output_style,
                    "output_structure": output_structure,
                    "elapsed_ms": elapsed_ms,
                }
            )
        except RequestError as exc:
            response = {"ok": False, "error": str(exc), "error_code": exc.code}
            if path == "/api/polish":
                response.update(polish_meta or polish_request_metadata(body, self.headers))
            self.respond_json(response, exc.status)
        except BridgeAuthError as exc:
            status = HTTPStatus.UNAUTHORIZED
            response = {"ok": False, "error": str(exc)}
            if path == "/api/polish":
                response.update(polish_meta or polish_request_metadata(body, self.headers))
            self.respond_json(response, status=status)
            return
        except BridgeUnavailableError as exc:
            status = HTTPStatus.SERVICE_UNAVAILABLE
            response = {"ok": False, "error": str(exc)}
            if path == "/api/polish":
                response.update(polish_meta or polish_request_metadata(body, self.headers))
            self.respond_json(response, status=status)
            return
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            status = HTTPStatus.BAD_GATEWAY if isinstance(exc, RuntimeError) else HTTPStatus.BAD_REQUEST
            response = {"ok": False, "error": str(exc)}
            if path == "/api/polish":
                response.update(polish_meta or polish_request_metadata(body, self.headers))
            self.respond_json(response, status=status)

    def require_local_request(self) -> None:
        try:
            peer = ipaddress.ip_address(self.client_address[0])
            if not peer.is_loopback:
                raise ValueError("non-loopback peer")
            host = urlsplit(f"http://{self.headers.get('Host', '')}")
            if host.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("invalid host")
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                normalized = _normalized_origin(origin)
                loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                same_origin = loopback and parsed.netloc == host.netloc
                hosted_origin = normalized in panel_allowed_origins()
                if not same_origin and not hosted_origin:
                    raise ValueError("invalid origin")
        except ValueError as exc:
            raise RequestError("local_request_required", "仅允许同源本机请求", 403) from exc

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        ui_assets = {"index.html", "app.js", "styles.css", "favicon.svg", "prompt-flow.svg"}
        if relative not in ui_assets:
            self.respond_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        web_root = APP_ROOT / "frontend"
        target = (web_root / relative).resolve()
        if target != web_root / relative:
            self.respond_json({"error": "Invalid path"}, status=HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.respond_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def respond_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = _normalized_origin(self.headers.get("Origin", ""))
            if origin and self._request_origin_is_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _request_origin_is_allowed(self, origin: str) -> bool:
        parsed = urlsplit(origin)
        host = urlsplit(f"http://{self.headers.get('Host', '')}")
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        return (loopback and parsed.netloc == host.netloc) or origin in panel_allowed_origins()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        safe_path = request_path_without_query(self.path)
        requestline = f"{self.command} {safe_path} {self.request_version}"
        self.log_message('"%s" %s %s', requestline, str(code), str(size))

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[prompt-sidecar] {self.address_string()} {format % args}")


def ensure_config_file() -> None:
    """Create the ignored local config from the public example when possible."""
    if CONFIG_PATH.exists():
        return
    try:
        if CONFIG_EXAMPLE_PATH.exists():
            content = CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8")
        else:
            content = json.dumps(DEFAULT_SIDECAR_CONFIG, ensure_ascii=False, indent=4) + "\n"
        CONFIG_PATH.write_text(content, encoding="utf-8")
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Prompt Sidecar UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    ensure_config_file()
    server = ThreadingHTTPServer((args.host, args.port), PromptSidecarHandler)
    print(f"Prompt Sidecar: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

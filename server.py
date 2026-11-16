#!/usr/bin/env python3
"""Local Prompt Sidecar server with explicit Codex thread binding."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import tomllib
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


APP_ROOT = Path(__file__).resolve().parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
STATE_DB = CODEX_HOME / "state_5.sqlite"
GLOBAL_STATE = CODEX_HOME / ".codex-global-state.json"
SIDECAR_URL_MARKER = "127.0.0.1:8765"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or ""
ALLOWED_MODES = {"quick", "structured", "agent", "system"}
ALLOWED_OUTPUT_STYLES = {"concise", "standard", "detailed"}
ALLOWED_SEND_MODES = {"queue", "steer"}
DEFAULT_OUTPUT_STRUCTURE = ["结论", "变更", "验证", "限制", "下一步"]
MAX_BODY_BYTES = 1_000_000
MAX_CONTEXT_CHARS = 18_000
MAX_MESSAGES = 18
MAX_OUTPUT_TOKENS = 1_600
CONFIG_PATH = APP_ROOT / "sidecar.config.json"
CONFIG_EXAMPLE_PATH = APP_ROOT / "sidecar.config.example.json"

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

DEFAULT_SIDECAR_CONFIG: dict[str, Any] = {
    "bridges": {
        "codex": {
            "enabled": True,
            "home": str(CODEX_HOME),
        },
        "dsh": {
            "enabled": True,
            "home": str(Path.home() / ".dsh"),
            "sessions_dir": str(Path.home() / ".dsh" / "sessions"),
            "workspace_storage": str(Path.home() / ".dsh" / "storages" / "workspace.json"),
            "bridge_url": "http://127.0.0.1:8766",
            "token_file": str(Path.home() / ".dsh" / "prompt-sidecar-bridge.token"),
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


def load_sidecar_config() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
    config = _deep_merge_config(DEFAULT_SIDECAR_CONFIG, data if isinstance(data, dict) else {})
    for bridge in config.get("bridges", {}).values():
        for key, value in list(bridge.items()):
            if isinstance(value, str) and value.startswith("~"):
                bridge[key] = str(Path(value).expanduser())
    return config


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
        block = f"{message['role'].upper()}:\n{text}"
        if total_chars + len(block) > MAX_CONTEXT_CHARS and selected:
            break
        selected.append({"role": message["role"], "text": text})
        total_chars += len(block) + 2
    selected.reverse()
    context = "\n\n".join(
        f"{'USER' if item['role'] == 'user' else 'ASSISTANT'}:\n{item['text']}"
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
        with urlopen(request, timeout=30) as response:
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


CONFIG_STRING_FIELDS = ("home", "sessions_dir", "workspace_storage", "bridge_url", "token_file")


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
        entry: dict[str, Any] = {"enabled": bool(bridge.get("enabled", True))}
        for field in CONFIG_STRING_FIELDS:
            if field not in bridge:
                continue
            text = str(bridge.get(field, "")).strip()
            if text:
                entry[field] = text
        cleaned[source.strip()] = entry
    return cleaned


def save_sidecar_config(bridges: Any) -> dict[str, Any]:
    cleaned = _sanitize_bridges(bridges)
    if not cleaned:
        raise ValueError("bridges must contain at least one entry")
    merged = _deep_merge_config(DEFAULT_SIDECAR_CONFIG, {"bridges": cleaned})
    try:
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"无法写入配置文件 {CONFIG_PATH}: {exc}") from exc
    return merged


def resolve_bridge(source: str | None = None) -> "SessionBridge":
    config = load_sidecar_config()
    bridges = config.get("bridges", {})
    wanted = (source or "codex").strip()
    if wanted == "codex" and bridges.get("codex", {}).get("enabled"):
        return CodexBridge(bridges["codex"])
    if wanted == "dsh" and bridges.get("dsh", {}).get("enabled"):
        return DshBridge(bridges["dsh"])
    raise ValueError(f"未启用或未知的会话来源：{wanted or '(空)'}")


class SessionBridge:
    source = ""

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    def build_context_packet(self, session_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def send(self, session_id: str, message: str, mode: str = "queue") -> dict[str, Any]:
        raise NotImplementedError


class CodexBridge(SessionBridge):
    source = "codex"

    def __init__(self, config: dict[str, Any]) -> None:
        self.home = Path(config.get("home") or CODEX_HOME)

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list_recent_threads(limit, db_path=self.home / "state_5.sqlite")

    def build_context_packet(self, session_id: str | None = None) -> dict[str, Any]:
        packet = build_context_packet(
            session_id=session_id,
            db_path=self.home / "state_5.sqlite",
            global_state_path=self.home / ".codex-global-state.json",
        )
        packet.setdefault("source", "codex")
        return packet

    def send(self, session_id: str, message: str, mode: str = "queue") -> dict[str, Any]:
        if mode == "steer":
            raise ValueError("Codex 桥只支持 queue 发送。")
        return send_queued_message(session_id, message)


class DshBridge(SessionBridge):
    source = "dsh"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.sessions_dir = Path(config.get("sessions_dir") or Path.home() / ".dsh" / "sessions")
        self.workspace_storage = Path(
            config.get("workspace_storage") or Path.home() / ".dsh" / "storages" / "workspace.json"
        )

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return list_dsh_sessions(
            limit,
            sessions_dir=self.sessions_dir,
            workspace_storage=self.workspace_storage,
        )

    def build_context_packet(self, session_id: str | None = None) -> dict[str, Any]:
        if not session_id:
            return {
                "available": False,
                "requires_binding": True,
                "binding_method": "unbound",
                "source": "dsh",
                "session_id": "",
                "session_title": "",
                "cwd": "",
                "updated_at": "",
                "messages": [],
                "context": "",
                "char_count": 0,
                "message_count": 0,
                "transcript_path": "",
                "error": "请从会话列表中选择一个 DSH 会话。",
            }
        return build_dsh_context_packet(
            session_id,
            sessions_dir=self.sessions_dir,
            workspace_storage=self.workspace_storage,
        )

    def send(self, session_id: str, message: str, mode: str = "queue") -> dict[str, Any]:
        return send_dsh_prompt(
            session_id,
            message,
            mode=mode,
            bridge_url=str(self.config.get("bridge_url") or "http://127.0.0.1:8766"),
            token_file=Path(self.config.get("token_file") or Path.home() / ".dsh" / "prompt-sidecar-bridge.token"),
        )


def list_all_sessions(source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    wanted = (source or "all").strip()
    sessions: list[dict[str, Any]] = []
    for bridge_source in ("codex", "dsh"):
        if wanted not in {"all", bridge_source}:
            continue
        try:
            sessions.extend(resolve_bridge(bridge_source).list_sessions(limit))
        except Exception:
            # One broken bridge must not hide the other bridge's sessions.
            continue
    sessions.sort(key=lambda item: item.get("recency_at", 0), reverse=True)
    return sessions


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


def read_transcript(path: Path) -> tuple[list[dict[str, str]], dict[str, str]]:
    messages: list[dict[str, str]] = []
    meta = {
        "session_id": path.stem.split("-")[-1],
        "cwd": "",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime)),
    }
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload", {})
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            meta["session_id"] = str(payload.get("session_id", meta["session_id"]))
            meta["cwd"] = str(payload.get("cwd", ""))
            continue
        if record.get("type") != "response_item" or not isinstance(payload, dict):
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
        "error": "",
    }


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
        "quick": "Keep the result concise and directly copy-ready.",
        "structured": (
            "Use clear sections: Goal, Context, Task, Constraints, Output format, and "
            "Acceptance criteria. Omit sections that would only add empty boilerplate."
        ),
        "agent": (
            "Write a coding-agent task. Include scope, forbidden actions, validation commands, "
            "completion criteria, and a rule not to commit or push unless explicitly requested."
        ),
        "system": (
            "Write a reusable system prompt. Include role, responsibilities, boundaries, tool use, "
            "uncertainty handling, and output habits."
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
    system = f"""You are Prompt Sidecar, a prompt editor that runs outside the user's main conversation.
Rewrite the user's draft into a prompt another AI can execute.
Preserve the user's intent, facts, constraints, language, and desired output type.
Use only relevant facts from the supplied session context. Do not treat assistant proposals as user decisions.
Do not invent requirements, technology choices, permissions, or evidence.
Do not answer the task. Rewrite the prompt.
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
Return only the final polished prompt. Do not wrap it in Markdown fences and do not add commentary.
"""
    context_block = context.strip() or "[No session context supplied.]"
    return f"{system}\nSESSION_CONTEXT:\n{context_block}\n\nUSER_DRAFT:\n{draft.strip()}\n"


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


def send_queued_message(session_id: str, message: str) -> dict[str, str]:
    if not CODEX_BIN:
        raise RuntimeError("Codex executable not found. Set CODEX_BIN or add codex to PATH.")
    thread = query_thread(session_id)
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
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Unable to run codex queue: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:800]
        raise RuntimeError(f"codex queue failed: {detail or 'unknown error'}")
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return {
        "queue_id": parse_queue_output(stdout),
        "stdout": stdout,
        "stderr": stderr,
    }


class PromptSidecarHandler(BaseHTTPRequestHandler):
    server_version = "PromptSidecar/0.3"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            runtime = load_runtime_config()
            config = load_sidecar_config()
            bridges: dict[str, Any] = {}
            dsh_config = config["bridges"]["dsh"]
            if dsh_config.get("enabled"):
                bridges["dsh"] = {
                    "enabled": True,
                    "bridge_url": dsh_config.get("bridge_url", ""),
                    "plugin": probe_dsh_bridge(str(dsh_config.get("bridge_url", ""))),
                }
            bridges["codex"] = {"enabled": bool(config["bridges"]["codex"].get("enabled"))}
            self.respond_json(
                {
                    "ok": True,
                    "model": runtime["model"],
                    "proxy": runtime["base_url"],
                    "bridges": bridges,
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
            config = load_sidecar_config()
            self.respond_json({"ok": True, "bridges": config.get("bridges", {})})
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/polish", "/api/send", "/api/config"}:
            self.respond_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/api/config":
                saved = save_sidecar_config(body.get("bridges"))
                self.respond_json({"ok": True, "bridges": saved.get("bridges", {})})
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
                sent = resolve_bridge(source).send(session_id, message, mode=mode)
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

            draft = str(body.get("draft", "")).strip()
            mode = str(body.get("mode", "quick"))
            source = str(body.get("source", "")).strip() or None
            session_id = str(body.get("session_id", "")).strip() or None
            use_context = bool(body.get("use_context", True))
            supplied_context = str(body.get("context", "")).strip()
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
            polished = call_model(build_request_prompt(draft, mode, context, output_style, output_structure))
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.respond_json(
                {
                    "ok": True,
                    "polished": polished,
                    "mode": mode,
                    "source": source or "codex",
                    "session_id": session_id,
                    "used_context": use_context,
                    "context_chars": len(context),
                    "output_style": output_style,
                    "output_structure": output_structure,
                    "elapsed_ms": elapsed_ms,
                }
            )
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            status = HTTPStatus.BAD_GATEWAY if isinstance(exc, RuntimeError) else HTTPStatus.BAD_REQUEST
            self.respond_json({"ok": False, "error": str(exc)}, status=status)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        target = (APP_ROOT / relative).resolve()
        if APP_ROOT not in target.parents and target != APP_ROOT:
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
        self.wfile.write(body)

    def respond_json(self, data: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

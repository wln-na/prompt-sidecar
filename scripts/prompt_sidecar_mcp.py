#!/usr/bin/env python3
"""Read-only MCP bridge for connecting 豆包工作 to Prompt Sidecar.

The server never sends messages to a session. It only exposes local Sidecar
health, session discovery, and context reads over the MCP stdio transport.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


SIDECAR_URL = "http://127.0.0.1:8765"

TOOLS = [
    {
        "name": "prompt_sidecar_health",
        "description": "读取 Prompt Sidecar 的模型和桥接健康状态。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prompt_sidecar_sessions",
        "description": "只读列出 Prompt Sidecar 当前可发现的会话。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "可选的桥接 source"},
            },
        },
    },
    {
        "name": "prompt_sidecar_context",
        "description": "按显式 source 和 session_id 只读读取会话上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["source", "session_id"],
        },
    },
]


def sidecar_json(path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
    suffix = f"?{urlencode(query)}" if query else ""
    with urlopen(f"{SIDECAR_URL}{path}{suffix}", timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Sidecar returned a non-object response.")
    return payload


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "prompt_sidecar_health":
        return sidecar_json("/api/health")
    if name == "prompt_sidecar_sessions":
        source = str(arguments.get("source") or "")
        return sidecar_json("/api/sessions", {"source": source} if source else None)
    if name == "prompt_sidecar_context":
        source = str(arguments.get("source") or "").strip()
        session_id = str(arguments.get("session_id") or "").strip()
        if not source or not session_id:
            raise ValueError("source and session_id are required")
        return sidecar_json("/api/context", {"source": source, "session_id": session_id})
    raise ValueError(f"Unknown read-only tool: {name}")


def write_message(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict) or "method" not in request:
            continue
        method = request["method"]
        request_id = request.get("id")
        if method == "initialize":
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "prompt-sidecar-readonly", "version": "0.1.0"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            write_message({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params") or {}
            try:
                result = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
                content = [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]
                write_message({"jsonrpc": "2.0", "id": request_id, "result": {"content": content}})
            except Exception as exc:
                write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )
        elif request_id is not None:
            write_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unsupported method: {method}"},
                }
            )


if __name__ == "__main__":
    main()

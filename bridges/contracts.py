"""Typed contracts shared by Prompt Sidecar bridge adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Literal, NotRequired, TypedDict


BridgeConfig = dict[str, Any]
ContextMode = Literal["full", "best_effort", "metadata", "none"]
SendMode = Literal["queue", "steer"]
ModelConfigMode = Literal["bridge_default", "explicit"]
ModelSelectionMode = Literal["dynamic", "fixed", "unsupported"]
SidecarMode = Literal["standalone", "agentxray"]


class SessionRecord(TypedDict, total=False):
    source: str
    session_id: str
    native_session_id: str
    title: str
    raw_title: str
    cwd: str
    updated_at: str
    recency_at: int
    archived: bool
    status: str


class ModelOption(TypedDict):
    id: str
    label: str


class ModelSelectionConfig(TypedDict):
    mode: ModelConfigMode
    model_id: str


class BridgeSendRequest(TypedDict):
    source: str
    session_id: str
    message: str
    mode: SendMode
    model_id: str


class IntegrationTarget(TypedDict):
    platform: str
    agent: str | None
    session_id: str
    cwd: str | None


class AgentXRayHandoff(TypedDict):
    handoff_id: str
    attention_id: str
    target: IntegrationTarget
    target_fingerprint: str
    snapshot_version: str
    expires_at: str
    session_refs: list[dict[str, Any]]


class ModelSelectionCapability(TypedDict):
    mode: ModelSelectionMode
    default_model_id: str
    fixed_model_id: str
    fixed_model_label: str
    options: list[ModelOption]
    allow_custom: bool
    reason: str


class BridgeCapabilities(TypedDict):
    list_sessions: bool
    read_context: bool
    send: bool
    send_modes: list[str]
    context_mode: ContextMode
    model_selection: ModelSelectionCapability


class BridgeHealth(TypedDict, total=False):
    available: bool | None
    status: str
    message: str


class ContextPacket(TypedDict, total=False):
    available: bool
    requires_binding: bool
    binding_method: str
    source: str
    session_id: str
    session_title: str
    cwd: str
    updated_at: str
    messages: list[dict[str, str]]
    context: str
    char_count: int
    message_count: int
    transcript_path: str
    error: str
    model_id: str
    model_source: str
    model_timestamp: str
    metadata_only: NotRequired[bool]


class SendResult(TypedDict, total=False):
    accepted: bool
    mode: str
    queue_id: str
    run_id: str
    delivery: str
    stop_reason: str
    effective_model: str
    result: Any
    stdout: str
    stderr: str


class BridgeAuthError(RuntimeError):
    """Authentication or authorization failed for a bridge."""


class BridgeUnavailableError(RuntimeError):
    """The bridge transport or runtime is temporarily unavailable."""


class SessionBridge(ABC):
    source = ""
    adapter = ""

    def __init__(self, config: BridgeConfig | None = None) -> None:
        self.config = config or {}

    @abstractmethod
    def capabilities(self) -> BridgeCapabilities:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> BridgeHealth:
        raise NotImplementedError

    @abstractmethod
    def list_sessions(self, limit: int = 50) -> list[SessionRecord]:
        raise NotImplementedError

    @abstractmethod
    def build_context_packet(self, session_id: str | None = None) -> ContextPacket:
        raise NotImplementedError

    @abstractmethod
    def send(self, request: BridgeSendRequest) -> SendResult:
        raise NotImplementedError


def empty_context_packet(
    source: str,
    error: str,
    requires_binding: bool = False,
) -> ContextPacket:
    return {
        "available": False,
        "requires_binding": requires_binding,
        "binding_method": "unbound" if requires_binding else "explicit-session-id",
        "source": source,
        "session_id": "",
        "session_title": "",
        "cwd": "",
        "updated_at": "",
        "messages": [],
        "context": "",
        "char_count": 0,
        "message_count": 0,
        "transcript_path": "",
        "model_id": "",
        "model_source": "",
        "model_timestamp": "",
        "error": error,
    }


def retag_sessions(sessions: list[SessionRecord], source: str) -> list[SessionRecord]:
    return [{**session, "source": source} for session in sessions]


def retag_context(packet: ContextPacket, source: str) -> ContextPacket:
    packet["source"] = source
    return packet


def dynamic_model_selection(
    *,
    default_model_id: str = "session-default",
    options: list[ModelOption] | None = None,
    allow_custom: bool = True,
    reason: str = "",
) -> ModelSelectionCapability:
    return {
        "mode": "dynamic",
        "default_model_id": default_model_id,
        "fixed_model_id": "",
        "fixed_model_label": "",
        "options": options or [],
        "allow_custom": allow_custom,
        "reason": reason,
    }


def fixed_model_selection(
    fixed_model_id: str,
    fixed_model_label: str,
    reason: str = "",
) -> ModelSelectionCapability:
    return {
        "mode": "fixed",
        "default_model_id": "",
        "fixed_model_id": fixed_model_id,
        "fixed_model_label": fixed_model_label,
        "options": [],
        "allow_custom": False,
        "reason": reason,
    }


def unsupported_model_selection(reason: str) -> ModelSelectionCapability:
    return {
        "mode": "unsupported",
        "default_model_id": "",
        "fixed_model_id": "",
        "fixed_model_label": "",
        "options": [],
        "allow_custom": False,
        "reason": reason,
    }

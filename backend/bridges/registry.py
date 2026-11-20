"""Adapter registry for Prompt Sidecar bridges."""

from __future__ import annotations

from collections.abc import Callable

from .contracts import BridgeConfig, SessionBridge


BridgeFactory = Callable[[BridgeConfig], SessionBridge]


class BridgeRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, BridgeFactory] = {}

    def register(
        self,
        adapter: str,
        factory: BridgeFactory,
        *,
        replace: bool = False,
    ) -> None:
        key = adapter.strip()
        if not key:
            raise ValueError("adapter name must be non-empty")
        if key in self._factories and not replace:
            raise ValueError(f"adapter already registered: {key}")
        self._factories[key] = factory

    def create(self, source: str, config: BridgeConfig) -> SessionBridge:
        adapter = str(config.get("adapter") or source).strip()
        factory = self._factories.get(adapter)
        if factory is None:
            raise ValueError(f"未知的桥接适配器：{adapter}")
        bridge = factory(config)
        bridge.source = source
        return bridge

    def adapter_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


BRIDGE_REGISTRY = BridgeRegistry()

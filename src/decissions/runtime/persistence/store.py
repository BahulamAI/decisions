"""Host persistence protocol and bounded cache; no database ownership."""
from __future__ import annotations
from collections import OrderedDict
from typing import Any, Protocol
from decissions.contracts.models import DecisionRecord

class Store(Protocol):
    """What the engine, API and tools need from a decision store."""

    async def get_by_hash(self, tenant_id: str, input_hash: str) -> DecisionRecord | None: ...
    async def get(self, tenant_id: str, decision_id: str) -> DecisionRecord | None: ...
    async def save(self, d: DecisionRecord) -> DecisionRecord: ...
    async def add_shadow(self, decision_id: str, domain: str, selector: str, option_id: str | None,
                         agreed: bool | None, latency_ms: float, error: str | None, tenant_id: str = "") -> None: ...
    async def add_override(self, tenant_id: str, decision_id: str, frm: str, to: str, actor: str) -> str: ...
    async def add_feedback(self, tenant_id: str, decision_id: str, verdict: str,
                           preferred: str | None, severity: str | None, actor: str) -> str: ...
    async def review_feedback(self, feedback_id: str, status: str, intent: str | None = None,
                              preferred: str | None = None) -> bool: ...
    async def feedback(self, status: str) -> list[dict[str, Any]]: ...
    async def shadow_agreement(self) -> dict[str, dict[str, dict[str, float]]]: ...
    async def ping(self) -> bool: ...
    async def aclose(self) -> None: ...

class LRUCache:
    """Tiny in-process cache in front of the store. Key already contains tenant + all versions."""

    def __init__(self, max_entries: int) -> None:
        self.max, self._d = max_entries, OrderedDict[str, DecisionRecord]()

    def get(self, key: str) -> DecisionRecord | None:
        if key in self._d:
            self._d.move_to_end(key)
            return self._d[key]
        return None

    def put(self, key: str, value: DecisionRecord) -> None:
        self._d[key] = value
        self._d.move_to_end(key)
        while len(self._d) > self.max:
            self._d.popitem(last=False)

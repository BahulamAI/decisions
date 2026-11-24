"""Keep Jev spend near zero while developing and testing.

ResponseCache
    off     every call goes to OpenRouter (production)
    record  call OpenRouter once per distinct payload, save the response to disk
    replay  never call OpenRouter; serve saved responses; a missing one is an error
  The key is the SHA-256 of the exact request payload (model + state + questions), so any change to
  a domain's questions, options or the pinned model naturally misses the cache. Eval runs in CI use
  replay: unlimited reruns, zero cost, fully reproducible.

BudgetGuard
  A soft, process-local daily cap from `usage.cost` on each response (UTC day). When it is reached the
  selector raises SelectorBudgetExceeded and the engine serves the domain's rules/safe default. The
  hard cap belongs on the OpenRouter key itself (limit + limit_reset), which also protects you across
  replicas and laptops.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
UTC = timezone.utc
from pathlib import Path
from typing import Any


def payload_key(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


class CacheMiss(LookupError):
    pass


class ResponseCache:
    def __init__(self, mode: str, directory: str | Path) -> None:
        if mode not in ("off", "record", "replay"):
            raise ValueError(f"bad cache mode {mode}")
        self.mode, self.dir = mode, Path(directory)
        if mode != "off":
            self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self.mode == "off":
            return None
        p = self._path(payload_key(payload))
        if p.exists():
            return json.loads(p.read_text())
        if self.mode == "replay":
            raise CacheMiss("no recorded Jev response for this payload; run once with DECISION_JEV_CACHE_MODE=record")
        return None

    def put(self, payload: dict[str, Any], body: dict[str, Any]) -> None:
        if self.mode == "record":
            self._path(payload_key(payload)).write_text(json.dumps(body, sort_keys=True))


class BudgetGuard:
    def __init__(self, daily_usd: float | None) -> None:
        self.daily_usd = daily_usd
        self._day = self._today()
        self.spent_usd = 0.0
        self.calls = 0
        self._lock = threading.Lock()

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self.spent_usd, self.calls = today, 0.0, 0

    def allow(self) -> bool:
        with self._lock:
            self._roll()
            return self.daily_usd is None or self.spent_usd < self.daily_usd

    def record(self, usage: dict[str, Any]) -> None:
        cost = usage.get("cost")
        with self._lock:
            self._roll()
            self.calls += 1
            if isinstance(cost, int | float) and cost > 0:
                self.spent_usd += float(cost)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._roll()
            return {"day_utc": self._day, "spent_usd": round(self.spent_usd, 6), "calls": self.calls,
                    "daily_budget_usd": self.daily_usd}


def estimate_tokens(payload: dict[str, Any]) -> int:
    """Rough input-token estimate (about 4 characters per token for JSON/English)."""
    return max(1, len(json.dumps(payload, separators=(",", ":"))) // 4)

"""State that every replica must see the same way (D13): Jev budget, breaker, response cache and rate limits.

Why it lives outside the process: with two replicas, a budget kept in memory is two budgets, a tripped breaker
protects only the replica that saw the failures, and a rate limit is multiplied by the replica count.

Two implementations of one interface:
* LocalState  in-process. Used without Redis (tests, one replica) and as the fallback while Redis is down.
* RedisState  shared. Every operation is atomic (MULTI or a Lua script) and bounded by a short timeout.

Failure policy when Redis is unreachable, chosen per operation (the service keeps serving either way):
* budget          fail CLOSED: no Jev spend we cannot count. The rules answer is served instead.
* breaker flag    fall back to this replica's own breaker.
* response cache  a miss.
* rate limit      fall back to this replica's own bucket: still bounded, never an outage for users.
After an error Redis is skipped for a few seconds, so an outage costs one timeout, not one per request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
UTC = timezone.utc
from typing import Any, TypeVar

from .observability import METRICS, log

logger = logging.getLogger("decision.shared")
T = TypeVar("T")
_DAY_S = 86_400
_BACKOFF_S = 5.0

# A token bucket, run atomically inside Redis with Redis's own clock, so replicas never disagree on time.
_RATE_LIMIT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1e6
local rate, burst = tonumber(ARGV[1]), tonumber(ARGV[2])
local s = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(s[1]) or burst
local ts = tonumber(s[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(burst / rate * 1000) + 1000)
return {allowed, tostring(tokens)}
"""


def _day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _hide(value: str) -> str:
    """Tenant and caller ids become key parts as short hashes: no ids in Redis, no key injection."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class LocalState:
    """One process's view. Correct for one replica; per-replica (and documented as such) for more."""

    def __init__(self, daily_usd: float | None = None, tenant_daily_usd: float | None = None,
                 rate_per_s: float = 0.0, burst: int = 40) -> None:
        self.daily_usd, self.tenant_daily_usd, self.rate, self.burst = daily_usd, tenant_daily_usd, rate_per_s, burst
        self._spent: dict[str, float] = {}
        self._calls: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._buckets: dict[str, tuple[float, float]] = {}

    async def budget_allow(self, tenant: str) -> bool:
        d = _day()
        if self.daily_usd is not None and self._spent.get(d, 0.0) >= self.daily_usd:
            return False
        return self.tenant_daily_usd is None or self._spent.get(f"{d}:{_hide(tenant)}", 0.0) < self.tenant_daily_usd

    async def budget_record(self, tenant: str, cost: float) -> None:
        d = _day()
        for key in (d, f"{d}:{_hide(tenant)}"):
            self._spent[key] = self._spent.get(key, 0.0) + cost
        self._calls[d] = self._calls.get(d, 0) + 1

    async def budget_snapshot(self) -> dict[str, Any]:
        d = _day()
        return {"day_utc": d, "spent_usd": round(self._spent.get(d, 0.0), 6), "calls": self._calls.get(d, 0),
                "daily_budget_usd": self.daily_usd, "shared": False}

    async def breaker_open(self, name: str) -> bool:
        return self._open_until.get(name, 0.0) > time.monotonic()

    async def breaker_trip(self, name: str, seconds: float) -> None:
        self._open_until[name] = time.monotonic() + seconds

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        hit = self._cache.get(key)
        return hit[1] if hit and hit[0] > time.monotonic() else None

    async def cache_put(self, key: str, body: dict[str, Any], ttl_s: int) -> None:
        if ttl_s > 0:
            self._cache[key] = (time.monotonic() + ttl_s, body)

    async def take_token(self, tenant: str, caller: str) -> tuple[bool, int]:
        """Token bucket: `rate` tokens per second up to `burst`. Returns (allowed, retry_after_s)."""
        if self.rate <= 0:
            return True, 0
        key, now = f"{_hide(tenant)}:{_hide(caller)}", time.monotonic()
        tokens, ts = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - ts) * self.rate)
        allowed = tokens >= 1
        self._buckets[key] = (tokens - 1 if allowed else tokens, now)
        return allowed, 0 if allowed else max(1, math.ceil((1 - tokens) / self.rate))

    async def ping(self) -> bool | None:
        return None  # nothing shared to check

    async def aclose(self) -> None:
        return None


class RedisState(LocalState):
    """Shared across replicas through Redis. Inherits LocalState as the fallback for every operation."""

    def __init__(self, url: str, prefix: str, timeout_ms: int, **local: Any) -> None:
        super().__init__(**local)
        import redis.asyncio as redis  # imported only when configured

        timeout = timeout_ms / 1000
        self.r = redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout,
                                health_check_interval=30, decode_responses=True)
        self.p = prefix
        self._skip_until = 0.0
        self._bucket = self.r.register_script(_RATE_LIMIT_LUA)

    async def _try(self, op: str, call: Callable[[], Awaitable[T]], fallback: Callable[[], Awaitable[T]]) -> T:
        if time.monotonic() < self._skip_until:
            return await fallback()
        try:
            return await call()
        except Exception as e:  # noqa: BLE001 - any Redis failure degrades, it never fails the request
            self._skip_until = time.monotonic() + _BACKOFF_S
            METRICS.inc("shared_state_errors", op=op)
            log(logger, "shared_state_unavailable", op=op, error=type(e).__name__)
            return await fallback()

    def _keys(self, tenant: str) -> tuple[str, str]:
        d = _day()
        return f"{self.p}budget:{d}", f"{self.p}budget:{d}:t:{_hide(tenant)}"

    async def budget_allow(self, tenant: str) -> bool:
        async def shared() -> bool:
            total, mine = await self.r.mget(*self._keys(tenant))
            if self.daily_usd is not None and float(total or 0) >= self.daily_usd:
                return False
            return self.tenant_daily_usd is None or float(mine or 0) < self.tenant_daily_usd

        async def closed() -> bool:
            return False  # spend we cannot count is spend we do not make

        return await self._try("budget_allow", shared, closed)

    async def budget_record(self, tenant: str, cost: float) -> None:
        async def shared() -> None:
            total, mine = self._keys(tenant)
            async with self.r.pipeline(transaction=True) as tx:  # MULTI/EXEC: both counters or neither
                tx.incrbyfloat(total, cost).incrbyfloat(mine, cost).incr(f"{total}:calls")
                tx.expire(total, 2 * _DAY_S).expire(mine, 2 * _DAY_S).expire(f"{total}:calls", 2 * _DAY_S)
                await tx.execute()

        await self._try("budget_record", shared, lambda: LocalState.budget_record(self, tenant, cost))

    async def budget_snapshot(self) -> dict[str, Any]:
        async def shared() -> dict[str, Any]:
            total, calls = await self.r.mget(f"{self.p}budget:{_day()}", f"{self.p}budget:{_day()}:calls")
            return {"day_utc": _day(), "spent_usd": round(float(total or 0), 6), "calls": int(calls or 0),
                    "daily_budget_usd": self.daily_usd, "shared": True}

        return await self._try("budget_snapshot", shared, lambda: LocalState.budget_snapshot(self))

    async def breaker_open(self, name: str) -> bool:
        async def shared() -> bool:
            return bool(await self.r.exists(f"{self.p}breaker:{name}"))

        return await self._try("breaker_open", shared, lambda: LocalState.breaker_open(self, name))

    async def breaker_trip(self, name: str, seconds: float) -> None:
        async def shared() -> None:
            await self.r.set(f"{self.p}breaker:{name}", "open", px=max(1, int(seconds * 1000)))

        await LocalState.breaker_trip(self, name, seconds)
        await self._try("breaker_trip", shared, lambda: LocalState.breaker_trip(self, name, seconds))

    async def cache_get(self, key: str) -> dict[str, Any] | None:
        async def shared() -> dict[str, Any] | None:
            raw = await self.r.get(f"{self.p}jev:{key}")
            return json.loads(raw) if raw else None

        async def miss() -> None:
            return None

        return await self._try("cache_get", shared, miss)

    async def cache_put(self, key: str, body: dict[str, Any], ttl_s: int) -> None:
        async def shared() -> None:
            await self.r.set(f"{self.p}jev:{key}", json.dumps(body), ex=ttl_s)

        async def skip() -> None:
            return None

        if ttl_s > 0:
            await self._try("cache_put", shared, skip)

    async def take_token(self, tenant: str, caller: str) -> tuple[bool, int]:
        if self.rate <= 0:
            return True, 0

        async def shared() -> tuple[bool, int]:
            allowed, tokens = await self._bucket(keys=[f"{self.p}rl:{_hide(tenant)}:{_hide(caller)}"],
                                                 args=[self.rate, self.burst])
            ok = int(allowed) == 1
            return ok, 0 if ok else max(1, math.ceil((1 - float(tokens)) / self.rate))

        return await self._try("rate_limit", shared, lambda: LocalState.take_token(self, tenant, caller))

    async def ping(self) -> bool | None:
        try:
            return bool(await self.r.ping())
        except Exception:  # noqa: BLE001 - readiness reports, it never raises
            return False

    async def aclose(self) -> None:
        await self.r.aclose()


def build_shared(settings: Any) -> LocalState:
    local = {"daily_usd": settings.jev_daily_budget_usd, "tenant_daily_usd": settings.jev_tenant_daily_budget_usd,
             "rate_per_s": settings.rate_limit_per_s, "burst": settings.rate_limit_burst}
    if settings.redis_url is None:
        return LocalState(**local)
    return RedisState(settings.redis_url.get_secret_value(), settings.redis_prefix, settings.redis_timeout_ms,
                      **local)

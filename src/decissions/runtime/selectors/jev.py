"""TypeSafe Jev through a configured Decisions endpoint, for any candidate-selection domain.

Wire format (TypeSafe native schema, passed through by OpenRouter):
    request  {"model", "state", "questions": {id: {"type": "choice"|"noul", "instructions", "criteria"}}}
    response {"model": "<resolved release>", "answers": {id: {...}}, "usage": {..., "cost"}}

One request asks everything, because Jev evaluates questions in parallel (tokens, not latency):
  * a Choice over the domain's eligible options (criteria = server-authored descriptions),
  * a companion Noul "at least one option fits" (TypeSafe advises this over a none-of-the-above option),
  * the domain's signal Nouls (become MODEL_JUDGMENT reason codes and distillation features).

Trust boundary: instructions and criteria are authored by our code. Untrusted text (a user message,
an agent's tool arguments) appears only inside `state` under a key that says it is untrusted.
The alpha API is isolated here: nothing outside this file knows its shape.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx

from decissions.contracts.domain import DecisionDomain
from decissions.contracts.models import DecisionContext, Eligibility, SelectorOutput
from ..shared import LocalState
from .base import (
    SelectorBudgetExceeded,
    SelectorCircuitOpen,
    SelectorError,
    SelectorInvalidResponse,
    SelectorRateLimited,
    SelectorTimeout,
)
from .jev_cost import BudgetGuard, CacheMiss, ResponseCache, payload_key
from decissions.adapters.jev import _validate_endpoint

CHOICE_ID = "choice"
FIT_ID = "faithful_option_exists"
SIGNAL_PREFIX = "signal_"
MAX_OPTIONS = 255  # Jev Choice ceiling; larger catalogs need a hierarchical Choice


class CircuitBreaker:
    """Opens after N consecutive failures; half-opens after a cool-down. With shared state, the replica that trips
    it also raises a shared flag for the same cool-down, so every replica stops calling a failing provider."""

    def __init__(self, threshold: int, reset_s: float) -> None:
        self.threshold, self.reset_s = threshold, reset_s
        self.failures, self.opened_at = 0, 0.0

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        return time.monotonic() - self.opened_at >= self.reset_s  # half-open probe

    def record(self, ok: bool) -> bool:
        """Returns True when this failure opened the breaker."""
        if ok:
            self.failures = 0
            return False
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
            return self.failures == self.threshold
        return False


def build_state(domain: DecisionDomain, ctx: DecisionContext) -> dict[str, Any]:
    state: dict[str, Any] = {"decision": domain.description, "facts": domain.model_state(ctx)}
    text = domain.untrusted_text(ctx.input)
    if text:
        state["untrusted_user_request"] = text  # data, never instructions
    return state


def build_questions(domain: DecisionDomain, elig: Eligibility, include_signals: bool = True) -> dict[str, Any]:
    if not 1 <= len(elig.options) <= MAX_OPTIONS:
        raise SelectorInvalidResponse('candidate count must be between 1 and 255')
    options = elig.options
    criteria = {o.option_id: {**o.description, "caveats": ", ".join(o.disclosures) or "none"} for o in options}
    q: dict[str, Any] = {
        CHOICE_ID: {"type": "choice", "instructions": domain.choice_instructions, "criteria": criteria},
        FIT_ID: {
            "type": "noul",
            "instructions": {"question": "At least one of `options` handles this request faithfully.",
                             "options": {oid: v["what"] for oid, v in criteria.items()}},
            "criteria": {"true": "One option directly serves what is being asked for",
                         "false": "The request needs something none of the options provides, or is unclear"},
        },
    }
    for name, text in (domain.signal_questions() if include_signals else {}).items():
        q[f"{SIGNAL_PREFIX}{name}"] = {"type": "noul", "instructions": text}
    return q


def _prob(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, int | float) or not math.isfinite(x) or not 0 <= x <= 1:
        raise SelectorInvalidResponse(f"bad probability {x!r}")
    return float(x)


def parse_response(body: Any, elig: Eligibility, *, model: str | None = None,
                   signal_names: set[str] | None = None) -> SelectorOutput:
    try:
        release = body['model']
        if not isinstance(release, str) or not release or (model is not None and
                not (release == model or release.startswith(model + '-'))):
            raise SelectorInvalidResponse('unexpected model release')
        answers = body["answers"]
        choice = answers[CHOICE_ID]
        if choice['type'] != 'choice' or answers[FIT_ID]['type'] != 'noul':
            raise SelectorInvalidResponse('wrong answer type')
        expected = {CHOICE_ID, FIT_ID} | {SIGNAL_PREFIX + name for name in (signal_names or set())}
        if set(answers) != expected:
            raise SelectorInvalidResponse('missing or unexpected answers')
        picked = choice["choice"]
        probs = {str(k): _prob(v) for k, v in choice["probabilities"].items()}
        conf = _prob(choice["confidence"]) if choice.get("confidence") is not None else None
        fit = _prob(answers[FIT_ID]["noul"])
        if any(v.get('type') != 'noul' for k, v in answers.items() if k.startswith(SIGNAL_PREFIX)):
            raise SelectorInvalidResponse('wrong signal type')
        signals = {k.removeprefix(SIGNAL_PREFIX): _prob(v["noul"])
                   for k, v in answers.items() if k.startswith(SIGNAL_PREFIX)}
    except (KeyError, TypeError, AttributeError) as e:
        raise SelectorInvalidResponse(f"missing field: {e}") from e
    valid = {o.option_id for o in elig.options}
    if picked not in valid or set(probs) != valid:
        raise SelectorInvalidResponse("answer outside the option set")
    if abs(sum(probs.values()) - 1.0) > 0.02:
        raise SelectorInvalidResponse("probabilities not normalized")
    return SelectorOutput(option_id=picked, probabilities=probs, provider_confidence=conf, fit_probability=fit,
                          model_release=body.get("model"), signals=signals, usage=body.get("usage") or {})


class JevSelector:
    name = "hosted_model"

    def __init__(self, client: httpx.AsyncClient, api_key: str, url: str, model: str,
                 max_retries: int = 1, breaker: CircuitBreaker | None = None,
                 cache: ResponseCache | None = None, budget: BudgetGuard | None = None,
                 app_title: str = "Decissions", app_url: str = "",
                 include_signals: bool = True, shared: LocalState | None = None, cache_ttl_s: int = 0) -> None:
        if not api_key or not model:
            raise ValueError('credentials and pinned model are required')
        self.client, self.api_key, self.url, self.model = client, api_key, _validate_endpoint(url), model
        self.include_signals = include_signals  # intent yes/no questions; off saves tokens when serving
        self.max_retries = max_retries
        self.breaker = breaker or CircuitBreaker(5, 30.0)
        self.cache = cache or ResponseCache("off", ".jev-cache")
        self.budget = budget or BudgetGuard(None)
        self.shared, self.cache_ttl_s = shared, cache_ttl_s  # budget, breaker and cache across replicas
        # OpenRouter app attribution: separates this service's usage on a shared organisation key.
        self.attribution = {"X-Title": app_title, **({"HTTP-Referer": app_url} if app_url else {})}

    def release_for(self, domain: DecisionDomain) -> str:
        return self.model

    async def select(self, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                     budget_s: float) -> SelectorOutput:
        payload = {"model": self.model, "state": build_state(domain, ctx),
                   "questions": build_questions(domain, elig, self.include_signals)}
        try:
            cached = self.cache.get(payload)  # record/replay: zero-cost reruns in dev and CI
        except CacheMiss as e:
            raise SelectorInvalidResponse(str(e)) from e
        key = payload_key(payload)
        if cached is None and self.shared is not None and self.cache_ttl_s:
            cached = await self.shared.cache_get(key)
        if cached is not None:
            out = parse_response(cached, elig, model=self.model,
                                 signal_names=set(domain.signal_questions()) if self.include_signals else set())
            out.model, out.usage = self.model, {**out.usage, "cost": 0.0, "cached": True}
            return out
        if not self.budget.allow() or (self.shared is not None and not await self.shared.budget_allow(ctx.tenant_id)):
            raise SelectorBudgetExceeded("daily Jev budget reached")
        if not self.breaker.allow() or (self.shared is not None and await self.shared.breaker_open("jev")):
            raise SelectorCircuitOpen("circuit open")
        try:
            return await self._call(payload, key, ctx, elig, budget_s)
        except SelectorError:
            if self.breaker.failures >= self.breaker.threshold and self.shared is not None:
                await self.shared.breaker_trip("jev", self.breaker.reset_s)
            raise

    async def _call(self, payload: dict[str, Any], key: str, ctx: DecisionContext, elig: Eligibility,
                    budget_s: float) -> SelectorOutput:
        deadline = time.monotonic() + budget_s
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0.05:
                self.breaker.record(False)
                raise SelectorTimeout("deadline exhausted")
            try:
                resp = await asyncio.wait_for(
                    self.client.post(self.url, json=payload, timeout=remaining,
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "X-Request-Id": ctx.request_id, **self.attribution}),
                    timeout=remaining)
            except (TimeoutError, httpx.TimeoutException) as e:
                self.breaker.record(False)
                raise SelectorTimeout(str(e) or "timeout") from e
            except httpx.TransportError as e:
                err: SelectorError = SelectorError(f"transport: {e}")
            else:
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                        out = parse_response(body, elig, model=self.model,
                            signal_names={k.removeprefix(SIGNAL_PREFIX) for k in payload['questions']
                                          if k.startswith(SIGNAL_PREFIX)})
                    except ValueError as e:
                        self.breaker.record(False)
                        raise SelectorInvalidResponse(str(e)) from e
                    self.breaker.record(True)
                    self.budget.record(out.usage)
                    self.cache.put(payload, body)
                    if self.shared is not None:
                        cost = out.usage.get("cost")
                        await self.shared.budget_record(ctx.tenant_id, float(cost) if isinstance(cost, int | float)
                                                        else 0.0)
                        await self.shared.cache_put(key, body, self.cache_ttl_s)
                    out.model = self.model
                    return out
                if resp.status_code in (402, 403):  # out of credits, key limit, or guardrail budget
                    raise SelectorBudgetExceeded(f"provider refused on budget/policy: {resp.status_code}")
                if resp.status_code == 429:
                    err = SelectorRateLimited("rate limited")
                elif resp.status_code >= 500:
                    err = SelectorError(f"provider {resp.status_code}")
                else:  # 4xx is our bug or an API change: never retry
                    self.breaker.record(False)
                    raise SelectorInvalidResponse(f"provider rejected request: {resp.status_code}")
            attempt += 1
            if attempt > self.max_retries:
                self.breaker.record(False)
                raise err
            await asyncio.sleep(min(0.1 * attempt, max(0.0, deadline - time.monotonic() - 0.1)))

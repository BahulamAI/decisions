"""The domain-agnostic decision engine.

    eligibility -> allowlist -> (user preference | single option | rules answer + serving selector)
    -> validate -> approval -> persist -> background shadows

Invariants (each has a test, for every domain):
1. The chosen option is always a member of the domain's eligible set.
2. Abstention or low confidence degrades to the domain's safe default. A selector that fails (timeout, error,
   budget, breaker) degrades to the rules answer when the domain allows it, otherwise to the safe default.
   HIGH-risk domains always take the safe default.
3. Hosted inference only for tenants allowed by that domain's policy; otherwise rules, never another vendor.
4. Shadow selectors never change the answer and never add latency.
5. Same tenant + same versioned input -> the stored decision (idempotent replay).
6. HIGH-risk domains flag approval_required when a model (not rules or the user) made the choice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from ..config import ResolvedPolicy, SelectorKind, Settings
from ..observability import METRICS, log
from ..persistence.store import LRUCache, Store
from ..selectors.base import Selector, SelectorError
from ..selectors.rules import RulesSelector
from decissions.contracts.domain import DecisionDomain
from decissions.contracts.models import (
    DecisionContext,
    DecisionRecord,
    Eligibility,
    FallbackReason,
    Ineligible,
    Option,
    RiskTier,
    SelectionSource,
    SelectorOutput,
    SubjectRef,
)

logger = logging.getLogger("decision.engine")
_STRONG_SIGNAL = 0.7
_SOURCE = {SelectorKind.RULES: SelectionSource.RULE, SelectorKind.HOSTED: SelectionSource.HOSTED,
           SelectorKind.IN_HOUSE: SelectionSource.IN_HOUSE}


@dataclass
class Engine:
    settings: Settings = field(default_factory=Settings)
    store: Store | None = None
    selectors: dict[SelectorKind, Selector] = field(default_factory=dict)
    cache: LRUCache = field(default_factory=lambda: LRUCache(10_000))
    _bg: set[asyncio.Task] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.selectors = {SelectorKind.RULES: RulesSelector(), **self.selectors}

    def serving_kind(self, domain: DecisionDomain, tenant_id: str, pol: ResolvedPolicy) -> SelectorKind:
        if self.settings.rules_only:  # global kill switch
            return SelectorKind.RULES
        kind = pol.serving
        if kind == SelectorKind.HOSTED and tenant_id not in pol.external_inference_tenants:
            return SelectorKind.RULES
        if kind == SelectorKind.IN_HOUSE and domain.scorer is None:
            return SelectorKind.RULES
        return kind if kind in self.selectors else SelectorKind.RULES

    async def decide(self, domain: DecisionDomain, ctx: DecisionContext | None = None, *,
                     request_id: str | None = None, tenant_id: str | None = None,
                     subject: SubjectRef | None = None, input: dict | None = None,
                     preference: str | None = None, policy: ResolvedPolicy | None = None) -> DecisionRecord:
        if ctx is None:
            if not request_id or not tenant_id or subject is None or input is None:
                raise ValueError('request, tenant, subject and input are required')
            ctx = DecisionContext(domain=domain.name, request_id=request_id, tenant_id=tenant_id,
                                  subject=subject, input=domain.input_model.model_validate(input),
                                  preference=preference)
        elif any(x is not None for x in (request_id, tenant_id, subject, input, preference)):
            raise ValueError('provide either a context or named input fields')
        if ctx.domain != domain.name or not ctx.request_id or not ctx.tenant_id:
            raise ValueError('context identity does not match domain')
        # A model_copy or a mutable nested input must not bypass domain validation.
        ctx = ctx.model_copy(update={'input': domain.input_model.model_validate(ctx.input.model_dump())})
        t0 = time.perf_counter()
        pol = policy or self.settings.policy(domain.name, domain.policy_defaults)
        kind = self.serving_kind(domain, ctx.tenant_id, pol)
        skipped = domain.model_allowed(ctx) if kind != SelectorKind.RULES else None
        if skipped:  # this input must not reach a model at all
            kind = SelectorKind.RULES
        routed = None
        if kind != SelectorKind.RULES and pol.model_routing == "when_unclear":
            routed = domain.rules_suffice(ctx)  # the rules can answer this one well: no model call, no cost
            if routed:
                kind = SelectorKind.RULES
        selector = self.selectors[kind]
        release = selector.release_for(domain)
        key = "sha256:" + hashlib.sha256(json.dumps({"context": ctx.input_hash(domain.version, release),
            "policy": pol.model_dump(mode="json")}, sort_keys=True).encode()).hexdigest()

        cached = (self.cache.get(key) or await self.store.get_by_hash(ctx.tenant_id, key)) if self.store else None
        if cached:
            METRICS.inc("decision_replays", domain=domain.name)
            self.cache.put(key, cached)
            return cached.model_copy(update={"replayed": True})

        elig = self._allowed(domain.eligibility(ctx.input), ctx.tenant_id, pol)
        ids = [o.option_id for o in elig.options]
        if len(ids) != len(set(ids)):
            raise ValueError('domain returned duplicate option identities')
        elig.safe_default  # Every domain must supply an eligible fallback.
        reasons: list[str] = []
        if skipped:
            reasons.append(f"MODEL_SKIPPED:{skipped}")
        elif routed:
            reasons.append(f"ROUTED_TO_RULES:{routed}")
        elif kind != pol.serving:
            reasons.append(f"SERVING_{pol.serving.value.upper()}_NOT_AVAILABLE_FOR_REQUEST")
        option_count: int | None = None

        out: SelectorOutput | None = None
        chosen: Option | None = None
        source = SelectionSource.RULE
        fallback = FallbackReason.NONE

        if ctx.preference:
            chosen = domain.match_preference(ctx.preference, elig)
            if chosen:
                source = SelectionSource.USER
                reasons.append("USER_PREFERENCE_ELIGIBLE")
            else:
                reasons.append(f"USER_PREFERENCE_INELIGIBLE:{ctx.preference}")

        if chosen is None and len(elig.options) == 1:
            chosen, source = elig.options[0], SelectionSource.RULE
            reasons.append("SINGLE_ELIGIBLE_OPTION")

        if chosen is None:
            # The rules answer comes first: it is what we serve if the model cannot give one in time.
            rules_answer = elig.by_id(domain.rules_select(ctx, elig)) or elig.safe_default
            budget = pol.selector_deadline_ms / 1000
            ts = time.perf_counter()
            failed = False
            try:
                choice = elig if kind == SelectorKind.RULES else self._choice(domain, ctx, elig)
                option_count = len(choice.options)
                raw = await asyncio.wait_for(selector.select(domain, ctx, choice, budget), timeout=budget)
                out = SelectorOutput.model_validate(raw)
                chosen, source, fallback = self._validate(out, choice, elig, kind, pol)
                failed = fallback == FallbackReason.INVALID_RESPONSE  # a broken answer is a failure, not a doubt
            except Exception as e:
                fallback = (FallbackReason.TIMEOUT if isinstance(e, asyncio.TimeoutError)
                            else e.reason if isinstance(e, SelectorError)
                            else FallbackReason.PROVIDER_ERROR)
                failed = True
                chosen, source = elig.safe_default, SelectionSource.FALLBACK
                log(logger, "selector_failed", domain=domain.name, selector=kind.value, reason=fallback.value,
                    error=type(e).__name__)
            if failed and self._rules_on_failure(domain, pol):
                chosen, source = rules_answer, SelectionSource.RULE
                reasons.append("SELECTOR_FAILED_RULES_SERVED")
            if fallback != FallbackReason.NONE:
                out = None  # Rejected evidence must not be attributed to the fallback answer.
            METRICS.observe("selector_latency_ms", (time.perf_counter() - ts) * 1000,
                            domain=domain.name, selector=kind.value)

        if source in (SelectionSource.RULE, SelectionSource.HOSTED, SelectionSource.IN_HOUSE):
            variant = domain.variant_for(ctx, elig, chosen)
            canonical = elig.by_id(variant.option_id)
            if canonical is None:
                chosen, source, fallback, out = elig.safe_default, SelectionSource.FALLBACK, FallbackReason.INVALID_RESPONSE, None
            else:
                if canonical.option_id != chosen.option_id:
                    reasons.append(f"VARIANT:{canonical.group}")
                chosen = canonical
        reasons += domain.reason_codes(chosen)
        if out and out.model_release:
            reasons += [f"MODEL_JUDGMENT:{k.upper()}" for k, v in out.signals.items() if v >= _STRONG_SIGNAL]

        record = DecisionRecord(
            decision_id=f"dec_{uuid.uuid4().hex}",
            domain=domain.name,
            domain_version=domain.version,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
            input_hash=key,
            subject=ctx.subject,
            option_id=chosen.option_id,
            option=chosen,
            selection_source=source,
            fallback_reason=fallback,
            approval_required=domain.approval_required(chosen, source.value,
                                                       out.provider_confidence if out else None),
            provider=selector.name if kind != SelectorKind.RULES and out else None,
            model=out.model if out else None,
            model_release=out.model_release if out else None,
            probabilities=out.probabilities if out else None,
            provider_confidence=out.provider_confidence if out else None,
            calibrated_confidence=(out.probabilities or {}).get(chosen.option_id)
            if out and out.calibration_version else None,
            calibration_version=out.calibration_version if out else None,
            reason_codes=reasons,
            explanation=domain.explain(chosen, FallbackReason.NONE if source == SelectionSource.RULE else fallback),
            alternatives=self._alternatives(domain, ctx, elig, chosen, out),
            ineligible=elig.ineligible,
            selector_release=release,
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            option_count=option_count,
            input_summary=domain.audit_summary(ctx),
            probability_margin=self._margin(out),
        )
        if self.store:
            record = await self.store.save(record)
            self.cache.put(key, record)
        # One line per decision: ids, versions and outcomes only. Never input values or user text.
        log(logger, "decision_made", decision_id=record.decision_id, domain=domain.name, tenant=ctx.tenant_id,
            option=record.option_id, source=record.selection_source.value, fallback=record.fallback_reason.value,
            approval_required=record.approval_required, selector=release, ms=record.latency_ms)
        METRICS.inc("decisions", domain=domain.name, source=record.selection_source.value,
                    fallback=record.fallback_reason.value, type=domain.metric_type(record.option))
        METRICS.observe("decision_latency_ms", record.latency_ms, domain=domain.name)
        if self.store and source != SelectionSource.USER and len(elig.options) > 1:
            self._spawn_shadows(domain, ctx, elig, record, pol, kind)
        return record

    def _allowed(self, elig: Eligibility, tenant_id: str, pol: ResolvedPolicy) -> Eligibility:
        """Config kill switch: options whose family is not allowed for this tenant are withdrawn, never the
        safe default."""
        groups = pol.groups_for(tenant_id)
        if groups is None:
            return elig
        keep = [o for o in elig.options if (o.family or o.group) in groups or o.option_id == elig.safe_default_id]
        dropped = [Ineligible(group=o.group, reason_codes=["NOT_ENABLED_FOR_TENANT"])
                   for o in elig.options if o not in keep]
        return Eligibility(keep, [*elig.ineligible, *dropped], elig.safe_default_id)

    @staticmethod
    def _choice(domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility) -> Eligibility:
        options = domain.choice_options(ctx, elig)
        ids = [o.option_id for o in options]
        if not ids or len(set(ids)) != len(ids) or any(elig.by_id(oid) is None for oid in ids):
            raise ValueError('invalid provider option set')
        options = [elig.by_id(oid) for oid in ids]
        safe = elig.safe_default_id if any(o.option_id == elig.safe_default_id for o in options) \
            else options[0].option_id
        return Eligibility(options, elig.ineligible, safe)

    @staticmethod
    def _rules_on_failure(domain: DecisionDomain, pol: ResolvedPolicy) -> bool:
        return pol.on_selector_failure == "rules" and domain.risk != RiskTier.HIGH  # HIGH risk fails closed

    @staticmethod
    def _margin(out: SelectorOutput | None) -> float | None:
        probs = sorted((out.probabilities or {}).values(), reverse=True) if out else []
        return round(probs[0] - probs[1], 4) if len(probs) >= 2 else None

    @staticmethod
    def _alternatives(domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility, chosen: Option,
                      out: SelectorOutput | None) -> list[Option]:
        """Model probabilities first when a model answered, then the rules ranking, so the order is stable."""
        probs = (out.probabilities if out else None) or {}
        rank = {oid: i for i, oid in enumerate(domain.rules_ranking(ctx, elig))}
        rest = [o for o in elig.options if o.option_id != chosen.option_id]
        return sorted(rest, key=lambda o: (-probs.get(o.option_id, -1.0), rank.get(o.option_id, len(rank))))

    def _validate(self, out: SelectorOutput, choice: Eligibility, elig: Eligibility, kind: SelectorKind,
                  pol: ResolvedPolicy) -> tuple[Option, SelectionSource, FallbackReason]:
        if out.option_id is None:
            return elig.safe_default, SelectionSource.FALLBACK, FallbackReason.ABSTAINED
        opt = choice.by_id(out.option_id)
        if opt is None:
            return elig.safe_default, SelectionSource.FALLBACK, FallbackReason.INVALID_RESPONSE
        if out.probabilities is not None and (set(out.probabilities) != {o.option_id for o in choice.options}
                or abs(sum(out.probabilities.values()) - 1) > 0.02):
            return elig.safe_default, SelectionSource.FALLBACK, FallbackReason.INVALID_RESPONSE
        if out.fit_probability is not None and out.fit_probability < pol.min_fit_probability:
            return elig.safe_default, SelectionSource.FALLBACK, FallbackReason.ABSTAINED
        if out.provider_confidence is not None and out.provider_confidence < pol.min_auto_confidence:
            return elig.safe_default, SelectionSource.FALLBACK, FallbackReason.LOW_CONFIDENCE
        return opt, _SOURCE[kind], FallbackReason.NONE

    def _sampled(self, key: str) -> bool:
        """Deterministic sampling by idempotency key: the same input is always in or always out."""
        rate = self.settings.shadow_sample_rate
        return rate >= 1.0 or (rate > 0 and int(key[-8:], 16) % 10_000 < rate * 10_000)

    def _spawn_shadows(self, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                       served: DecisionRecord, pol: ResolvedPolicy, serving: SelectorKind) -> None:
        if self.settings.rules_only or domain.model_allowed(ctx) is not None or not self._sampled(served.input_hash):
            return
        for kind in pol.shadows:
            sel = self.selectors.get(kind)
            if not sel or kind == serving:
                continue
            if kind == SelectorKind.HOSTED and ctx.tenant_id not in pol.external_inference_tenants:
                continue  # shadows obey tenant data policy too
            if kind == SelectorKind.IN_HOUSE and domain.scorer is None:
                continue
            task = asyncio.create_task(self._shadow(sel, domain, ctx, elig, served, pol.deadline_ms / 1000))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)

    async def _shadow(self, sel: Selector, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                      served: DecisionRecord, budget: float) -> None:
        t = time.perf_counter()
        oid, agreed, err = None, None, None
        try:
            # The same narrowed question serving would ask (D5), so shadow results predict serving behaviour.
            choice = self._choice(domain, ctx, elig)
            out = await asyncio.wait_for(sel.select(domain, ctx, choice, budget), timeout=budget)
            oid, agreed = out.option_id, out.option_id == served.option_id
        except Exception as e:  # noqa: BLE001 - a shadow must never raise
            err = type(e).__name__
        METRICS.inc("shadow_runs", domain=domain.name, selector=sel.name, agreed=str(agreed))
        await self.store.add_shadow(served.decision_id, domain.name, sel.name, oid, agreed,
                                    (time.perf_counter() - t) * 1000, err, tenant_id=served.tenant_id)

    async def drain(self) -> None:
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)

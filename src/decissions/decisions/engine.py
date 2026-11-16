"""In-process, domain-neutral decisions. No fetching, enforcement or persistence."""
from __future__ import annotations

import asyncio
import math
import time
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from .domain import DecisionDomain
from .models import (DecisionContext, DecisionRecord, Eligibility, FallbackReason,
                     SelectionSource, SelectorOutput, SubjectRef)


class Selector(Protocol):
    name: str

    def release_for(self, domain: DecisionDomain) -> str: ...

    async def select(self, domain: DecisionDomain, context: DecisionContext,
                     eligible: Eligibility, budget_s: float) -> SelectorOutput: ...


class DecisionEngine:
    """Trusted domain code owns eligibility and the safe fallback; providers only select."""

    def __init__(self, *, selector: Selector | None = None, timeout_seconds: float = 2.0):
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('timeout must be finite and positive')
        self.selector, self.timeout_seconds = selector, timeout_seconds

    async def decide(self, domain: DecisionDomain, *, request_id: str, tenant_id: str,
                     subject: SubjectRef, input: dict, preference: str | None = None) -> DecisionRecord:
        if not request_id or not tenant_id:
            raise ValueError('request and tenant identity are required')
        started = time.monotonic()
        inp = domain.input_model.model_validate(input)
        context = DecisionContext(domain=domain.name, request_id=request_id, tenant_id=tenant_id,
                                  subject=subject, input=inp, preference=preference)
        eligible = domain.eligibility(inp)
        ids = [o.option_id for o in eligible.options]
        if len(ids) != len(set(ids)):
            raise ValueError('domain returned duplicate option identities')
        chosen = eligible.safe_default  # Validate this even when a model is available.
        source, fallback = SelectionSource.RULE, FallbackReason.NONE
        output = None
        release = f'rules/{domain.name}@{domain.version}'
        preferred = domain.match_preference(preference, eligible) if preference else None
        if preferred is not None:
            chosen, source = preferred, SelectionSource.USER
        elif self.selector is None or domain.model_allowed(context) is not None:
            candidate = eligible.by_id(domain.rules_select(context, eligible))
            if candidate is None:
                source, fallback = SelectionSource.FALLBACK, FallbackReason.INVALID_RESPONSE
            else:
                chosen = candidate
        else:
            try:
                release = self.selector.release_for(domain)
                offered = domain.choice_options(context, eligible)
                offered_ids = {o.option_id for o in offered}
                if (not offered_ids or len(offered_ids) != len(offered)
                        or not offered_ids.issubset(ids)):
                    raise ValueError('invalid provider option set')
                narrowed = Eligibility(offered, eligible.ineligible, eligible.safe_default_id)
                raw = await asyncio.wait_for(
                    self.selector.select(domain, context, narrowed, self.timeout_seconds),
                    timeout=self.timeout_seconds)
                output = SelectorOutput.model_validate(raw)
                if output.option_id is None:
                    fallback = FallbackReason.ABSTAINED
                elif output.option_id not in offered_ids:
                    fallback = FallbackReason.INVALID_RESPONSE
                elif output.fit_probability is not None and output.fit_probability < 0.5:
                    fallback = FallbackReason.ABSTAINED
                else:
                    chosen = eligible.by_id(output.option_id)
                    source = SelectionSource(self.selector.name)
                    if source not in (SelectionSource.HOSTED, SelectionSource.IN_HOUSE):
                        raise ValueError('invalid provider source')
            except asyncio.TimeoutError:
                fallback = FallbackReason.TIMEOUT
            except Exception:
                # Never place provider errors or input values in a result or log.
                fallback = FallbackReason.PROVIDER_ERROR
            if fallback != FallbackReason.NONE:
                chosen, source, output = eligible.safe_default, SelectionSource.FALLBACK, None
        if source in (SelectionSource.RULE, SelectionSource.HOSTED, SelectionSource.IN_HOUSE):
            variant = domain.variant_for(context, eligible, chosen)
            if eligible.by_id(variant.option_id) is None:
                chosen, source, fallback = eligible.safe_default, SelectionSource.FALLBACK, FallbackReason.INVALID_RESPONSE
            else:
                chosen = eligible.by_id(variant.option_id)
        input_hash = context.input_hash(domain.version, release)
        return DecisionRecord(
            decision_id=str(uuid5(NAMESPACE_URL, f'{request_id}:{input_hash}:{chosen.option_id}')),
            domain=domain.name, domain_version=domain.version, request_id=request_id, tenant_id=tenant_id,
            input_hash=input_hash, subject=subject, option_id=chosen.option_id, option=chosen,
            selection_source=source, fallback_reason=fallback,
            approval_required=domain.approval_required(chosen, source.value, output.provider_confidence if output else None),
            provider=self.selector.name if output and self.selector else None,
            model=output.model if output else None, model_release=output.model_release if output else None,
            probabilities=output.probabilities if output else None,
            provider_confidence=output.provider_confidence if output else None,
            reason_codes=domain.reason_codes(chosen), explanation=domain.explain(chosen, fallback),
            alternatives=[o for o in eligible.options if o.option_id != chosen.option_id],
            ineligible=eligible.ineligible, selector_release=release,
            latency_ms=(time.monotonic()-started)*1000, input_summary=domain.audit_summary(context))

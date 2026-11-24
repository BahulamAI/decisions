"""Deterministic baseline for every domain: delegates to the domain's own rules. It is the serving
default until a model beats it on that domain's locked test set."""

from __future__ import annotations

from decissions.contracts.domain import DecisionDomain
from decissions.contracts.models import DecisionContext, Eligibility, SelectorOutput


class RulesSelector:
    name = "rule"

    def release_for(self, domain: DecisionDomain) -> str:
        return f"rules/{domain.name}@{domain.version}"

    async def select(self, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                     budget_s: float) -> SelectorOutput:
        oid = domain.rules_select(ctx, elig)
        if elig.by_id(oid) is None:  # a domain bug; fall back rather than serve an ineligible option
            oid = elig.safe_default_id
        return SelectorOutput(option_id=oid, model=self.release_for(domain))

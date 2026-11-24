"""Runs a domain's own in-house Scorer (if it has one). Domains without a trained model
raise SelectorUnavailable, and the engine falls back to rules for them."""

from __future__ import annotations

from decissions.contracts.domain import DecisionDomain
from decissions.contracts.models import DecisionContext, Eligibility, SelectorOutput
from .base import SelectorUnavailable


class InHouseSelector:
    name = "in_house_model"

    def release_for(self, domain: DecisionDomain) -> str:
        return domain.scorer.release if domain.scorer else "none"

    async def select(self, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                     budget_s: float) -> SelectorOutput:
        if domain.scorer is None:
            raise SelectorUnavailable(f"{domain.name} has no in-house model")
        return domain.scorer.score(ctx, elig)

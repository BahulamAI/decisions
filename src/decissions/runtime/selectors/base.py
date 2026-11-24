"""The single seam every decision maker plugs into. Selectors are domain-agnostic: they ask the
domain for options, facts and rules, so one Jev adapter serves every candidate-selection domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from decissions.contracts.models import DecisionContext, Eligibility, FallbackReason, SelectorOutput

if TYPE_CHECKING:
    from decissions.contracts.domain import DecisionDomain


class SelectorError(Exception):
    reason: FallbackReason = FallbackReason.PROVIDER_ERROR


class SelectorTimeout(SelectorError):
    reason = FallbackReason.TIMEOUT


class SelectorRateLimited(SelectorError):
    reason = FallbackReason.RATE_LIMITED


class SelectorInvalidResponse(SelectorError):
    reason = FallbackReason.INVALID_RESPONSE


class SelectorCircuitOpen(SelectorError):
    reason = FallbackReason.CIRCUIT_OPEN


class SelectorBudgetExceeded(SelectorError):
    reason = FallbackReason.BUDGET_EXCEEDED


class SelectorUnavailable(SelectorError):
    reason = FallbackReason.UNAVAILABLE


@runtime_checkable
class Selector(Protocol):
    name: str  # matches SelectionSource value

    def release_for(self, domain: DecisionDomain) -> str:
        """Immutable version recorded in every decision and part of the idempotency key."""
        ...

    async def select(self, domain: DecisionDomain, ctx: DecisionContext, elig: Eligibility,
                     budget_s: float) -> SelectorOutput: ...

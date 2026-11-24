"""Domain plugin interface: eligible candidates and rules, executed by the shared runtime."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from .models import DecisionContext, Eligibility, FallbackReason, Option, RiskTier, SelectorOutput


class Scorer(ABC):
    """Optional in-house model returning probabilities over eligible options."""

    release: str
    calibration_version: str | None = None

    @abstractmethod
    def score(self, ctx: DecisionContext, elig: Eligibility) -> SelectorOutput: ...


class DecisionDomain(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    risk: ClassVar[RiskTier] = RiskTier.LOW
    description: ClassVar[str] = ""
    input_model: ClassVar[type[BaseModel]]
    choice_instructions: ClassVar[str]  # trusted Jev Choice instructions
    subject_kind: ClassVar[str]

    scorer: Scorer | None = None
    # Domain defaults for policy fields (see config.DomainPolicy); configuration still overrides them.
    policy_defaults: ClassVar[dict[str, Any]] = {}

    # ---------------------------------------------------------------- required
    @abstractmethod
    def eligibility(self, inp: BaseModel) -> Eligibility:
        """Deterministic. Build every option that is SAFE for this input; explain the rest."""

    @abstractmethod
    def rules_select(self, ctx: DecisionContext, elig: Eligibility) -> str:
        """Deterministic baseline choice among eligible options. Must return an eligible option_id."""

    # ---------------------------------------------------------------- optional hooks
    def untrusted_text(self, inp: BaseModel) -> str:
        """User- or agent-authored free text. Sent to models only as data, never as instructions."""
        return ""

    def model_state(self, ctx: DecisionContext) -> dict[str, Any]:
        """Minimal, redacted facts a hosted model may see. Default: nothing but the text."""
        return {}

    def signal_questions(self) -> dict[str, str]:
        """Extra yes/no judgments asked in the same model call (become MODEL_JUDGMENT reason codes)."""
        return {}

    def rules_ranking(self, ctx: DecisionContext, elig: Eligibility) -> list[str]:
        """Every eligible option id, best first by rules. Orders Switch view when no model answered."""
        first = self.rules_select(ctx, elig)
        return [first, *(o.option_id for o in elig.options if o.option_id != first)]

    def choice_options(self, ctx: DecisionContext, elig: Eligibility) -> list[Option]:
        """The options a model may choose among. Default: all of them. Domains trim to a small, distinct set."""
        return list(elig.options)

    def audit_summary(self, ctx: DecisionContext) -> dict[str, Any] | None:
        """A de-identified description of the input, kept on the record for review. Never labels or values."""
        return None

    def variant_for(self, ctx: DecisionContext, elig: Eligibility, chosen: Option) -> Option:
        """After a family is chosen (by rules or a model), code may pick a variant of it from the data (D5)."""
        return chosen

    def metric_type(self, option: Option) -> str:
        """A low-cardinality label for metrics (a chart type, a route). Never ids, labels or values."""
        return option.group

    def rules_suffice(self, ctx: DecisionContext) -> str | None:
        """With policy model_routing="when_unclear": a reason code when the rules can answer this input well on
        their own (so no model is asked), else None. Never a safety check; that is model_allowed."""
        return None

    def model_allowed(self, ctx: DecisionContext) -> str | None:
        """None if a model may see this input; otherwise a reason code, and rules decide instead."""
        return None

    def match_preference(self, pref: str, elig: Eligibility) -> Option | None:
        return next((o for o in elig.options if pref in (o.option_id, o.group)), None)

    def reason_codes(self, option: Option) -> list[str]:
        return [f"POLICY:{option.group.upper()}"]

    def explain(self, option: Option, fallback: FallbackReason) -> str:
        base = option.description.get("what", option.option_id)
        if fallback in (FallbackReason.NONE, FallbackReason.NO_ELIGIBLE):
            return base
        return f"{base} (safe default: no option could be chosen reliably)"

    def approval_required(self, option: Option, source: str, confidence: float | None) -> bool:
        """HIGH-risk domains need a human unless rules or the user chose, or the domain says otherwise."""
        return self.risk == RiskTier.HIGH and source not in ("user", "rule")

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "risk": self.risk.value,
                "description": self.description, "subject_kind": self.subject_kind,
                "input_schema": self.input_model.model_json_schema(),
                "has_in_house_model": self.scorer is not None}

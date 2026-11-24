"""Domain-agnostic decision contract: decision-platform.decision/1.

Every decision in application, whether it picks a chart, routes a chat message, gates an agent's tool
call or triages a migration asset, has the same shape:

    subject (what we decide about) + typed input  ->  eligible OPTIONS (built by code)
    -> one option chosen (user | rules | hosted model | in-house model | safe fallback)
    -> an auditable DecisionRecord

Domains differ only in how they build options and what their rules are. Everything else (auth,
tenancy, deadlines, fallback, idempotency, shadowing, audit, metrics, feedback, evals) is shared.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from .compat import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

RECORD_SCHEMA = "decision-platform.decision/1"

Ident = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RiskTier(StrEnum):
    LOW = "low"  # wrong answer is cosmetic or instantly reversible (a chart, a route)
    MEDIUM = "medium"  # wrong answer wastes work (a migration queue)
    HIGH = "high"  # wrong answer can cause harm (an agent action): fail closed to a human


class SelectionSource(StrEnum):
    RULE = "rule"
    USER = "user"
    HOSTED = "hosted_model"
    IN_HOUSE = "in_house_model"
    FALLBACK = "fallback"


class FallbackReason(StrEnum):
    NONE = "none"
    NO_ELIGIBLE = "no_eligible_candidates"
    ABSTAINED = "abstained"
    LOW_CONFIDENCE = "low_confidence"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    INVALID_RESPONSE = "invalid_response"
    CIRCUIT_OPEN = "circuit_open"
    UNAVAILABLE = "selector_unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"


class SubjectRef(Frozen):
    """What the decision is about. Opaque to the platform; meaningful to the calling service."""

    kind: Ident  # e.g. rationalization_result, chat_message, agent_tool_call, migration_asset
    id: Ident
    revision: int | None = None
    content_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None = None
    attrs: dict[Annotated[str, Field(max_length=64)], Annotated[str, Field(max_length=256)]] = Field(
        default_factory=dict, max_length=16)


class Option(Frozen):
    option_id: Ident
    group: Ident  # template / category / action
    family: str | None = None  # variants of one idea share a family (bar and horizontal_bar); default: group
    description: dict[str, str]  # trusted, server-authored: what / best_for / not_for
    disclosures: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)  # domain data (e.g. the chart candidate)


class Ineligible(Frozen):
    group: str
    reason_codes: list[str]


@dataclass(frozen=True)
class Eligibility:
    options: list[Option]
    ineligible: list[Ineligible]
    safe_default_id: str

    def by_id(self, oid: str | None) -> Option | None:
        return next((o for o in self.options if o.option_id == oid), None)

    @property
    def safe_default(self) -> Option:
        opt = self.by_id(self.safe_default_id)
        if opt is None:  # a domain bug, caught by tests: safe default must always be eligible
            raise RuntimeError(f"safe default {self.safe_default_id} is not eligible")
        return opt


class DecisionContext(BaseModel):
    """Everything a selector may look at. `input` is the domain's own validated model."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    domain: str
    request_id: str
    tenant_id: str
    subject: SubjectRef
    input: BaseModel
    preference: str | None = None  # an option_id or group the user explicitly asked for

    def input_hash(self, domain_version: str, selector_release: str) -> str:
        """Exact versioned idempotency/cache key. Tenant is part of it: no cross-tenant hits."""
        payload = {"tenant": self.tenant_id, "domain": [self.domain, domain_version],
                   "subject": self.subject.model_dump(mode="json"), "input": self.input.model_dump(mode="json"),
                   "preference": self.preference, "selector": selector_release}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


class SelectorOutput(BaseModel):
    """What a selector returns before the engine validates it."""

    model_config = ConfigDict(extra="forbid")
    option_id: str | None  # None = abstain
    probabilities: dict[str, Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]] | None = None
    provider_confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    fit_probability: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None  # companion Noul: "does any option fit faithfully?"
    model: str | None = None
    model_release: str | None = None
    calibration_version: str | None = None
    signals: dict[str, float] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)


class DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = RECORD_SCHEMA
    decision_id: str
    domain: str
    domain_version: str
    request_id: str
    tenant_id: str
    input_hash: str
    subject: SubjectRef
    option_id: str
    option: Option
    selection_source: SelectionSource
    fallback_reason: FallbackReason = FallbackReason.NONE
    approval_required: bool = False
    provider: str | None = None
    model: str | None = None
    model_release: str | None = None
    probabilities: dict[str, Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]] | None = None
    provider_confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    calibrated_confidence: float | None = None
    calibration_version: str | None = None
    reason_codes: list[str]
    explanation: str
    alternatives: list[Option]
    ineligible: list[Ineligible]
    selector_release: str
    latency_ms: float
    # De-identified shape of the input (roles, counts, true/false facts; no names, labels, values or user text),
    # so a reviewed mistake can be rebuilt as a synthetic regression case.
    input_summary: dict[str, Any] | None = None
    option_count: int | None = None  # how many options the model actually chose among
    probability_margin: float | None = None  # top probability minus the second: how close the call was
    replayed: bool = False

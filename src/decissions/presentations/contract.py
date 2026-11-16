"""decision-platform.presentation-selection/2: the presentation domain's input contract (used by Django).

Rules of the contract:
* Callers never send candidates. Candidates are built server-side from the profile, so a caller
  (or a prompt-injected label) cannot smuggle in a chart the policy would reject.
* Nullable numeric fields mean "unavailable", never zero.
* Everything is bounded (lengths, counts) so one request cannot exhaust memory.

/2 adds the series, source, target and relation roles and a declared category `order`. /1 requests are still
accepted and mean exactly what they meant before; they may not use what /2 added.
"""

from __future__ import annotations

import math
from ..decisions.compat import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "decision-platform.presentation-selection/2"
LEGACY_SCHEMA_VERSION = "decision-platform.presentation-selection/1"
SchemaVersion = Literal["decision-platform.presentation-selection/1", "decision-platform.presentation-selection/2"]
MAX_ORDER = 50  # declared category orders are short scales, never a list of every label

Ident = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- result identity
class SourceMode(StrEnum):
    SAVED = "saved_result"  # immutable rationalization outcome
    CURRENT_SNAPSHOT = "current_snapshot"  # materialized "now" view, labelled as such


class ResultRef(Frozen):
    result_id: Ident
    revision: Annotated[int, Field(ge=1)]
    content_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    source_mode: SourceMode = SourceMode.SAVED
    result_kind: Ident = "dataset"


# ---------------------------------------------------------------- intent
class ChartKind(StrEnum):
    BAR = "bar"
    LINE = "line"
    PIE = "pie"
    TABLE = "table"
    TEXT = "text"


class Audience(StrEnum):
    EXECUTIVE = "executive"
    ANALYST = "analyst"
    ENGINEER = "engineer"


class Intent(Frozen):
    text: Annotated[str, Field(max_length=1000)] = ""
    preference: ChartKind | None = None
    audience: Audience = Audience.ANALYST
    locale: Annotated[str, Field(max_length=16)] = "en"
    screen_reader: bool = False

    @field_validator("text")
    @classmethod
    def strip_control_chars(cls, v: str) -> str:
        # Untrusted text: drop control characters; it is data, never instructions.
        return "".join(ch for ch in v if ch.isprintable() or ch in "\n\t").strip()


# ---------------------------------------------------------------- profile
class FieldRole(StrEnum):
    DIMENSION = "dimension"
    MEASURE = "measure"
    TIME = "time"
    IDENTIFIER = "identifier"
    EVIDENCE = "evidence"
    STATUS = "status"
    SERIES = "series"  # /2: splits one measure into named series (a stack segment, a line, a heatmap row)
    SOURCE = "source"  # /2: the "from" end of an edge
    TARGET = "target"  # /2: the "to" end of an edge
    RELATION = "relation"  # /2: what a link means; values from relations.RELATIONS only


V2_ROLES = frozenset({FieldRole.SERIES, FieldRole.SOURCE, FieldRole.TARGET, FieldRole.RELATION})


class Semantics(StrEnum):
    COUNT = "count"  # additive, part-of-whole capable
    SCORE = "score"  # per-asset rating; NOT parts of a whole
    SIMILARITY = "similarity"  # NOT a duplicate probability
    PERCENTAGE = "percentage"
    CURRENCY = "currency"
    OTHER = "other"


class FieldProfile(Frozen):
    # Field names become parts of option ids, so they use id-safe characters. Model-safe is stricter (D9).
    name: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")]
    role: FieldRole
    dtype: Literal["string", "integer", "number", "datetime", "boolean"]
    semantics: Semantics = Semantics.OTHER
    unit: Annotated[str, Field(max_length=32)] | None = None
    additive: bool = False
    null_count: Annotated[int, Field(ge=0)] = 0
    negative_count: Annotated[int, Field(ge=0)] = 0
    distinct_count: Annotated[int, Field(ge=0)] = 0
    min: float | None = None
    max: float | None = None
    total: float | None = None  # backend-computed sum; the model never supplies aggregates
    score_scale: tuple[float, float] | None = None
    max_length: Annotated[int, Field(ge=0)] | None = None  # longest text value: chooses horizontal bars
    # The owner of the data's meaning declares an order (low < medium < high). Absent means nominal: the
    # categories are names, and no chart may imply that one comes before another.
    order: Annotated[tuple[Annotated[str, Field(max_length=200)], ...], Field(min_length=2,
                                                                                max_length=MAX_ORDER)] | None = None

    @field_validator("min", "max", "total")
    @classmethod
    def finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError("profile statistics must be finite")
        return v


class ResultProfile(Frozen):
    row_count: Annotated[int, Field(ge=0)]
    total_rows: Annotated[int, Field(ge=0)] | None = None  # None = unknown denominator
    is_complete: bool  # declared complete by the result store, not inferred from a page
    sample_only: bool = False
    fields: Annotated[list[FieldProfile], Field(max_length=64)]
    grain: Annotated[str, Field(max_length=64)] | None = None
    duplicated_categories: bool = False
    time_ordered: bool = False
    comparable_runs: bool = True
    mixed_units: bool = False
    # Cell facts for results with a series field (one row per x and series). Without one they equal the
    # facts above, so a guard reading them can never be laxer than the guard it replaces.
    duplicated_cells: bool = False
    missing_cells: Annotated[int, Field(ge=0)] = 0
    series_time_ordered: bool = False
    # Link facts for results with source and target fields (one row per link).
    node_count: Annotated[int, Field(ge=0)] = 0
    relations: tuple[str, ...] = ()  # distinct declared relations, sorted
    self_links: Annotated[int, Field(ge=0)] = 0
    duplicate_links: bool = False
    has_cycle: bool = False  # following links source -> target can come back to where it started
    multi_parent: Annotated[int, Field(ge=0)] = 0  # sources linked to more than one target
    roots: Annotated[int, Field(ge=0)] = 0  # targets that are never a source: the top of a hierarchy
    missing_evidence_count: Annotated[int, Field(ge=0)] = 0
    filters: dict[str, str] = Field(default_factory=dict, max_length=32)

    @property
    def coverage(self) -> float | None:
        if self.total_rows in (None, 0):
            return None
        return min(1.0, self.row_count / self.total_rows)

    def field(self, role: FieldRole) -> list[FieldProfile]:
        return [f for f in self.fields if f.role == role]

    @model_validator(mode="after")
    def unique_names(self) -> ResultProfile:
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        return self


# ---------------------------------------------------------------- candidates
class Candidate(Frozen):
    candidate_id: Ident
    template_id: Ident
    template_version: Annotated[int, Field(ge=1)]
    kind: ChartKind  # the coarse kind the /1 block is drawn as (a donut falls back to a pie, a histogram to a table)
    chart_type: str | None = None  # the exact Workplane chat chart type, when the template draws one
    bindings: dict[str, str | list[str]]
    aggregation: str | None = None
    ordering: str | None = None
    top_n: int | None = None
    description: dict[str, str]  # what / best_for / not_for (trusted, server-authored)
    disclosures: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- request / decision
class SelectionRequest(Frozen):
    schema_version: SchemaVersion = LEGACY_SCHEMA_VERSION
    request_id: Ident
    result_ref: ResultRef
    intent: Intent
    profile: ResultProfile

"""presentation.select: PRD-64 as one DecisionDomain plugin."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..contracts.domain import DecisionDomain
from ..contracts.models import (
    DecisionContext,
    Eligibility,
    FallbackReason,
    Ineligible,
    Option,
    RiskTier,
    SubjectRef,
)
from .contract import Audience, Candidate, ChartKind, Intent, ResultProfile, ResultRef
from .intent import INTENT_QUESTIONS, keyword_signals
from .policy import CATALOG, CATALOG_VERSION, POLICY_VERSION, eligible_candidates

MAX_CHOICES = 10  # D5: at most ten options reach a model
LONG_LABEL = 18  # characters: longer category names read better on horizontal bars
WAFFLE_MAX_CATEGORIES = 4  # a 10x10 grid stays readable for a few shares
SAFE_FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")  # D9: anything else never reaches a model
_TYPE_OF = {t.template_id: (CATALOG.chart_type(t.chart_type) if t.chart_type else None) for t in CATALOG.templates}


def _family(template_id: str) -> str:
    spec = _TYPE_OF.get(template_id)
    return template_id if spec is None else (spec.variant_of or spec.chart_type)

_BY_SIGNAL: dict[str, tuple[str, ...]] = {
    "inspect": ("evidence_table", "detail_table"),
    "trend": ("time_metric_line",),
    "share": ("category_share_pie", "category_metric_bar", "category_metric_top_n_bar", "detail_table"),
    "rank": ("category_metric_top_n_bar", "category_metric_bar"),
    "compare": ("category_metric_bar", "category_metric_top_n_bar"),
}
_SIGNAL_ORDER = ("inspect", "trend", "share", "rank", "compare")
_DEFAULT = ("category_metric_bar", "category_metric_top_n_bar", "time_metric_line", "evidence_table",
            "detail_table", "empty_state")
_KIND_PREFERENCE = {
    ChartKind.BAR: ("category_metric_bar", "category_metric_top_n_bar"),
    ChartKind.LINE: ("time_metric_line",),
    ChartKind.PIE: ("category_share_pie",),
    ChartKind.TABLE: ("evidence_table", "detail_table"),
    ChartKind.TEXT: ("empty_state",),
}
_REASON = {"category_metric_bar": "VALID_CATEGORY_METRIC", "category_metric_top_n_bar": "HIGH_CARDINALITY_TOP_N",
           "category_share_pie": "COMPLETE_ADDITIVE_SMALL_SET", "time_metric_line": "ORDERED_COMPARABLE_RUNS",
           "evidence_table": "ASSET_LEVEL_EVIDENCE", "detail_table": "EXACT_VALUES", "empty_state": "NO_ROWS"}


class PresentationInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    result_ref: ResultRef
    intent: Intent
    profile: ResultProfile


def to_option(c: Candidate) -> Option:
    return Option(option_id=c.candidate_id, group=c.template_id, family=_family(c.template_id),
                  description=c.description, disclosures=c.disclosures, payload=c.model_dump(mode="json"))


def _is_root(option: Option) -> bool:
    spec = _TYPE_OF.get(option.group)
    return spec is None or spec.variant_of is None


def _flag(option: Option, name: str) -> bool:
    spec = _TYPE_OF.get(option.group)
    return True if spec is None else bool(getattr(spec, name))  # tables and the empty state are always allowed


class PresentationDomain(DecisionDomain):
    name = "presentation.select"
    version = f"{CATALOG_VERSION}+{POLICY_VERSION}"
    risk = RiskTier.LOW
    description = "Choose how an authorized result is shown in chat"
    subject_kind = "data_result"
    input_model = PresentationInput
    # D6: a model gets 1.5 s of a 2 s decision; if it fails, the rules answer is served, not a bare table.
    policy_defaults = {"deadline_ms": 2000, "selector_deadline_ms": 1500, "on_selector_failure": "rules"}
    choice_instructions = ("Which presentation best answers the user's request about this analysis result "
                           "without misleading them about scope, scale or completeness?")

    @staticmethod
    def subject_for(ref: ResultRef) -> SubjectRef:
        return SubjectRef(kind="data_result", id=ref.result_id, revision=ref.revision,
                          content_hash=ref.content_hash,
                          attrs={"result_kind": ref.result_kind, "source_mode": ref.source_mode.value})

    def eligibility(self, inp: BaseModel) -> Eligibility:
        assert isinstance(inp, PresentationInput)
        cs = eligible_candidates(inp.profile, inp.result_ref.result_kind)
        options = [to_option(c) for c in cs.candidates]
        groups = {o.group: o.option_id for o in options}
        safe = groups.get("detail_table") or groups.get("empty_state") or options[0].option_id
        return Eligibility(options, [Ineligible(group=t, reason_codes=r) for t, r in cs.rejected], safe)

    def rules_ranking(self, ctx: DecisionContext, elig: Eligibility) -> list[str]:
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        signals = keyword_signals(inp.intent.text)
        by_group = {o.group: o.option_id for o in elig.options}
        order: list[str] = [g for s in _SIGNAL_ORDER if signals[s] > 0 for g in _BY_SIGNAL[s]]
        if inp.intent.audience == Audience.ENGINEER:
            order.append("detail_table")
        order.extend(_DEFAULT)
        ranked = list(dict.fromkeys([*(by_group[g] for g in order if g in by_group), elig.safe_default_id,
                                     *(o.option_id for o in elig.options)]))
        return ranked

    def rules_select(self, ctx: DecisionContext, elig: Eligibility) -> str:
        # D11: a type the catalog has not cleared for rules selection can be offered, not chosen.
        return next((oid for oid in self.rules_ranking(ctx, elig)
                     if (o := elig.by_id(oid)) is not None and _flag(o, "rules_selectable")), elig.safe_default_id)

    def choice_options(self, ctx: DecisionContext, elig: Eligibility) -> list[Option]:
        """What a model may choose among: types cleared for model selection, one per family, at most ten.

        Kept in eligibility order, so the request a model sees does not depend on rules preferences.
        Within a family the root type is the default; variants stay available in Switch view.
        """
        allowed = [o for o in elig.options if _flag(o, "selectable")]
        first: dict[str, Option] = {}
        for o in allowed:  # one per family: the root type wins over its variants, else the first seen
            fam, current = o.family or o.group, first.get(o.family or o.group)
            if current is None or (_is_root(o) and not _is_root(current)):
                first[fam] = o
        picked = [o for o in allowed if first.get(o.family or o.group) is o]
        if len(picked) > MAX_CHOICES:
            rank = {oid: i for i, oid in enumerate(self.rules_ranking(ctx, elig))}
            keep = set(sorted((o.option_id for o in picked), key=lambda oid: rank[oid])[:MAX_CHOICES])
            keep.add(elig.safe_default_id)
            picked = [o for o in picked if o.option_id in keep][: MAX_CHOICES]
        return picked

    def audit_summary(self, ctx: DecisionContext) -> dict[str, Any] | None:
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        p = inp.profile
        return {"result_kind": inp.result_ref.result_kind, "row_count": p.row_count,
                "total_rows": p.total_rows, "is_complete": p.is_complete, "sample_only": p.sample_only,
                "duplicated_categories": p.duplicated_categories, "time_ordered": p.time_ordered,
                "comparable_runs": p.comparable_runs, "mixed_units": p.mixed_units,
                "fields": [{"role": f.role.value, "dtype": f.dtype, "semantics": f.semantics.value,
                            "additive": f.additive, "distinct": f.distinct_count, "nulls": f.null_count,
                            "negatives": f.negative_count} for f in p.fields]}

    def variant_for(self, ctx: DecisionContext, elig: Eligibility, chosen: Option) -> Option:
        """Pick a variant of the chosen family from facts about the data and the reader, never from label text.

        Only variants cleared for rules selection are used (D11), so an offered-only variant stays in Switch view.
        """
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        if not _is_root(chosen) or chosen.payload.get("ordering") == "declared_order":
            return chosen  # a declared order is the point of the chart; a variant would re-sort it
        family = chosen.family or chosen.group
        siblings = {(_TYPE_OF[o.group].chart_type if _TYPE_OF.get(o.group) else o.group): o
                    for o in elig.options if (o.family or o.group) == family and o.option_id != chosen.option_id
                    and _flag(o, "rules_selectable")}
        dim = next((f for f in inp.profile.fields if f.role.value == "dimension"), None)
        if family == "bar" and "horizontal_bar" in siblings and dim and (dim.max_length or 0) > LONG_LABEL:
            return siblings["horizontal_bar"]
        if family == "pie" and "waffle" in siblings and inp.intent.audience == Audience.EXECUTIVE and dim \
                and dim.distinct_count <= WAFFLE_MAX_CATEGORIES:
            return siblings["waffle"]
        return chosen

    def metric_type(self, option: Option) -> str:
        return type_of_template(option.group)

    def rules_suffice(self, ctx: DecisionContext) -> str | None:
        """The question names its intent in words the rules recognise (compare, share, trend, rank, inspect).
        Measured on recorded Jev answers: the rules are as good or better there, and Jev helps on vague phrasing."""
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        return "CLEAR_INTENT" if any(keyword_signals(inp.intent.text).values()) else None

    def model_allowed(self, ctx: DecisionContext) -> str | None:
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        if not all(SAFE_FIELD_NAME.match(f.name) for f in inp.profile.fields):
            return "FIELD_NAMES_NOT_MODEL_SAFE"
        return None

    def untrusted_text(self, inp: BaseModel) -> str:
        assert isinstance(inp, PresentationInput)
        return inp.intent.text

    def model_state(self, ctx: DecisionContext) -> dict[str, Any]:
        inp = ctx.input
        assert isinstance(inp, PresentationInput)
        p = inp.profile
        # Profile statistics and schema names only. Asset/category LABELS are never sent.
        return {
            "audience": inp.intent.audience.value,
            "result": {
                "kind": inp.result_ref.result_kind, "source_mode": inp.result_ref.source_mode.value,
                "row_count": p.row_count, "complete": p.is_complete and not p.sample_only,
                "coverage": None if p.coverage is None else round(p.coverage, 3),
                "missing_evidence_rows": p.missing_evidence_count,
                "fields": [{"name": f.name, "role": f.role.value, "type": f.dtype, "semantics": f.semantics.value,
                            "unit": f.unit, "distinct": f.distinct_count} for f in p.fields],
            },
        }

    def signal_questions(self) -> dict[str, str]:
        return dict(INTENT_QUESTIONS)

    def match_preference(self, pref: str, elig: Eligibility) -> Option | None:
        try:
            groups = _KIND_PREFERENCE[ChartKind(pref)]
        except ValueError:
            return super().match_preference(pref, elig)
        return next((o for g in groups for o in elig.options if o.group == g), None)

    def reason_codes(self, option: Option) -> list[str]:
        return [f"POLICY:{_REASON.get(option.group, option.group.upper())}"]

    def explain(self, option: Option, fallback: FallbackReason) -> str:
        base = option.description["what"] + "."
        if fallback in (FallbackReason.NONE, FallbackReason.NO_ELIGIBLE):
            return base
        return base + " Showing exact values because no chart could be chosen reliably."


def type_of_template(template_id: str) -> str:
    """The chart type a template draws; tables and the empty state are their own type. A metric label."""
    spec = _TYPE_OF.get(template_id)
    return spec.chart_type if spec else template_id


def candidate_of(option: Option) -> Candidate:
    return Candidate.model_validate(option.payload)

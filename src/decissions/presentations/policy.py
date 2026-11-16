"""Versioned template catalog + semantic eligibility policy (PRD-64 §6.3), driven by data.

A template either binds to the profile and yields ONE complete candidate (fields, ordering, aggregation,
disclosures all fixed), or it returns machine-readable reason codes explaining why it would mislead.
Selectors only ever pick among the complete candidates produced here, which is what makes "zero critical
misleading presentations" a property of construction, not of model luck.

What each template needs and checks lives in catalog/chart_types.json; what each check means lives in
guards.py. This module only binds the two to a profile. The catalog is loaded and checked when this module
is imported, so a bad catalog stops the service from starting.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .catalog_loader import TemplateSpec, load_catalog
from .contract import Candidate, ChartKind, FieldProfile, FieldRole, ResultProfile, Semantics
from .guards import GUARDS, GuardContext

CATALOG = load_catalog()
CATALOG_VERSION = CATALOG.catalog_version
POLICY_VERSION = "presentation-policy/2"
TABLE_PAGE_SIZE = 20
SAFE_DEFAULT_TEMPLATE = "detail_table"


@dataclass(frozen=True)
class Binding:
    candidate: Candidate | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateSet:
    candidates: list[Candidate]
    rejected: list[tuple[str, list[str]]]  # (template_id, reason codes)


def _first(profile: ResultProfile, role: str) -> FieldProfile | None:
    roles = [FieldRole.EVIDENCE, FieldRole.STATUS] if role == "evidence|status" else [FieldRole(role)]
    return next((f for r in roles for f in profile.field(r)), None)


def _words(f: FieldProfile) -> str:
    return f.name.replace("_", " ")


def _coverage_disclosures(p: ResultProfile) -> list[str]:
    out: list[str] = []
    if not p.is_complete or p.sample_only:
        cov = p.coverage
        out.append(f"PARTIAL_COVERAGE:{cov:.0%}" if cov is not None else "PARTIAL_COVERAGE:unknown_total")
    if p.missing_evidence_count:
        out.append(f"MISSING_EVIDENCE:{p.missing_evidence_count}")
    return out


def _measure_label(m: FieldProfile) -> list[str]:
    out = []
    if m.semantics == Semantics.SCORE:
        scale = f"{m.score_scale[0]:g}-{m.score_scale[1]:g}" if m.score_scale else "undeclared"
        out.append(f"SCORE_DEFINITION_SHOWN:scale={scale}")
        out.append("SCORES_ARE_PER_ASSET_NOT_PARTS_OF_WHOLE")
    if m.semantics == Semantics.SIMILARITY:
        out.append("SIMILARITY_IS_NOT_DUPLICATE_PROBABILITY")
    return out


def _disclosures(t: TemplateSpec, p: ResultProfile, fields: dict[str, FieldProfile],
                 fmt: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in t.disclosures:
        if item == "@coverage":
            out += _coverage_disclosures(p)
        elif item == "@measure_semantics":
            out += _measure_label(fields["measure"]) if "measure" in fields else []
        elif item == "@paginated":
            out += [f"PAGINATED:{TABLE_PAGE_SIZE}_per_page"] if p.row_count > TABLE_PAGE_SIZE else []
        else:
            out.append(item.format(**fmt))
    return out


def bind(t: TemplateSpec, p: ResultProfile) -> Binding:
    """Bind one template to a profile: the complete candidate, or every reason it would mislead."""
    fields: dict[str, FieldProfile] = {}
    for need in t.requires:
        f = _first(p, need.role)
        if f is None:
            if need.optional:
                continue
            return Binding(None, (need.missing,))
        fields.setdefault(need.role, f)
    reasons: list[str] = []
    for use in t.guards:
        reasons += GUARDS[use.name](GuardContext(p, fields, t.prefix, dict(use.model_extra or {})))
    if reasons:
        return Binding(None, tuple(reasons))
    fmt: dict[str, Any] = {"version": t.version, "top_n": t.top_n}
    for role, f in fields.items():
        if "|" not in role:
            fmt[role], fmt[f"{role}_words"] = f.name, _words(f)
    if t.top_n is not None:
        fmt["omitted"] = max(fields["dimension"].distinct_count, p.total_rows or 0) - t.top_n
    bindings: dict[str, str | list[str]] = {
        name: [f.name for f in p.fields] if role == "@all_fields" else fields[role].name
        for name, role in t.bindings.items() if role == "@all_fields" or role in fields}
    return Binding(Candidate(
        candidate_id=t.candidate_id.format(**fmt),
        template_id=t.template_id,
        template_version=t.version,
        kind=ChartKind(t.kind),
        chart_type=t.chart_type,
        bindings=bindings,
        ordering=t.ordering,
        top_n=t.top_n,
        description={k: v.format(**fmt) for k, v in t.description.model_dump().items()},
        disclosures=_disclosures(t, p, fields, fmt),
    ))


_ACTIVE = CATALOG.active_templates()
_EMPTY = next(t for t in _ACTIVE if t.empty_state)
TEMPLATES: dict[str, Callable[[ResultProfile], Binding]] = {
    t.template_id: (lambda p, t=t: bind(t, p)) for t in _ACTIVE if not t.empty_state}
EMPTY_STATE = Candidate(
    candidate_id=_EMPTY.candidate_id.format(version=_EMPTY.version),
    template_id=_EMPTY.template_id,
    template_version=_EMPTY.version,
    kind=ChartKind(_EMPTY.kind),
    bindings={},
    description=_EMPTY.description.model_dump(),
    disclosures=list(_EMPTY.disclosures),
)


def eligible_candidates(profile: ResultProfile, kind: str | None = None) -> CandidateSet:
    """Deterministic and order-stable. Same profile -> same candidate list, always."""
    if profile.row_count == 0:
        return CandidateSet([EMPTY_STATE], [(t, ["NO_ROWS"]) for t in TEMPLATES])
    ok: list[Candidate] = []
    bad: list[tuple[str, list[str]]] = []
    for tid, fn in TEMPLATES.items():
        b = fn(profile)
        if b.candidate:
            ok.append(b.candidate)
        else:
            bad.append((tid, list(b.reasons)))
    return CandidateSet(ok, bad)

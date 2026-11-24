"""Deterministic profiler. Turns an authorized, bounded result page plus its declared schema into a
ResultProfile. It never guesses semantics: whether a number is a count, a score or a similarity is
declared by the result store (FieldSpec), because the digits alone cannot tell you."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contract import LEGACY_SCHEMA_VERSION, MAX_ORDER, V2_ROLES, FieldProfile, FieldRole, ResultProfile, Semantics
from .relations import RELATIONS


class FieldSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:\-]+$")
    role: FieldRole
    dtype: str = Field(pattern="^(string|integer|number|datetime|boolean)$")
    semantics: Semantics = Semantics.OTHER
    unit: str | None = Field(default=None, max_length=32)
    additive: bool = False
    score_scale: tuple[float, float] | None = None
    order: tuple[str, ...] | None = Field(default=None, min_length=2, max_length=MAX_ORDER)  # /2: declared order

    @model_validator(mode="after")
    def _distinct_order(self) -> FieldSpec:
        if self.order is not None and len(set(self.order)) != len(self.order):
            raise ValueError("a declared order lists each category once")
        return self


def check_fields(schema_version: str, fields: list[FieldSpec], rows: list[dict[str, Any]]) -> None:
    """Contract rules that span fields and rows. Errors name the rule and the field, never a value."""
    for f in fields:
        if schema_version == LEGACY_SCHEMA_VERSION and (f.role in V2_ROLES or f.order is not None):
            raise ValueError(f"field {f.name} uses a /2 feature; send schema_version presentation-selection/2")
        if f.role == FieldRole.RELATION and any(r.get(f.name) not in RELATIONS for r in rows):
            raise ValueError(f"field {f.name} names a relation outside the server vocabulary")
        if f.order is not None:
            allowed = set(f.order)
            if any(r.get(f.name) is not None and str(r.get(f.name)) not in allowed for r in rows):
                raise ValueError(f"field {f.name} has a value outside its declared order")


class ProfileInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fields: list[FieldSpec] = Field(min_length=1, max_length=64)
    rows: list[dict[str, Any]] = Field(max_length=5000)
    total_rows: int | None = Field(default=None, ge=0)
    is_complete: bool
    grain: str | None = None
    comparable_runs: bool = True
    filters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _declared_order_holds(self) -> ProfileInput:
        check_fields("decision-platform.presentation-selection/2", self.fields, self.rows)
        return self


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v) if math.isfinite(float(v)) else None
    return None


def parse_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _has_cycle(edges: set[tuple[str, str]]) -> bool:
    """Kahn's algorithm: a graph is acyclic exactly when repeatedly removing nodes with no incoming link empties it."""
    nodes = {n for e in edges for n in e}
    incoming = {n: 0 for n in nodes}
    out: dict[str, list[str]] = {n: [] for n in nodes}
    for a, b in edges:
        incoming[b] += 1
        out[a].append(b)
    ready = [n for n, k in incoming.items() if k == 0]
    removed = 0
    while ready:
        n = ready.pop()
        removed += 1
        for m in out[n]:
            incoming[m] -= 1
            if incoming[m] == 0:
                ready.append(m)
    return removed < len(nodes)


def _link_facts(inp: ProfileInput, rows: list[dict[str, Any]]) -> dict[str, Any]:
    role = {s.role: s.name for s in reversed(inp.fields)}  # the first field with each role
    src, dst, rel = role.get(FieldRole.SOURCE), role.get(FieldRole.TARGET), role.get(FieldRole.RELATION)
    if src is None or dst is None or not rows:
        return {}
    pairs = [(str(r.get(src)), str(r.get(dst)), r.get(rel) if rel else None) for r in rows]
    proper = {(a, b) for a, b, _ in pairs if a != b}
    # An undirected link a-b is the same link as b-a.
    keys = [(a, b, k) if (k in RELATIONS and RELATIONS[k].directed) else (min(a, b), max(a, b), k)
            for a, b, k in pairs]
    parents: dict[str, set[str]] = {}
    for a, b, _ in pairs:
        parents.setdefault(a, set()).add(b)
    sources, targets = {a for a, _, _ in pairs}, {b for _, b, _ in pairs}
    return {"node_count": len(sources | targets),
            "relations": tuple(sorted({str(k) for _, _, k in pairs if k is not None})),
            "self_links": sum(1 for a, b, _ in pairs if a == b),
            "duplicate_links": len(keys) != len(set(keys)),
            "has_cycle": _has_cycle(proper),
            "multi_parent": sum(1 for ps in parents.values() if len(ps) > 1),
            "roots": len(targets - sources)}


def build_profile(inp: ProfileInput) -> ResultProfile:
    rows = inp.rows
    profiles: list[FieldProfile] = []
    for spec in inp.fields:
        values = [r.get(spec.name) for r in rows]
        nulls = sum(1 for v in values if v is None or v == "")
        nums = [n for n in (_num(v) for v in values) if n is not None]
        distinct = len({repr(v) for v in values if v is not None and v != ""})
        is_numeric = spec.dtype in ("integer", "number")
        profiles.append(
            FieldProfile(
                name=spec.name,
                role=spec.role,
                dtype=spec.dtype,  # type: ignore[arg-type]
                semantics=spec.semantics,
                unit=spec.unit,
                additive=spec.additive,
                null_count=nulls,
                negative_count=sum(1 for n in nums if n < 0) if is_numeric else 0,
                distinct_count=distinct,
                min=min(nums) if is_numeric and nums else None,
                max=max(nums) if is_numeric and nums else None,
                total=math.fsum(nums) if is_numeric and nums else None,
                score_scale=spec.score_scale,
                max_length=max((len(str(v)) for v in values if v is not None), default=0)
                if spec.dtype == "string" else None,
                order=spec.order,
            )
        )

    dims = [s for s in inp.fields if s.role == FieldRole.DIMENSION]
    duplicated = False
    if dims and rows:
        keys = [r.get(dims[0].name) for r in rows]
        duplicated = len(keys) != len(set(map(repr, keys)))

    time_fields = [s for s in inp.fields if s.role == FieldRole.TIME]
    time_ordered = False
    if time_fields and rows:
        parsed = [parse_dt(r.get(time_fields[0].name)) for r in rows]
        if all(p is not None for p in parsed):
            stamps = [p.timestamp() for p in parsed if p is not None]
            time_ordered = all(a < b for a, b in zip(stamps, stamps[1:], strict=False))

    duplicated_cells, missing_cells, series_time_ordered = duplicated, 0, time_ordered
    series = next((s for s in inp.fields if s.role == FieldRole.SERIES), None)
    x = dims[0] if dims else (time_fields[0] if time_fields else None)
    if series is not None and x is not None and rows:
        cells = [(repr(r.get(x.name)), repr(r.get(series.name))) for r in rows]
        duplicated_cells = len(cells) != len(set(cells))
        xs, ss = {c[0] for c in cells}, {c[1] for c in cells}
        missing_cells = len(xs) * len(ss) - len(set(cells))
    if series is not None and time_fields and rows:
        by_series: dict[str, list[datetime | None]] = {}
        for r in rows:
            by_series.setdefault(repr(r.get(series.name)), []).append(parse_dt(r.get(time_fields[0].name)))
        series_time_ordered = all(
            all(p is not None for p in ps)
            and all(a.timestamp() < b.timestamp() for a, b in zip(ps, ps[1:], strict=False))  # type: ignore[union-attr]
            for ps in by_series.values())

    links = _link_facts(inp, rows)

    units = {s.unit for s in inp.fields if s.role == FieldRole.MEASURE and s.unit}
    evidence = [s for s in inp.fields if s.role == FieldRole.EVIDENCE]
    missing_evidence = 0
    if evidence:
        missing_evidence = sum(1 for r in rows if r.get(evidence[0].name) in (None, "", []))

    return ResultProfile(
        row_count=len(rows),
        total_rows=inp.total_rows,
        is_complete=inp.is_complete,
        sample_only=inp.total_rows is not None and inp.total_rows > len(rows),
        fields=profiles,
        grain=inp.grain,
        duplicated_categories=duplicated,
        time_ordered=time_ordered,
        comparable_runs=inp.comparable_runs,
        mixed_units=len(units) > 1,
        duplicated_cells=duplicated_cells,
        missing_cells=missing_cells,
        series_time_ordered=series_time_ordered,
        **links,
        missing_evidence_count=missing_evidence,
        filters=inp.filters,
    )

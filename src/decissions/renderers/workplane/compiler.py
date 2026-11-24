"""Workplane-specific compiler extracted from the decision service.
Builds validated chart/table snapshots from the same admitted rows used for profiling.
The host owns data access, authorization, persistence and replay.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from typing import Any

from .chat_chart import BUILDABLE, CONTRACT_VERSION, ChartInvalid, chart_type_of, compile_chat, payload_sha256
from ...presentations.contract import Audience, Candidate, ChartKind, FieldRole, ResultRef
from ...presentations.policy import CATALOG_VERSION, POLICY_VERSION, TABLE_PAGE_SIZE
from ...presentations.profile import parse_dt

SNAPSHOT_FORMAT = "decision-platform.presentation-snapshot/2"
LEGACY_SNAPSHOT_FORMAT = "decision-platform.presentation-snapshot/1"
MAX_RESULT_ROWS = 1000  # ResultBlock refuses more
MAX_RESULT_COLUMNS = 50
_COLUMN_TYPES = {"integer": "integer", "number": "number", "boolean": "boolean"}


class CompileError(ValueError):
    """Raised when rows contradict the candidate (defence in depth after eligibility)."""


def _num(v: Any, column: str) -> int | float:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(float(v)):
        raise CompileError(f"column {column} has a null or non-numeric value")  # never echo the value
    return v


def _cell(v: Any, column: str) -> str | int | float | bool | None:
    if isinstance(v, float) and not math.isfinite(v):
        raise CompileError(f"column {column} has a non-finite value")
    if v is None or isinstance(v, str | int | float | bool):
        return v
    return json.dumps(v, sort_keys=True, default=str)  # Workplane cells are scalars


def rows_hash(rows: list[dict[str, Any]], field_names: Sequence[str]) -> str:
    """Fingerprint of the rows a decision saw, over the declared fields only and independent of row order.

    Cells are normalised the way the snapshot stores them, so rows that Django rebuilds from a saved
    snapshot (already sorted and projected by this compiler) hash the same as the rows it first sent.
    """
    def canon(v: Any) -> Any:
        if isinstance(v, float) and not math.isfinite(v):
            return repr(v)
        if v is None or isinstance(v, str | int | float | bool):
            return v
        return json.dumps(v, sort_keys=True, default=str)

    lines = sorted(json.dumps([canon(r.get(n)) for n in field_names], separators=(",", ":")) for r in rows)
    return "sha256:" + hashlib.sha256(json.dumps(lines).encode()).hexdigest()


def _inferred_type(values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if present and all(isinstance(v, bool) for v in present):
        return "boolean"
    if present and all(isinstance(v, int | float) and not isinstance(v, bool) for v in present):
        return "number"
    return "string"


def _columns(rows: list[dict[str, Any]], fields: Sequence[Any] | None) -> list[dict[str, str]]:
    if fields is not None:
        return [{"name": f.name, "type": _COLUMN_TYPES.get(f.dtype, "string")} for f in fields]
    names = list(dict.fromkeys(k for r in rows for k in r))
    return [{"name": n, "type": _inferred_type([r.get(n) for r in rows])} for n in names]


def _chart(kind: str, x: str, y: str, query_id: str, entity: str | None) -> dict[str, Any]:
    option: dict[str, Any] = {"series": [{"type": kind, "name": y}],
                              "tooltip": {"trigger": "item", "formatter": "{b}: {c} ({d}%)"} if kind == "pie"
                              else {"trigger": "axis"}}
    if kind != "pie":
        option.update(xAxis=[{"type": "category"}], yAxis=[{"type": "value", "name": y}])
    series = {"index": 0, "valueColumn": y, **({"entityColumn": entity} if entity else {})}
    return {"option": option, "bind": {"queryId": query_id, "categoryColumn": x, "series": [series]}}


def _largest_first(rows: list[dict[str, Any]], category: str, value: str) -> list[dict[str, Any]]:
    """Largest value first; equal values by category, so the same rows in any order give the same chart."""
    return sorted(rows, key=lambda r: (-_num(r.get(value), value), str(r.get(category))))


def _declared(fields: Sequence[Any] | None, name: str) -> dict[str, int] | None:
    order = next((getattr(f, "order", None) for f in fields or [] if f.name == name), None)
    return {v: i for i, v in enumerate(order)} if order else None


def _grid_order(rows: list[dict[str, Any]], b: dict[str, Any], fields: Sequence[Any] | None,
                by_time: bool) -> list[dict[str, Any]]:
    """One fixed row order for series charts, whatever order the rows arrived in: by time (or by declared order,
    else largest total first, for categories), then by series in declared order, else by name."""
    x, y, s = str(b["x"]), str(b["y"]), (str(b["series"]) if "series" in b else None)
    x_rank, s_rank = _declared(fields, x), (_declared(fields, s) if s else None)
    totals: dict[str, float] = {}
    for r in rows:
        totals[str(r.get(x))] = totals.get(str(r.get(x)), 0.0) + _num(r.get(y), y)

    def x_key(r: dict[str, Any]) -> tuple[Any, ...]:
        if by_time:
            stamp = parse_dt(r.get(x))
            if stamp is None:
                raise CompileError(f"column {x} has a value that is not a date")
            return (stamp.timestamp(),)
        if x_rank is not None:
            return (x_rank.get(str(r.get(x)), len(x_rank)),)
        return (-totals[str(r.get(x))], str(r.get(x)))

    def s_key(r: dict[str, Any]) -> tuple[Any, ...]:
        if s is None:
            return ()
        return (s_rank.get(str(r.get(s)), len(s_rank)),) if s_rank is not None else (str(r.get(s)),)

    return sorted(rows, key=lambda r: (*x_key(r), *s_key(r)))


def _link_order(rows: list[dict[str, Any]], b: dict[str, Any], *, groups: bool) -> list[dict[str, Any]]:
    """One fixed order for link tables, whatever order rows arrive in: strongest link first (by group first for
    hierarchies), then by names. Links without a value sort after those with one."""
    src, dst, val = str(b["source"]), str(b["target"]), (str(b["value"]) if "value" in b else None)

    def key(r: dict[str, Any]) -> tuple[Any, ...]:
        v = r.get(val) if val else None
        known = isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(float(v))
        strength = (0, -float(v)) if known else (1, 0.0)  # type: ignore[arg-type]
        return (str(r.get(dst)), *strength, str(r.get(src))) if groups else \
            (*strength, str(r.get(src)), str(r.get(dst)))

    return sorted(rows, key=key)


def compile_spec(candidate: Candidate, rows: list[dict[str, Any]], ref: ResultRef, *,
                 query_id: str | None = None, title: str | None = None,
                 fields: Sequence[Any] | None = None, audience: Audience = Audience.ANALYST,
                 screen_reader: bool = False) -> dict[str, Any]:
    """`query_id` is the saved view id: the frontend requests the result under that id."""
    if len(rows) > MAX_RESULT_ROWS:
        raise CompileError(f"result exceeds the {MAX_RESULT_ROWS} row inline limit")
    columns = _columns(rows, fields)
    if len(columns) > MAX_RESULT_COLUMNS:
        raise CompileError(f"result exceeds the {MAX_RESULT_COLUMNS} column inline limit")
    names = [c["name"] for c in columns]
    qid = query_id or ref.result_id
    entity = next((f.name for f in fields or [] if f.role == FieldRole.IDENTIFIER), None)
    b = candidate.bindings
    ordered = rows
    renderer, fallback = "workplane.table", "Open the result as a table."
    spec: dict[str, Any] = {"source": "query", "queryId": qid, "columns": names, "pageSize": TABLE_PAGE_SIZE}

    if candidate.kind == ChartKind.TEXT:
        fallback = "No rows match this result and scope."
    elif candidate.kind == ChartKind.TABLE:
        if candidate.ordering == "missing_evidence_first":
            ev = next((f.name for f in fields or [] if f.role == FieldRole.EVIDENCE),
                      next((n for n in names if "evidence" in n), None))
            if ev:
                ordered = sorted(rows, key=lambda r: bool(r.get(ev)))
        elif candidate.ordering in ("links", "groups"):
            ordered = _link_order(rows, b, groups=candidate.ordering == "groups")
        elif candidate.ordering in ("grid", "time_asc"):
            ordered = _grid_order(rows, b, fields, by_time=candidate.ordering == "time_asc")
    elif candidate.kind == ChartKind.LINE:
        x, y = str(b["x"]), str(b["y"])
        for r in rows:
            _num(r.get(y), y)
        renderer, spec = "workplane.echarts", _chart("line", x, y, qid, entity)
    elif candidate.kind == ChartKind.BAR:
        x, y = str(b["x"]), str(b["y"])
        rank = _declared(fields, x) if candidate.ordering == "declared_order" else None
        if candidate.ordering == "declared_order" and rank is None:
            raise CompileError("declared order missing at compile time")
        ordered = _largest_first(rows, x, y) if rank is None else \
            sorted(rows, key=lambda r: rank.get(str(r.get(x)), len(rank)))
        renderer, spec = "workplane.echarts", _chart("bar", x, y, qid, entity)
        if candidate.top_n:
            top = ordered[: candidate.top_n]
            spec = {"option": {**spec["option"],
                               "xAxis": [{"type": "category", "data": [str(r[x]) for r in top]}],
                               "series": [{"type": "bar", "name": y,
                                           "data": [{"name": str(r[x]), "value": r[y]} for r in top]}]}}
    elif candidate.kind == ChartKind.PIE:
        cat, val = str(b["category"]), str(b["value"])
        ordered = _largest_first(rows, cat, val)
        values = [_num(r.get(val), val) for r in ordered]
        if any(v < 0 for v in values) or math.fsum(values) <= 0:
            raise CompileError("pie invariants violated at compile time")
        renderer, spec = "workplane.echarts", _chart("pie", cat, val, qid, entity)
    else:  # pragma: no cover - exhaustive enum
        raise CompileError(f"unsupported kind {candidate.kind}")

    block_title = title or candidate.description["what"]
    chart: dict[str, Any] | None = None
    chart_status, disclosures = "none", list(candidate.disclosures)
    if chart_type_of(candidate) in BUILDABLE:
        try:
            payload, extra = compile_chat(
                candidate, ordered, title=block_title, audience=audience, screen_reader=screen_reader,
                units={f.name: getattr(f, "unit", None) for f in fields or []},
                additive={f.name: bool(getattr(f, "additive", False)) for f in fields or []})
            chart = {"contract": CONTRACT_VERSION, "payload": payload, "payload_sha256": payload_sha256(payload)}
            chart_status, disclosures = "ok", disclosures + extra
        except (ChartInvalid, CompileError):
            chart_status = "invalid"  # the API layer counts these; the table block still renders

    return {
        "format": SNAPSHOT_FORMAT,
        "block": {"id": qid, "kind": "analysis", "title": block_title,
                  "rendererId": renderer, "specVersion": "1", "spec": spec, "bindings": {}, "dataRefs": [qid],
                  "fallback": fallback},
        "columns": columns,
        "rows": [[_cell(r.get(n), n) for n in names] for r in ordered],
        "chart": chart,
        "disclosures": disclosures,
        "provenance": {
            "result_id": ref.result_id, "revision": ref.revision, "content_hash": ref.content_hash,
            "source_mode": ref.source_mode, "template": f"{candidate.template_id}@v{candidate.template_version}",
            "catalog_version": CATALOG_VERSION, "policy_version": POLICY_VERSION,
            "contract_version": CONTRACT_VERSION, "chart_status": chart_status,
            "payload_sha256": chart["payload_sha256"] if chart else None,
        },
    }

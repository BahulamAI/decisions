"""Build `workplane.chat-chart/1` payloads (rendered by ChatChart in @bahulam/workplane-ui) from authorized rows.

Every value, label and setting comes from code and the rows; nothing comes from a model (D1, D7). A payload is
validated here against the vendored contract's JSON Schema before it leaves the service, again by Django when it
is saved, and again by the browser before it is drawn (D8). Any failure means "show the table instead".

Builders are grouped by the contract's series shape:
* POINT ({label, value} items): bar, horizontal_bar, lollipop, pareto, pie, donut, waffle, sparkline
* MULTI (named series of {x, y} points): line, area, step_line, stacked_area, stacked_area_100, stacked_bar,
  stacked_bar_100. With a series binding, rows split into one named series per value, in the row order the
  compiler fixed.
* CELLS (one series of {x, y, value} cells): heatmap
* NETWORK (one {nodes, links} graph): graph, sankey. Link types and node categories come from relations.RELATIONS,
  never from the rows, because the renderer uses a link type as an ECharts label formatter.
* HIERARCHY (recursive {name, value?, children?} nodes): treemap, sunburst, tree, built from membership links
* SAMPLE (named lists of raw values; the renderer bins them): histogram, box_plot, strip_plot, beeswarm, ecdf,
  density
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from ...presentations.contract import Audience, Candidate
from ...presentations.relations import RELATIONS

_CONTRACT = json.loads((resources.files("decissions") / "renderers/workplane/contracts/chat-chart-v1.json").read_text())
CONTRACT_VERSION: str = _CONTRACT["contract_version"]
ENVELOPE = {"schema_version": "bahulam_render_v1", "type": "chart", "contract_version": CONTRACT_VERSION}
LIMITS: dict[str, int] = _CONTRACT["limits"]
MAX_ITEMS = 500  # the schema's maxItems for a series list
LABEL_CAP = 120
_VALIDATOR = Draft202012Validator(_CONTRACT["tool_schema"])
# ECharts reads {..} and | as template syntax in some text it formats. Labels are data, so neutralise them.
_NEUTRAL = str.maketrans({"{": "(", "}": ")", "|": "¦"})


POINT_TYPES = frozenset({"bar", "horizontal_bar", "lollipop", "pareto", "pie", "donut", "waffle", "sparkline"})
# No axes: the whole is the circle or the grid, and a sparkline hides them by design.
NO_AXES_TYPES = frozenset({"pie", "donut", "waffle", "sparkline"})
MULTI_TYPES = frozenset({"line", "area", "step_line", "stacked_area", "stacked_area_100", "stacked_bar",
                         "stacked_bar_100"})
SAMPLE_TYPES = frozenset({"histogram", "box_plot", "strip_plot", "beeswarm", "ecdf", "density"})
CELL_TYPES = frozenset({"heatmap"})
PERCENT_TYPES = frozenset({"stacked_bar_100", "stacked_area_100"})
NETWORK_TYPES = frozenset({"graph", "sankey"})
HIERARCHY_TYPES = frozenset({"treemap", "sunburst", "tree"})
MAX_NODES, MAX_LINKS = 100, 300  # the contract's graph limits
MAX_GROUPS, MAX_CHILDREN = 20, 30  # top-level nodes the contract allows; children kept readable
BUILDABLE = POINT_TYPES | MULTI_TYPES | SAMPLE_TYPES | CELL_TYPES | NETWORK_TYPES | HIERARCHY_TYPES


def chart_type_of(candidate: Candidate) -> str:
    return candidate.chart_type or candidate.kind.value


class ChartInvalid(ValueError):
    """The payload breaks the contract. Messages name the rule, never a value."""


def label(value: Any) -> str:
    """A safe display label: printable, no template characters, at most 120 characters."""
    text = "".join(ch for ch in str(value) if unicodedata.category(ch)[0] != "C").translate(_NEUTRAL).strip()
    return text if len(text) <= LABEL_CAP else text[: LABEL_CAP - 1] + "…"


def _value(v: Any) -> float | int:
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(float(v)):
        raise ChartInvalid("a charted value is null or not a finite number")
    return v


def presentation_settings(audience: Audience, screen_reader: bool) -> dict[str, Any]:
    """Theme and density follow the reader, never the model (D7)."""
    if screen_reader:
        return {"theme": "high_contrast", "density": "comfortable",
                "accessibility": {"high_contrast": True, "patterns": True}}
    if audience == Audience.EXECUTIVE:
        return {"theme": "presentation", "density": "presentation"}
    return {"theme": "default", "density": "comfortable"}


def _words(name: str, unit: str | None) -> str:
    text = name.replace("_", " ")
    return label(f"{text} ({unit})" if unit else text)


def _cap_points(points: list[dict[str, Any]], additive: bool) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep within the contract. Additive values fold the tail into "Other"; anything else is truncated."""
    if len(points) <= MAX_ITEMS:
        return points, []
    head, tail = points[: MAX_ITEMS - 1], points[MAX_ITEMS - 1:]
    if additive:
        return [*head, {"label": "Other", "value": math.fsum(p["value"] for p in tail)}], \
            [f"CHART_OTHER_BUCKET:{len(tail)}"]
    return points[:MAX_ITEMS], [f"CHART_TRUNCATED:{len(points) - MAX_ITEMS}"]


def _relation(value: Any) -> Any:
    try:
        return RELATIONS[str(value)]
    except KeyError:
        raise ChartInvalid("a link names a relation outside the vocabulary") from None


def _network(chart_type: str, ordered: list[dict[str, Any]], b: dict[str, Any]) -> list[dict[str, Any]]:
    src, dst, rel, val = str(b["source"]), str(b["target"]), str(b["relation"]), b.get("value")
    owner: dict[str, str] = {}  # display name -> the raw value it came from
    kinds: dict[str, str] = {}  # display name -> node category, from the vocabulary

    def node(raw: Any, kind: str) -> str:
        name = label(raw)
        if owner.setdefault(name, str(raw)) != str(raw):
            # The renderer joins links by display name, so two nodes sharing one would silently merge.
            raise ChartInvalid("two different nodes would share one label")
        kinds.setdefault(name, kind)
        return name

    links = []
    for r in ordered:
        relation = _relation(r.get(rel))
        link: dict[str, Any] = {"source": node(r.get(src), relation.source_kind),
                                "target": node(r.get(dst), relation.target_kind), "type": relation.label}
        if chart_type == "sankey":
            link["value"] = _value(r.get(str(val)))  # ribbon width is the flow; only sankey sizes links
        elif relation.directed:
            link["directed"] = True
        links.append(link)
    if len(kinds) > MAX_NODES or len(links) > MAX_LINKS:
        raise ChartInvalid("a graph has more nodes or links than the contract allows")
    one_kind = len(set(kinds.values())) == 1  # a legend with one entry says nothing
    nodes = [{"id": n, **({} if one_kind or chart_type == "sankey" else {"category": k})} for n, k in kinds.items()]
    return [{"nodes": nodes, "links": links}]


def _fold(nodes: list[dict[str, Any]], cap: int, noun: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Largest first; past `cap`, the rest become one "Other" node holding their summed size."""
    if len(nodes) <= cap:
        return nodes, []
    keep, rest = nodes[: cap - 1], nodes[cap - 1:]
    other = {"name": f"Other ({len(rest)} {noun})", "value": math.fsum(_size(n) for n in rest)}
    return [*keep, other], [f"CHART_OTHER_BUCKET:{len(rest)}"]


def _size(n: dict[str, Any]) -> float:
    return n["value"] if "children" not in n else math.fsum(_size(c) for c in n["children"])


def _hierarchy(chart_type: str, ordered: list[dict[str, Any]], b: dict[str, Any]
               ) -> tuple[list[dict[str, Any]], list[str]]:
    src, dst, val = str(b["source"]), str(b["target"]), str(b["value"])
    kids: dict[str, list[str]] = {}
    value: dict[str, float | int] = {}
    for r in ordered:
        child, parent = str(r.get(src)), str(r.get(dst))
        kids.setdefault(parent, []).append(child)
        value[child] = _value(r.get(val))
    notes: list[str] = []

    def build(name: str, seen: frozenset[str]) -> dict[str, Any]:
        if name in seen:
            raise ChartInvalid("membership links form a cycle")
        if name not in kids:
            return {"name": label(name), "value": value[name]}
        children = sorted((build(c, seen | {name}) for c in kids[name]), key=lambda n: (-_size(n), n["name"]))
        children, extra = _fold(children, MAX_CHILDREN, "members")
        notes.extend(extra)
        return {"name": label(name), "children": children}

    members = {c for cs in kids.values() for c in cs}
    roots = sorted((build(p, frozenset()) for p in kids if p not in members), key=lambda n: (-_size(n), n["name"]))
    if chart_type == "tree" and len(roots) != 1:
        raise ChartInvalid("a tree needs exactly one root")
    roots, extra = _fold(roots, MAX_GROUPS, "groups")
    return roots, notes + extra


def compile_chat(candidate: Candidate, ordered: list[dict[str, Any]], *, title: str,
                 units: dict[str, str | None], additive: dict[str, bool],
                 audience: Audience = Audience.ANALYST, screen_reader: bool = False
                 ) -> tuple[dict[str, Any], list[str]]:
    """Return (payload, extra disclosures). `ordered` is the rows in the order the table shows them."""
    b, disclosures, chart_type = candidate.bindings, [], chart_type_of(candidate)
    if chart_type in POINT_TYPES:
        cat, val = (str(b["category"]), str(b["value"])) if "category" in b else (str(b["x"]), str(b["y"]))
        rows = ordered[: candidate.top_n] if candidate.top_n else ordered
        series, disclosures = _cap_points([{"label": label(r.get(cat)), "value": _value(r.get(val))} for r in rows],
                                          additive.get(val, False))
        axes = {} if chart_type in NO_AXES_TYPES else {"x_label": _words(cat, None),
                                                    "y_label": _words(val, units.get(val))}
    elif chart_type in SAMPLE_TYPES:
        val = str(b["value"])
        values = [_value(r.get(val)) for r in ordered]
        if len(values) > LIMITS["max_points"]:
            raise ChartInvalid("a sample has more values than the contract allows")
        series = [{"name": _words(val, units.get(val)), "values": values}]
        axes = {"x_label": _words(val, units.get(val))}
    elif chart_type in MULTI_TYPES:
        x, y = str(b["x"]), str(b["y"])
        split = str(b["series"]) if "series" in b else None
        named: dict[str, list[dict[str, Any]]] = {}
        for r in ordered:
            name = label(r.get(split)) if split else _words(y, units.get(y))
            named.setdefault(name, []).append({"x": label(r.get(x)), "y": _value(r.get(y))})
        if len(named) > LIMITS["max_series"]:
            raise ChartInvalid("a chart has more series than the contract allows")
        if any(len(points) > MAX_ITEMS for points in named.values()) or len(ordered) > LIMITS["max_points"]:
            raise ChartInvalid("a series has more points than the contract allows")
        series = [{"name": name, "points": points} for name, points in named.items()]
        # The renderer normalises 100% stacks, so the axis shows shares, not the measure's own unit.
        y_label = label(f"share of {_words(y, None)} (%)") if chart_type in PERCENT_TYPES else _words(y, units.get(y))
        axes = {"x_label": _words(x, None), "y_label": y_label}
    elif chart_type in CELL_TYPES:
        x, s, y = str(b["x"]), str(b["series"]), str(b["y"])
        cells = [{"x": label(r.get(x)), "y": label(r.get(s)), "value": _value(r.get(y))} for r in ordered]
        if len(cells) > MAX_ITEMS:
            raise ChartInvalid("a heatmap has more cells than the contract allows")
        series = [{"name": _words(y, units.get(y)), "cells": cells}]
        axes = {"x_label": _words(x, None), "y_label": _words(s, None)}
    elif chart_type in NETWORK_TYPES:
        series, axes = _network(chart_type, ordered, b), {}
    elif chart_type in HIERARCHY_TYPES:
        (series, disclosures), axes = _hierarchy(chart_type, ordered, b), {}
    else:
        raise ChartInvalid(f"no chat builder for {candidate.kind}")
    payload = {**ENVELOPE, "chart_type": chart_type, "title": label(title), **axes, "series": series,
               **presentation_settings(audience, screen_reader)}
    validate(payload)
    return payload, disclosures


def validate(payload: dict[str, Any]) -> None:
    """The same checks Django and the browser repeat: envelope, JSON Schema, and the contract's size limits."""
    if any(payload.get(k) != v for k, v in ENVELOPE.items()):
        raise ChartInvalid("envelope does not match the contract")
    # The contract allows free-text observations under a chart. We never send them: text there would be a
    # claim nobody computed (D7). Refuse them here so no future builder can add them by accident.
    if "observations" in payload:
        raise ChartInvalid("observations are not allowed in service-built charts")
    body = {k: v for k, v in payload.items() if k not in ENVELOPE}
    error = next(iter(_VALIDATOR.iter_errors(body)), None)
    if error is not None:
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        raise ChartInvalid(f"payload fails the contract schema at {where}: {error.validator}")
    if len(json.dumps(payload, separators=(",", ":")).encode()) > LIMITS["max_bytes"]:
        raise ChartInvalid("payload exceeds the contract's byte limit")


def payload_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()

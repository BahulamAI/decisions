"""Named semantic guards: the reasons a chart would mislead, written once and reused by every template.

A guard looks at the result profile and the fields a template bound, and returns reason codes. An empty
list means "this guard has no objection". Templates list guards in order, and codes accumulate in that
order, so a reviewer reads every reason a chart was refused, not just the first.

Mandatory guards live here, in code, so the data catalog can add guards but never remove one: a pie can
not drop PART_OF_WHOLE by editing JSON. The catalog loader refuses to start if it tries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .contract import FieldProfile, FieldRole, ResultProfile, Semantics
from .relations import RELATIONS

ADDITIVE_SEMANTICS = {Semantics.COUNT, Semantics.CURRENCY}


@dataclass(frozen=True)
class GuardContext:
    profile: ResultProfile
    fields: dict[str, FieldProfile]  # role name -> the first field with that role
    prefix: str  # e.g. "PIE": reason codes read PIE_NEGATIVE_VALUES
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def measure(self) -> FieldProfile:
        return self.fields["measure"]

    @property
    def dimension(self) -> FieldProfile:
        return self.fields["dimension"]

    @property
    def duplicated(self) -> bool:
        """Repeated keys. Charts with a series field key rows by (x, series): `"grain": "cell"`. Without a series
        field both readings are the same fact, so the parameter can never loosen a guard."""
        p = self.profile
        return p.duplicated_cells if self.params.get("grain") == "cell" else p.duplicated_categories


Guard = Callable[[GuardContext], list[str]]
GUARDS: dict[str, Guard] = {}


def guard(name: str) -> Callable[[Guard], Guard]:
    def register(fn: Guard) -> Guard:
        GUARDS[name] = fn
        return fn
    return register


@guard("SINGLE_UNIT")
def single_unit(g: GuardContext) -> list[str]:
    return ["MIXED_UNITS"] if g.profile.mixed_units else []


@guard("NO_NULL_MEASURE")
def no_null_measure(g: GuardContext) -> list[str]:
    return ["MEASURE_HAS_NULLS"] if g.measure.null_count else []  # never zero-fill unknowns


@guard("UNIQUE_CATEGORIES")
def unique_categories(g: GuardContext) -> list[str]:
    return ["DUPLICATED_CATEGORIES_NEED_DECLARED_AGGREGATION"] if g.duplicated else []


@guard("MUTUALLY_EXCLUSIVE")
def mutually_exclusive(g: GuardContext) -> list[str]:
    return [f"{g.prefix}_CATEGORIES_NOT_MUTUALLY_EXCLUSIVE"] if g.duplicated else []


@guard("MAX_CATEGORIES")
def max_categories(g: GuardContext) -> list[str]:
    n, cap = g.dimension.distinct_count, int(g.params["max"])
    return [f"{g.prefix}_TOO_MANY_CATEGORIES:{n}>{cap}"] if n > cap else []


@guard("MORE_CATEGORIES_THAN")
def more_categories_than(g: GuardContext) -> list[str]:
    """For top-N charts: only worth it when the full chart would not fit."""
    return [str(g.params["code"])] if g.dimension.distinct_count <= int(g.params["min"]) else []


@guard("PART_OF_WHOLE")
def part_of_whole(g: GuardContext) -> list[str]:
    """Shares need additive, nonnegative values with a known, positive, complete total."""
    p, m, out = g.profile, g.measure, []
    if m.semantics not in ADDITIVE_SEMANTICS or not m.additive:
        out.append(f"{g.prefix}_{m.semantics.upper()}_NOT_PARTS_OF_WHOLE")
    if m.negative_count:
        out.append(f"{g.prefix}_NEGATIVE_VALUES")
    if m.total is None or m.total <= 0:
        out.append(f"{g.prefix}_NO_POSITIVE_TOTAL")
    if not p.is_complete or p.sample_only or p.total_rows is None:
        out.append(f"{g.prefix}_INCOMPLETE_DENOMINATOR")
    return out


@guard("ADDITIVE_NONNEGATIVE")
def additive_nonnegative(g: GuardContext) -> list[str]:
    """Cumulative shares (pareto) only mean something for additive, nonnegative values."""
    m, out = g.measure, []
    if m.semantics not in ADDITIVE_SEMANTICS or not m.additive:
        out.append(f"{g.prefix}_{m.semantics.upper()}_NOT_ADDITIVE")
    if m.negative_count:
        out.append(f"{g.prefix}_NEGATIVE_VALUES")
    return out


@guard("RAW_OBSERVATIONS")
def raw_observations(g: GuardContext) -> list[str]:
    """Distributions need one row per item. A distribution of totals per category describes the totals, not the
    items, and would mislead about how the items are spread."""
    ident = g.fields.get("identifier")
    if ident is None or ident.distinct_count != g.profile.row_count:
        return [f"{g.prefix}_NEEDS_ONE_ROW_PER_ITEM"]
    if g.measure.semantics in ADDITIVE_SEMANTICS and g.measure.additive:
        return [f"{g.prefix}_VALUES_ARE_TOTALS_NOT_OBSERVATIONS"]
    return []


@guard("MAX_ROWS")
def max_rows(g: GuardContext) -> list[str]:
    cap = int(g.params["max"])
    return [f"{g.prefix}_TOO_MANY_POINTS:{g.profile.row_count}>{cap}"] if g.profile.row_count > cap else []


@guard("ORDERED_TIME")
def ordered_time(g: GuardContext) -> list[str]:
    """Strictly increasing times. With `"grain": "cell"` the check holds within each series."""
    ok = g.profile.series_time_ordered if g.params.get("grain") == "cell" else g.profile.time_ordered
    return [] if ok else [f"{g.prefix}_TIME_NOT_ORDERED_OR_DUPLICATED"]


@guard("COMPARABLE_RUNS")
def comparable_runs(g: GuardContext) -> list[str]:
    return [] if g.profile.comparable_runs else [f"{g.prefix}_INCOMPARABLE_RUNS"]


@guard("ORDINAL_ORDER_DECLARED")
def ordinal_order_declared(g: GuardContext) -> list[str]:
    """A chart that reads left to right as low to high needs an order someone declared. Names sorted
    alphabetically are not a scale, and inventing one would claim a ranking the data does not have."""
    return [] if g.dimension.order else [f"{g.prefix}_CATEGORIES_HAVE_NO_DECLARED_ORDER"]


@guard("COMPLETE_GRID")
def complete_grid(g: GuardContext) -> list[str]:
    """Every x has a value for every series. A missing segment would quietly shrink a stacked total; the caller
    sends an explicit 0 when zero is the truth."""
    n = g.profile.missing_cells
    return [f"{g.prefix}_MISSING_CELLS:{n}"] if n else []


@guard("MAX_SERIES")
def max_series(g: GuardContext) -> list[str]:
    s, cap = g.fields.get("series"), int(g.params["max"])
    return [f"{g.prefix}_TOO_MANY_SERIES:{s.distinct_count}>{cap}"] if s and s.distinct_count > cap else []


@guard("SINGLE_SERIES")
def single_series(g: GuardContext) -> list[str]:
    """For charts that draw one series: rows split by a series field would be merged into one wrong line."""
    return [f"{g.prefix}_DRAWS_ONE_SERIES"] if g.profile.field(FieldRole.SERIES) else []


@guard("SIMILARITY_NOT_FLOW")
def similarity_not_flow(g: GuardContext) -> list[str]:
    """Ribbon width claims "this much moved from here to there". Only a declared flow carrying an additive
    quantity says that. Similarity is a likeness score: nothing moves, and two scores do not add up."""
    p, m = g.profile, g.fields.get("measure")
    if "similarity" in p.relations or (m is not None and m.semantics == Semantics.SIMILARITY):
        return [f"{g.prefix}_SIMILARITY_IS_NOT_A_FLOW"]
    if not p.relations or not all(RELATIONS[r].flow for r in p.relations):
        return [f"{g.prefix}_LINKS_ARE_NOT_FLOWS"]
    if m is None or m.semantics not in ADDITIVE_SEMANTICS or not m.additive:
        return [f"{g.prefix}_FLOW_NEEDS_AN_ADDITIVE_QUANTITY"]
    return []


@guard("ACYCLIC")
def acyclic(g: GuardContext) -> list[str]:
    return [f"{g.prefix}_HAS_A_CYCLE"] if g.profile.has_cycle else []


@guard("NO_SELF_LINKS")
def no_self_links(g: GuardContext) -> list[str]:
    n = g.profile.self_links
    return [f"{g.prefix}_SELF_LINKS:{n}"] if n else []


@guard("UNIQUE_LINKS")
def unique_links(g: GuardContext) -> list[str]:
    return [f"{g.prefix}_DUPLICATE_LINKS"] if g.profile.duplicate_links else []


@guard("MAX_NODES")
def max_nodes(g: GuardContext) -> list[str]:
    n, cap = g.profile.node_count, int(g.params["max"])
    return [f"{g.prefix}_TOO_MANY_NODES:{n}>{cap}"] if n > cap else []


@guard("MAX_LINKS")
def max_links(g: GuardContext) -> list[str]:
    n, cap = g.profile.row_count, int(g.params["max"])
    return [f"{g.prefix}_TOO_MANY_LINKS:{n}>{cap}"] if n > cap else []


@guard("MEMBERSHIP_LINKS")
def membership_links(g: GuardContext) -> list[str]:
    """Nesting says "this is part of that". Only membership links say it; similarity or dependency do not."""
    rels = g.profile.relations
    return [] if rels and all(RELATIONS[r].membership for r in rels) else [f"{g.prefix}_LINKS_ARE_NOT_MEMBERSHIP"]


@guard("DISJOINT_GROUPS")
def disjoint_groups(g: GuardContext) -> list[str]:
    """A member drawn inside two groups is counted in both, so the sizes add up to more than the whole."""
    n = g.profile.multi_parent
    return [f"{g.prefix}_MEMBERS_IN_SEVERAL_GROUPS:{n}"] if n else []


@guard("SINGLE_ROOT")
def single_root(g: GuardContext) -> list[str]:
    n = g.profile.roots
    return [] if n == 1 else [f"{g.prefix}_NEEDS_ONE_ROOT:{n}"]


@guard("MIN_ROWS")
def min_rows(g: GuardContext) -> list[str]:
    """At least `min` rows, or with `"distinct": role` at least `min` distinct values of that field (two runs, not
    two rows of one run)."""
    role = g.params.get("distinct")
    n = g.fields[role].distinct_count if role else g.profile.row_count
    return [str(g.params["code"]).format(prefix=g.prefix)] if n < int(g.params["min"]) else []


# The floor no catalog edit can lower. A family not listed in READY_FAMILIES cannot be enabled at all until
# its guards are written and reviewed here.
READY_FAMILIES = frozenset({"point", "multi", "sample", "heatmap", "graph", "hierarchy"})
MANDATORY_BY_FAMILY: dict[str, tuple[str, ...]] = {
    "point": ("SINGLE_UNIT", "NO_NULL_MEASURE"),
    "multi": ("SINGLE_UNIT", "NO_NULL_MEASURE"),
    "interval": ("SINGLE_UNIT", "NO_NULL_MEASURE"),
    "target": ("SINGLE_UNIT", "NO_NULL_MEASURE"),
    "heatmap": ("NO_NULL_MEASURE", "UNIQUE_CATEGORIES"),
    "sample": ("SINGLE_UNIT", "NO_NULL_MEASURE", "RAW_OBSERVATIONS"),
    "graph": ("NO_SELF_LINKS", "UNIQUE_LINKS", "MAX_NODES", "MAX_LINKS"),
    "hierarchy": ("NO_NULL_MEASURE", "ADDITIVE_NONNEGATIVE", "MEMBERSHIP_LINKS", "DISJOINT_GROUPS", "ACYCLIC"),
    "other": (),
}
PART_OF_WHOLE_TYPES = frozenset({"pie", "donut", "waffle", "stacked_bar_100", "stacked_area_100",
                                 "treemap", "sunburst"})
TREND_TYPES = frozenset({"line", "area", "step_line", "stacked_area", "stacked_area_100", "sparkline"})
# Stacking adds segments into a total, so the parts must be additive, nonnegative, and all present.
STACKED_TYPES = frozenset({"stacked_bar", "stacked_bar_100", "stacked_area", "stacked_area_100"})


def mandatory_guards(family: str, chart_type: str) -> tuple[str, ...]:
    out = list(MANDATORY_BY_FAMILY[family])
    if chart_type in PART_OF_WHOLE_TYPES:
        out += ["PART_OF_WHOLE", "MUTUALLY_EXCLUSIVE"]
    if chart_type in TREND_TYPES:
        out += ["ORDERED_TIME", "COMPARABLE_RUNS"]
    if chart_type == "pareto" or chart_type in STACKED_TYPES:
        out += ["ADDITIVE_NONNEGATIVE"]
    if chart_type in STACKED_TYPES:
        out += ["COMPLETE_GRID"]
    if chart_type == "sankey":
        out += ["SIMILARITY_NOT_FLOW", "ACYCLIC", "NO_NULL_MEASURE", "ADDITIVE_NONNEGATIVE"]
    if chart_type == "tree":
        out += ["SINGLE_ROOT"]
    return tuple(out)

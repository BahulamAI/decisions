"""The fixed vocabulary of relationships a link may declare (presentation-selection/2, role `relation`).

Why a closed list: Workplane's graph renderer passes a link's `type` to ECharts as a label *formatter*, and a
formatter interprets `{a}`, `{b}`, `{c}` and `{style|text}`. A caller-supplied string there is a template-injection
path. So callers only name an entry here (anything else is a 422 at the boundary), and the payload carries the
entry's server-written label, never the caller's text.

Direction and "is this a flow" belong to the relationship, not to each row: similarity cannot be declared directed,
and only a flow can size a sankey ribbon.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Relation:
    label: str  # drawn on the link; a fixed server string
    directed: bool
    flow: bool  # a real quantity moves from source to target (sankey)
    membership: bool  # source belongs to target (hierarchies)
    source_kind: str  # node legend category, also fixed
    target_kind: str


RELATIONS: dict[str, Relation] = {
    "similarity": Relation("similar to", directed=False, flow=False, membership=False,
                           source_kind="Item", target_kind="Item"),
    "cluster_member": Relation("member of", directed=True, flow=False, membership=True,
                               source_kind="Item", target_kind="Group"),
    "dependency": Relation("depends on", directed=True, flow=False, membership=False,
                           source_kind="Item", target_kind="Item"),
    "flow": Relation("flows to", directed=True, flow=True, membership=False,
                     source_kind="Stage", target_kind="Stage"),
}

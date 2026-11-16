from __future__ import annotations

import pytest

from decissions.decisions.models import DecisionContext
from decissions.presentations.contract import Intent, ResultRef
from decissions.presentations.domain import PresentationDomain, PresentationInput
from decissions.presentations.profile import FieldSpec, ProfileInput, build_profile


HASH = "sha256:" + "a" * 64
SECRET = "test-secret-please-rotate"
PRESENTATION = PresentationDomain()


def ref(kind: str = "platform_counts", tenant: str = "t1", result_id: str = "r1") -> ResultRef:
    return ResultRef(result_id=result_id,
                     revision=1, content_hash=HASH, result_kind=kind)


PLATFORM_FIELDS = [
    {"name": "platform", "role": "dimension", "dtype": "string"},
    {"name": "asset_count", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]
SCORE_FIELDS = [
    {"name": "asset_name", "role": "dimension", "dtype": "string"},
    {"name": "complexity_score", "role": "measure", "dtype": "number", "semantics": "score",
     "score_scale": [0, 100]},
]
RUN_FIELDS = [
    {"name": "run_date", "role": "time", "dtype": "datetime"},
    {"name": "open_duplicates", "role": "measure", "dtype": "integer", "semantics": "count"},
]


# /2 shapes: the three Phase 6 result kinds Django sends.
CLASS_FIELDS = [
    {"name": "score_class", "role": "dimension", "dtype": "string"},  # nominal: KG declares no order
    {"name": "asset_count", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]
RUN_SERIES_FIELDS = [
    {"name": "run_at", "role": "time", "dtype": "datetime"},
    {"name": "metric", "role": "series", "dtype": "string"},
    {"name": "assets", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]
CROSSTAB_FIELDS = [
    {"name": "platform", "role": "dimension", "dtype": "string"},
    {"name": "score_class", "role": "series", "dtype": "string"},
    {"name": "asset_count", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]


# Phase 7: link tables (one row per link) for relationship and hierarchy charts.
SIMILARITY_FIELDS = [
    {"name": "source", "role": "source", "dtype": "string"},
    {"name": "target", "role": "target", "dtype": "string"},
    {"name": "relation", "role": "relation", "dtype": "string"},
    {"name": "similarity", "role": "measure", "dtype": "number", "semantics": "similarity", "score_scale": [0, 1]},
]
MEMBERSHIP_FIELDS = [
    {"name": "asset", "role": "source", "dtype": "string"},
    {"name": "cluster", "role": "target", "dtype": "string"},
    {"name": "relation", "role": "relation", "dtype": "string"},
    {"name": "assets", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]
FLOW_FIELDS = [
    {"name": "source", "role": "source", "dtype": "string"},
    {"name": "target", "role": "target", "dtype": "string"},
    {"name": "relation", "role": "relation", "dtype": "string"},
    {"name": "moved_assets", "role": "measure", "dtype": "integer", "semantics": "count", "unit": "assets",
     "additive": True},
]


def similarity_rows():
    pairs = [("Sales Overview (tableau)", "Sales Summary (powerbi)", 0.93),
             ("Sales Overview (tableau)", "Regional Sales (tableau)", 0.88),
             ("Sales Summary (powerbi)", "Regional Sales (tableau)", 0.81),
             ("Sales Overview (tableau)", "Sales KPI (qlik)", 0.77)]
    return [{"source": a, "target": b, "relation": "similarity", "similarity": v} for a, b, v in pairs]


def membership_rows(clusters=("Candidate group 1", "Candidate group 2")):
    return [{"asset": f"{c[-1]}-asset-{j}", "cluster": c, "relation": "cluster_member", "assets": 1}
            for c in clusters for j in range(3 if c.endswith("1") else 2)]


def flow_rows():
    return [{"source": a, "target": b, "relation": "flow", "moved_assets": n}
            for a, b, n in (("Tableau", "Review", 30), ("Power BI", "Review", 12), ("Review", "Migrate", 25),
                            ("Review", "Retire", 17))]


def run_series_rows(runs: int = 4) -> list[dict]:
    return [{"run_at": f"2026-0{i}-01T09:00:00Z", "metric": m, "assets": n}
            for i in range(1, runs + 1) for m, n in (("duplicate_candidates", 10 + i), ("review_required", 40 - i))]


def crosstab_rows(platforms=("Tableau", "Power BI", "Qlik"), classes=("ACTIVE", "DUPLICATE", "MIGRATION_READY")):
    return [{"platform": p, "score_class": c, "asset_count": (i * 7 + j * 3) % 11}
            for i, p in enumerate(platforms) for j, c in enumerate(classes)]


def platform_rows(n: int = 4) -> list[dict]:
    names = ["Tableau", "Power BI", "Qlik", "Cognos", "Looker", "SSRS", "MicroStrategy", "BO"]
    return [{"platform": names[i % 8] + ("" if i < 8 else f" {i}"), "asset_count": 100 + i} for i in range(n)]


def profile(fields, rows, complete=True, total=None, comparable=True):
    return build_profile(ProfileInput(fields=[FieldSpec(**f) for f in fields], rows=rows,
                                      total_rows=len(rows) if total is None else total,
                                      is_complete=complete, comparable_runs=comparable))


def ctx(text="", fields=PLATFORM_FIELDS, rows=None, kind="platform_counts", tenant="t1", pref=None,
        complete=True, **kw) -> DecisionContext:
    """A presentation.select decision context."""
    rows = platform_rows() if rows is None else rows
    r = ref(kind, tenant)
    inp = PresentationInput(result_ref=r, intent=Intent(text=text, **kw), profile=profile(fields, rows, complete))
    return DecisionContext(domain=PRESENTATION.name, request_id="req-1", tenant_id=tenant,
                           subject=PresentationDomain.subject_for(r), input=inp, preference=pref)



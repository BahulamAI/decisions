"""Semantic safety: the misleading presentations PRD-64 calls critical are impossible by construction."""

import pytest

from decissions.renderers.workplane.compiler import CompileError, compile_spec
from decissions.presentations.policy import eligible_candidates
from tests.presentation_fixtures import PLATFORM_FIELDS, RUN_FIELDS, SCORE_FIELDS, platform_rows, profile, ref


def templates(p):
    return {c.template_id for c in eligible_candidates(p).candidates}


def reasons(p, tid):
    return next(r for t, r in eligible_candidates(p).rejected if t == tid)


def test_complete_small_counts_allow_pie_and_bar():
    t = templates(profile(PLATFORM_FIELDS, platform_rows(4)))
    assert {"category_share_pie", "category_metric_bar", "detail_table"} <= t


def test_scores_are_never_parts_of_a_whole():
    rows = [{"asset_name": f"a{i}", "complexity_score": 50.0 + i} for i in range(4)]
    p = profile(SCORE_FIELDS, rows)
    assert "category_share_pie" not in templates(p)
    assert "PIE_SCORE_NOT_PARTS_OF_WHOLE" in reasons(p, "category_share_pie")
    bar = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_metric_bar")
    assert "SCORES_ARE_PER_ASSET_NOT_PARTS_OF_WHOLE" in bar.disclosures


def test_partial_list_blocks_pie_and_discloses_coverage():
    p = profile(PLATFORM_FIELDS, platform_rows(4), complete=False, total=12)
    assert "PIE_INCOMPLETE_DENOMINATOR" in reasons(p, "category_share_pie")
    bar = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_metric_bar")
    assert "PARTIAL_COVERAGE:33%" in bar.disclosures


def test_unknown_total_is_not_complete():
    p = profile(PLATFORM_FIELDS, platform_rows(3), complete=False, total=None)
    assert "category_share_pie" not in templates(p)


@pytest.mark.parametrize("n,expect", [(6, True), (7, False)])
def test_pie_category_cap(n, expect):
    assert ("category_share_pie" in templates(profile(PLATFORM_FIELDS, platform_rows(n)))) is expect


def test_high_cardinality_uses_top_n_not_full_bar():
    t = templates(profile(PLATFORM_FIELDS, platform_rows(25)))
    assert "category_metric_top_n_bar" in t and "category_metric_bar" not in t


def test_null_values_never_zero_filled():
    rows = platform_rows(3)
    rows[0]["asset_count"] = None
    p = profile(PLATFORM_FIELDS, rows)
    assert "MEASURE_HAS_NULLS" in reasons(p, "category_metric_bar")
    assert templates(p) == {"detail_table"}


def test_negative_and_zero_totals_block_pie():
    rows = platform_rows(3)
    rows[0]["asset_count"] = -5
    assert "PIE_NEGATIVE_VALUES" in reasons(profile(PLATFORM_FIELDS, rows), "category_share_pie")
    zeros = [{"platform": p["platform"], "asset_count": 0} for p in platform_rows(3)]
    assert "PIE_NO_POSITIVE_TOTAL" in reasons(profile(PLATFORM_FIELDS, zeros), "category_share_pie")


def test_duplicated_categories_need_declared_aggregation():
    rows = platform_rows(3) + [platform_rows(1)[0]]
    t = templates(profile(PLATFORM_FIELDS, rows))
    assert "category_metric_bar" not in t and "category_share_pie" not in t


def test_line_requires_ordered_comparable_runs():
    ordered = [{"run_date": f"2026-0{i}-01", "open_duplicates": 10 * i} for i in range(1, 5)]
    assert "time_metric_line" in templates(profile(RUN_FIELDS, ordered))
    assert "LINE_TIME_NOT_ORDERED_OR_DUPLICATED" in reasons(profile(RUN_FIELDS, ordered[::-1]), "time_metric_line")
    assert "LINE_INCOMPARABLE_RUNS" in reasons(profile(RUN_FIELDS, ordered, comparable=False), "time_metric_line")


def test_empty_result_only_offers_empty_state():
    elig = eligible_candidates(profile(PLATFORM_FIELDS, []))
    assert [c.template_id for c in elig.candidates] == ["empty_state"]


def test_candidate_order_is_deterministic():
    p = profile(PLATFORM_FIELDS, platform_rows(5))
    assert [c.candidate_id for c in eligible_candidates(p).candidates] == \
           [c.candidate_id for c in eligible_candidates(p).candidates]


def test_compiler_rechecks_pie_invariants():
    p = profile(PLATFORM_FIELDS, platform_rows(3))
    pie = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_share_pie")
    bad = platform_rows(3)
    bad[0]["asset_count"] = -1  # rows changed after profiling: must not render
    with pytest.raises(CompileError):
        compile_spec(pie, bad, ref())


def test_compiled_spec_carries_provenance_and_exact_values():
    rows = platform_rows(3)
    p = profile(PLATFORM_FIELDS, rows)
    bar = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_metric_bar")
    spec = compile_spec(bar, rows, ref())
    assert spec["provenance"]["content_hash"].startswith("sha256:")
    values = [row[spec["columns"].index({"name": "asset_count", "type": "number"})] for row in spec["rows"]]
    assert values == sorted([r["asset_count"] for r in rows], reverse=True)
    assert spec["block"]["spec"]["bind"]["series"][0]["valueColumn"] == "asset_count"


def test_top_n_chart_is_inline_and_saved_rows_stay_complete():
    rows = platform_rows(30)
    p = profile(PLATFORM_FIELDS, rows)
    top = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_metric_top_n_bar")
    spec = compile_spec(top, rows, ref(), query_id="view-1")
    assert "bind" not in spec["block"]["spec"]
    assert len(spec["block"]["spec"]["option"]["series"][0]["data"]) == 10 and len(spec["rows"]) == 30
    assert spec["block"]["id"] == "view-1" and spec["block"]["dataRefs"] == ["view-1"]


def test_top_n_discloses_rows_beyond_a_partial_page():
    rows = platform_rows(30)
    p = profile(PLATFORM_FIELDS, rows, complete=False, total=200)
    top = next(c for c in eligible_candidates(p).candidates if c.template_id == "category_metric_top_n_bar")
    assert "TOP_N_OMITTED_ROWS:190" in top.disclosures


def test_compiler_refuses_more_rows_than_the_renderer_accepts():
    rows = platform_rows(1001)
    p = profile(PLATFORM_FIELDS, rows)
    table = next(c for c in eligible_candidates(p).candidates if c.template_id == "detail_table")
    with pytest.raises(CompileError):
        compile_spec(table, rows, ref())

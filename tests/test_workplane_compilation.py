"""Chat-chart payloads: built only from rows, safe labels, contract limits, reader-driven settings, table fallback,
and golden snapshots of every template's compiled output."""

import json
from pathlib import Path

import pytest

from decissions.renderers.workplane import chat_chart
from decissions.renderers.workplane.compiler import SNAPSHOT_FORMAT, compile_spec
from decissions.presentations.contract import Audience
from decissions.presentations.policy import EMPTY_STATE, eligible_candidates
from decissions.presentations.profile import FieldSpec, ProfileInput, build_profile
from tests.presentation_fixtures import (
    CROSSTAB_FIELDS,
    FLOW_FIELDS,
    MEMBERSHIP_FIELDS,
    PLATFORM_FIELDS,
    RUN_FIELDS,
    RUN_SERIES_FIELDS,
    SIMILARITY_FIELDS,
    crosstab_rows,
    flow_rows,
    membership_rows,
    platform_rows,
    profile,
    ref,
    run_series_rows,
    similarity_rows,
)

GOLDEN = Path(__file__).parent / "golden"
SPECS = [FieldSpec(**f) for f in PLATFORM_FIELDS]


def compiled(template: str, rows, fields=PLATFORM_FIELDS, **kw):
    p = profile(fields, rows)
    c = next(c for c in eligible_candidates(p).candidates if c.template_id == template)
    return compile_spec(c, rows, ref(), query_id="view-1", title="t", fields=[FieldSpec(**f) for f in fields], **kw)


def test_bar_payload_comes_from_the_rows_in_table_order():
    spec = compiled("category_metric_bar", platform_rows(4))
    payload = spec["chart"]["payload"]
    assert spec["format"] == SNAPSHOT_FORMAT and spec["provenance"]["chart_status"] == "ok"
    assert [p["value"] for p in payload["series"]] == [row[1] for row in spec["rows"]]
    assert "observations" not in payload and spec["chart"]["payload_sha256"] == spec["provenance"]["payload_sha256"]


def test_top_n_chart_draws_only_the_top_rows_while_the_table_keeps_all():
    spec = compiled("category_metric_top_n_bar", platform_rows(30))
    assert len(spec["chart"]["payload"]["series"]) == 10 and len(spec["rows"]) == 30


def test_line_payload_is_a_named_series_of_points():
    rows = [{"run_date": f"2026-0{i}-01", "open_duplicates": 10 - i} for i in range(1, 5)]
    payload = compiled("time_metric_line", rows, RUN_FIELDS)["chart"]["payload"]
    assert payload["chart_type"] == "line" and len(payload["series"][0]["points"]) == 4


def test_labels_cannot_carry_template_syntax_and_are_capped():
    assert chat_chart.label("{b}|{c}") == "(b)¦(c)"
    assert chat_chart.label("a\x00b\nc") == "abc"
    long = chat_chart.label("x" * 500)
    assert len(long) == 120 and long.endswith("…")


def test_reader_drives_theme_and_density_not_a_model():
    assert chat_chart.presentation_settings(Audience.EXECUTIVE, False)["theme"] == "presentation"
    assert chat_chart.presentation_settings(Audience.ANALYST, True)["accessibility"] == {"high_contrast": True,
                                                                                        "patterns": True}
    spec = compiled("category_metric_bar", platform_rows(3), screen_reader=True)
    assert spec["chart"]["payload"]["theme"] == "high_contrast"


def test_additive_overflow_folds_into_other_and_is_disclosed():
    points = [{"label": f"p{i}", "value": 1} for i in range(600)]
    capped, notes = chat_chart._cap_points(points, additive=True)
    assert len(capped) == chat_chart.MAX_ITEMS and capped[-1] == {"label": "Other", "value": 101}
    assert notes == ["CHART_OTHER_BUCKET:101"]
    truncated, notes = chat_chart._cap_points(points, additive=False)
    assert len(truncated) == chat_chart.MAX_ITEMS and notes == ["CHART_TRUNCATED:100"]


def test_invalid_payloads_are_refused_without_echoing_values():
    good = compiled("category_metric_bar", platform_rows(3))["chart"]["payload"]
    with pytest.raises(chat_chart.ChartInvalid) as e:
        chat_chart.validate({**good, "observations": ["secret-looking-value"]})
    assert "secret-looking-value" not in str(e.value)
    with pytest.raises(chat_chart.ChartInvalid):
        chat_chart.validate({**good, "chart_type": "spiral"})
    with pytest.raises(chat_chart.ChartInvalid):
        chat_chart.validate({**good, "schema_version": "other"})


def test_a_chart_that_cannot_be_built_leaves_the_table_block(monkeypatch):
    def refuse(*a, **kw):
        raise chat_chart.ChartInvalid("forced")
    monkeypatch.setattr("decissions.renderers.workplane.compiler.compile_chat", refuse)
    spec = compiled("category_metric_bar", platform_rows(3))
    assert spec["chart"] is None and spec["provenance"]["chart_status"] == "invalid"
    assert spec["block"]["rendererId"] == "workplane.echarts"


SAMPLE_FIELDS = [{"name": "asset_id", "role": "identifier", "dtype": "string"},
                 {"name": "complexity_score", "role": "measure", "dtype": "number", "semantics": "score",
                  "unit": "points", "score_scale": [0, 100]}]


def sample_rows():
    return [{"asset_id": f"a-{i:02d}", "complexity_score": (i * 37) % 100} for i in range(25)]


ORDERED_FIELDS = [{"name": "score_band", "role": "dimension", "dtype": "string", "order": ["low", "medium", "high"]},
                  PLATFORM_FIELDS[1]]

GOLDEN_CASES = {
    "category_metric_bar": ("category_metric_bar", lambda: platform_rows(4), PLATFORM_FIELDS),
    "category_metric_top_n_bar": ("category_metric_top_n_bar", lambda: platform_rows(30), PLATFORM_FIELDS),
    "category_share_pie": ("category_share_pie", lambda: platform_rows(3), PLATFORM_FIELDS),
    "time_metric_line": ("time_metric_line",
                         lambda: [{"run_date": f"2026-0{i}-01", "open_duplicates": 10 - i} for i in range(1, 5)],
                         RUN_FIELDS),
    "detail_table": ("detail_table", lambda: platform_rows(25), PLATFORM_FIELDS),
    "category_metric_horizontal_bar": ("category_metric_horizontal_bar", lambda: platform_rows(4), PLATFORM_FIELDS),
    "category_metric_lollipop": ("category_metric_lollipop", lambda: platform_rows(4), PLATFORM_FIELDS),
    "category_metric_pareto": ("category_metric_pareto", lambda: platform_rows(5), PLATFORM_FIELDS),
    "category_share_donut": ("category_share_donut", lambda: platform_rows(3), PLATFORM_FIELDS),
    "category_share_waffle": ("category_share_waffle", lambda: platform_rows(3), PLATFORM_FIELDS),
    **{t: (t, sample_rows, SAMPLE_FIELDS) for t in ("value_histogram", "value_box_plot", "value_strip_plot",
                                                    "value_beeswarm", "value_ecdf", "value_density")},
    "ordered_category_bar": ("ordered_category_bar", lambda: [{"score_band": b, "asset_count": n}
                                                               for b, n in (("high", 4), ("low", 9), ("medium", 6))],
                             ORDERED_FIELDS),
    **{t: (t, run_series_rows, RUN_SERIES_FIELDS) for t in ("time_metric_area", "time_metric_step_line",
                                                            "time_series_stacked_area",
                                                            "time_series_stacked_area_100")},
    "time_metric_sparkline": ("time_metric_sparkline",
                              lambda: [{"run_date": f"2026-0{i}-01", "open_duplicates": 10 - i} for i in range(1, 5)],
                              RUN_FIELDS),
    **{t: (t, crosstab_rows, CROSSTAB_FIELDS) for t in ("category_series_stacked_bar",
                                                        "category_series_stacked_bar_100",
                                                        "category_series_heatmap")},
    "link_graph": ("link_graph", similarity_rows, SIMILARITY_FIELDS),
    "link_graph_membership": ("link_graph", membership_rows, MEMBERSHIP_FIELDS),
    "flow_sankey": ("flow_sankey", flow_rows, FLOW_FIELDS),
    "group_treemap": ("group_treemap", membership_rows, MEMBERSHIP_FIELDS),
    "group_sunburst": ("group_sunburst", membership_rows, MEMBERSHIP_FIELDS),
    "group_tree": ("group_tree", lambda: membership_rows(("Candidate group 1",)), MEMBERSHIP_FIELDS),
}


@pytest.mark.parametrize("name", [*GOLDEN_CASES, "empty_state"])
def test_every_extracted_template_compiles_against_workplane_contract(name):
    if name == "empty_state":
        spec = compile_spec(EMPTY_STATE, [], ref(), fields=SPECS)
        assert spec["rows"] == []
    else:
        template, rows, fields = GOLDEN_CASES[name]
        source = rows()
        spec = compiled(template, source, fields)
        assert len(spec["rows"]) == len(source)
    if spec["chart"]:
        chat_chart.validate(spec["chart"]["payload"])
    assert spec["provenance"]["chart_status"] != "invalid"


def test_profile_builder_is_unchanged_by_chart_compilation():
    rows = platform_rows(3)
    before = build_profile(ProfileInput(fields=SPECS, rows=rows, total_rows=3, is_complete=True))
    compiled("category_metric_bar", rows)
    assert build_profile(ProfileInput(fields=SPECS, rows=rows, total_rows=3, is_complete=True)) == before

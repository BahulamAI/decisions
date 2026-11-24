"""Host-independent verification of exact-values Workplane snapshots."""
import json
from collections import Counter

from ..renderers.workplane.chat_chart import validate, payload_sha256


def validate_snapshot(data, snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("Invalid snapshot")
    names = [f.name for f in data.fields]
    if [c.get("name") for c in snapshot.get("columns", [])] != names:
        raise ValueError("Snapshot fields do not match")
    rows = snapshot.get("rows")
    if not isinstance(rows, list) or len(rows) != len(data.rows) or any(
            not isinstance(r, list) or len(r) != len(names) for r in rows):
        raise ValueError("Snapshot rows do not match")
    def encoded(row):
        return json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if Counter(map(encoded, rows)) != Counter(encoded([r.get(n) for n in names]) for r in data.rows):
        raise ValueError("Snapshot values do not match")
    chart = snapshot.get("chart")
    if chart is not None:
        if chart.get("contract") != "workplane.chat-chart/1":
            raise ValueError("Unsupported chart contract")
        validate(chart["payload"])
        if chart.get("payload_sha256") != payload_sha256(chart["payload"]):
            raise ValueError("Chart digest does not match")
    if not isinstance(snapshot.get("disclosures", []), list):
        raise ValueError("Invalid coverage disclosures")

# Decissions: reusable modules

The local repository directory, Python package and distribution are named `decissions`.
The GitHub repository is `BahulamAI/decissions`. This is an intentional import change: no `chat_safety`
compatibility package is shipped. CodeKepler and EntityBI adoption is a separate change.

## Implemented boundaries

| Module | Responsibility | Dependencies |
|---|---|---|
| `decissions.assessment`, `engine`, `registry`, `session` | Existing risk evidence, application policy and session behavior | Standard library; optional Jev HTTP adapter |
| `decissions.decisions` | Typed domain and selector interfaces, eligibility, bounded selection and decision receipts | Optional Pydantic |
| `decissions.presentations` | Generic field profiling, intent, chart eligibility and presentation orchestration | Decisions plus optional JSON Schema validation for the built-in catalog |
| `decissions.renderers.workplane` | Workplane chart/table compilation and contract validation | Presentation contracts, JSON Schema |
| `decissions.adapters.jev_decision` | Jev Choice among eligible candidates, with fit assessment | Decisions plus existing optional Jev transport |
| `decissions.adapters.jev_risk` | Existing risk-family assessment | Existing safety assessment types and Jev transport |

Assessment probabilities remain evidence. Chart selection is a separate candidate decision and never
grants access or runs tools. Data authorization, fetching, storage, replay, product policies and tenant
admission remain with the consuming application. Tenant strings in the in-process API are audit scope,
not authenticated permission grants. This implementation adds no HTTP service, database or deployment.

The decision interface can later support model routing, tool selection and other candidate-selection
domains. PII detection would require its own evidence/span contract and evaluator: it is not implemented
by reusing a chart choice or interpreting a risk score as a detected entity. Existing safety APIs remain
the first production-consumer use case; charts are the second implemented module.

## Extraction, not a competing implementation

Profiling, semantic guards, catalog loading, presentation policy, domain selection rules, core decision
contracts and Workplane compilation were extracted from the current `entitybi-decision-service` working
tree. `decision-extraction.json` records source paths and hashes, including uncommitted source work.
EntityBI result-kind enums, FileTask/workspace/run requirements, APIs, data access, persisted service state,
training infrastructure and product-specific domains were not imported. The original repository is retained
until consumers move; do not delete its code before cutover.

The built-in presentation catalog is an initial bounded policy, not a promise that every Workplane chart
can be selected. It supports comparisons, shares, distributions, trends, series, networks and hierarchies
subject to semantic guards. A chart renderer supporting a type does not automatically make that type
eligible. Business-specific category names are ordinary data, not built-in result types.

## Public use

Install from this checkout with `pip install '.[presentations]'`. Add `jev` for hosted selection:
`pip install '.[presentations,jev]'`. `pip install '.[dev]'` installs all local test dependencies.
No package publication is part of this change.

```python
from decissions.presentations import (
    PresentationService, ProfileInput, FieldSpec, ResultRef, Intent,
)

result = await PresentationService().present(
    request_id="chart-1", tenant_id="tenant-1",
    reference=ResultRef(result_id="authorized-result", revision=1,
                        content_hash="sha256:" + "a" * 64),  # use the actual host result hash
    data=ProfileInput(
        fields=[FieldSpec(name="region", role="dimension", dtype="string"),
                FieldSpec(name="orders", role="measure", dtype="integer",
                          semantics="count", additive=True)],
        rows=[{"region": "East", "orders": 12}, {"region": "West", "orders": 8}],
        is_complete=True, total_rows=2,
    ),
    intent=Intent(text="Compare orders by region"),
)
# result.decision: candidate, alternatives, rejection reasons and provenance
# result.rendering: Workplane snapshot with exact-values table and optional chart
# result.rendering_status: ready or table_fallback
```

An explicit eligible preference wins. An ineligible preference cannot force a misleading chart; the
receipt exposes why that candidate was rejected. Nulls stay unknown, scores cannot become shares,
similarity cannot become flow and incomplete denominators block part-to-whole charts.

The reference hash is host-provided provenance, not independent verification of source truth. The service
also hashes its bounded admitted data and binds that digest and renderer identity into the decision subject.
It snapshots data before awaiting a provider, so mutable caller rows cannot change between selection and
compilation. The library does not establish that the host fetched the correct data.

For Jev, construct `DecisionEngine(selector=JevDecisionSelector(api_key=..., model=...,
endpoint=...))` and pass it to `PresentationService`. The selector receives profile metadata,
trusted candidate descriptions and explicitly untrusted user intent—not raw result rows or category labels.
Field names, units and user intent may still contain sensitive content; the host must approve those disclosures.
Credentials and endpoint are explicitly configured, and no fallback to a different provider is performed.
No live Jev call has been made for this implementation; adapter tests use mocked HTTP responses.

## Renderer independence

Implement the `Renderer` protocol with `name`, `supports(candidate)` and
`compile(candidate, data, reference, intent, title)`. Eligibility is intersected with that adapter's
capabilities before selection. Every adapter must support the safe table/empty-state fallback.
Another renderer may reuse the built-in semantic catalog or introduce a separate trusted domain;
the decision kernel never imports Workplane or a product backend.

A selection failure returns the domain's eligible safe default. A compilation failure returns
`decision-platform.table/1`, with exact admitted values and disclosures.
Hosts must handle this renderer-independent fallback and supply an accessible table; the chart widget alone
is not the entire accessibility experience. A receipt records the attempted decision separately from rendering
status, so a compilation fallback is not reported as a successful chart rendering.

## Versions and compatibility

Existing assessment/policy/session wire schema names remain unchanged to avoid gratuitous evidence-format
changes. New decision/presentation schemas use `decision-platform.*`. Workplane payloads retain the upstream
`workplane.chat-chart/1` envelope. The Python import rename is intentionally breaking and requires backend
adoption later. No backend, frontend or consumer deployment was changed here.

The in-process engine supplies a deterministic receipt, not durable idempotency storage. Hosts own durable
replay and concurrency control. It does not import the original service's cache, auth, budgets or database.

## Verification

Run `PYTHONPATH=src python -m pytest -q`. Tests cover existing safety behavior, extracted chart eligibility
and compilation, selector failure/timeout, out-of-set choices, alternate renderers, tenant-separated hashes,
input bounds and Jev data minimization. The 30 compiled chart fixtures were also checked against the local
Workplane JavaScript validator. Browser visual/interaction acceptance and live-provider calibration remain
consumer-integration work.

The package rename retains existing wire schema identifiers (`chat-safety.*`, `decision-platform.*` and `workplane.chat-chart/1`). These identify contracts, not Python import paths. No legacy import package is shipped.

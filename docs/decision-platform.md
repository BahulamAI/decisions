# One runtime, policy-selected capabilities

`from decissions import Runtime` is the common package entry point. `decide(...)` and
`decide_many(...)` share policy admission, input validation, timeouts and response serialization.

```text
Runtime.decide / Runtime.decide_many
  -> pin policy/version, check tenant admission, snapshot input
  -> handler.prepare (typed validation, no I/O)
  -> handler.execute (bounded by policy deadline)
       safety.policy     -> existing SafetyGate and its policy
       safety.assessment -> existing RiskAssessor and its registry
       presentation      -> profile -> runtime selection -> renderer
       classification    -> runtime selection -> label or abstention
  -> decissions.response/1
```

Candidate selection has one implementation, `runtime/core/engine.py`. Safety retains its
assessment/action-policy semantics instead of being forced into a single-choice classifier.
Existing safety integrations and wire formats remain supported.

## Adopting an existing chat-safety integration

Replace the legacy `chat-safety[jev]` dependency with an immutable source pin to
`decissions[api,jev]` in every dependency manifest and regenerate the application's
lockfile. Import assessment contracts and `JevRiskEvaluator` from `decissions`.
Register `RiskAssessmentHandler(RiskAssessor(registry, evaluator))` under a versioned
policy, then invoke `Runtime.decide` for both family and selected-subfamily checks.
The policy version must equal the registry version; pass `family_ids` for the latter.

Keep the application's existing risk/intent batching, nudge/observe modes, history
selection and output buffering/rewrite policy. These are host behavior, not candidate
selection settings. The common envelope's `result` retains the assessment wire schema,
input/registry digests and model provenance. If `status` is unavailable and `result` is
empty, take the existing unavailable path; never treat missing evidence as permission
to release an output. A serialized assessment is JSON-compatible; consumers that need
the original dataclasses should validate it when decoding at their boundary.

CodeKepler's input and output assessment adapters use this pattern. Its gateway remains
the configured platform Decisions endpoint, and the library migration requires no
new gateway, credentials, database tables or policy thresholds. Package pins should be
updated before the backend image is rebuilt. Opening or merging a package PR does not
by itself update an already-running application image.

## Configure once, call uniformly

```python
from decissions import (
    Runtime, DecisionPolicy, DomainPolicy, SelectorKind,
    SafetyHandler, RiskAssessmentHandler, PresentationHandler, ClassificationHandler,
)

runtime = Runtime(
    policies=[
        DecisionPolicy(id="chat", version=gate.policy.version, handler="safety"),
        DecisionPolicy(id="risks", version=assessor.registry.version, handler="risks"),
        DecisionPolicy(id="chart", version="1", handler="charts"),
        DecisionPolicy(id="topics", version="1", handler="topics", selection=DomainPolicy(
            serving="hosted_model", external_inference_tenants=["tenant-1"],
            min_fit_probability=0.5, min_auto_confidence=0.6,
        )),
    ],
    handlers={
        "safety": SafetyHandler(gate),
        "risks": RiskAssessmentHandler(assessor),
        "charts": PresentationHandler(),
        "topics": ClassificationHandler(version="1",
            question="Which team should handle this message?",
            candidates={"billing": "Invoices and payments", "support": "Technical issues"}),
    },
    selectors={SelectorKind.HOSTED: selector},
    store=store,  # optional host-owned persistence
)

answer = await runtime.decide(
    policy="topics", policy_version="1", request_id="row-42", tenant_id="tenant-1",
    input={"text": "Please send my invoice"},
)
label = answer.result.get("label")  # None on abstention

assessment = await runtime.decide(
    policy="chat", policy_version=gate.policy.version,
    request_id="message-1", tenant_id="tenant-1",
    input={"content": "Explain the options", "actor_ref": "user-1", "stage": "input"},
)
```

`gate`, `assessor`, `selector` and `store` are host-configured objects. Omit unused handlers,
selectors and persistence. Default selection is rules; an unavailable classification provider
produces abstention, never an invented label. Example thresholds are not calibrated recommendations.
Runnable offline demos are `examples/unified.py` and `examples/sql_classification.py`.

Safety thresholds, hard rules, shadow/enforce mode and session behavior come from the installed
`PolicyBundle`. The route version must match that bundle or assessment registry. Classification
routes must match their category-definition version. Requests cannot override providers or policy.

## Common contract

Requests contain `policy`, `policy_version`, `request_id`, `tenant_id` and typed `input`.
Responses contain:

| Field | Meaning |
|---|---|
| `schema_version` | `decissions.response/1` |
| `request_id`, `tenant_id` | Correlation and scope |
| `policy`, `policy_version`, `policy_digest` | Selected route configuration |
| `kind` | Capability identifier |
| `status` | `complete`, `fallback` or `unavailable` |
| `result` | Capability-specific structured fields |
| `error_code` | Sanitized unavailability code |
| `latency_ms` | Execution time excluding the batch queue |

`complete` means the operation completed, not that its answer is safe/allowed/correct. A safety
refusal is a complete assessment. Consumers must inspect the action. Check status before accessing
result fields: an outer timeout returns unavailable with an empty result. Existing SafetyGate
provider errors retain its configured fallback behavior.

Classification returns `label`, `disposition`, `probabilities` and `decision`. Its internal abstention
option is excluded from the model's candidate distribution. Definitions are fingerprinted into the
domain revision. Presentations return `decision`, `rendering`, `renderer` and `rendering_status`.
Risk assessments retain `chat-safety.assessment/1` fields. Independent risk scores are never
renormalized into categorical probabilities.

## JSON policy configuration

```json
{
  "schema_version": "decissions.policies/1",
  "policies": [{
    "id": "topics", "version": "1", "handler": "topics",
    "tenants": ["tenant-1"], "timeout_seconds": 10,
    "selection": {
      "serving": "hosted_model",
      "external_inference_tenants": ["tenant-1"],
      "min_fit_probability": 0.5
    }
  }]
}
```

Use `load_decision_policies(path)` and supply a trusted handler mapping to Runtime. Unknown
handlers fail startup; duplicate IDs/JSON keys and unknown fields are rejected. `tenants: null`
allows any host-authenticated tenant to use the route; `tenants: []` permits none. `enabled: false`
disables it. Hosted candidate selection additionally requires `external_inference_tenants`.
Route selection overrides domain/global settings. Runtime policy digests namespace replay keys.

The global `rules_only` switch and hosted selector admission govern candidate selection. Safety
evaluators are explicitly installed through their handlers and retain their existing policies and
host configuration. A route cannot change a safety gate's mode or thresholds through `selection`.

With a store, the runtime retains cache/replay, policies, budgets, circuit breakers and shadow
comparisons. The host store owns atomic replay/durability. With no store there is no implicit replay
or shadow persistence. Call `await runtime.drain()` before closing host-owned resources when shadows
are enabled. A policy's tenant string scopes work; the host must authenticate it.

## Provider setup

```python
import httpx
from decissions import JevSelector, SelectorKind

async with httpx.AsyncClient() as client:
    selector = JevSelector(client=client, api_key=trusted_api_key,
        url=trusted_decisions_endpoint, model=pinned_model)
    # Install {SelectorKind.HOSTED: selector} and use Runtime while client is open.
```

There is one categorical Jev adapter: Choice + companion fit + optional domain signals. It validates
candidate coverage, normalization, answer types, fit coverage and model revision. Safety's Boolean
adapters retain their distinct contracts. Tests use mocked providers; they do not calibrate live models.

## SQL-row classification and batches

[MotherDuck's integration](https://motherduck.com/blog/motherduck-supports-jev/) classifies text
columns inside SQL. This package provides classification through Python: fetch authorized rows,
build one request per row and call `decide_many`. Use bounded `fetchmany` reads for large tables.

`examples/sql_classification.py` demonstrates read/classify/store using SQLite in memory and a
synthetic selector. It preserves row identity and stores status plus receipts; abstentions have null
labels. It does not install a native DuckDB/MotherDuck UDF or claim the linked benchmark's performance.
A database-specific adapter can call this same runtime later.

Batches default to at most 500 requests and concurrency 16. Results retain input order. Preflight
rejects invalid policies/input before execution; individual provider failures preserve other results.
IDs must be unique per tenant within a batch. Cancellation propagates.

## Extensions and migration

Implement `DecisionHandler`: `kind`, synchronous `prepare(request, policy)` without I/O, and async
`execute(prepared, request, policy, engine)` returning `DecisionOutcome`. Register its name and a
policy. Candidate-based extensions implement `decissions.contracts.DecisionDomain` and call the
supplied engine rather than adding an execution implementation.

| Module | Responsibility |
|---|---|
| `decissions` | Shallow imports for Runtime, policies, request/response and handlers |
| `runtime` | Common API, the selection engine, provider/persistence/operational support |
| `contracts` | Domain interfaces, candidate/receipt types and review contracts |
| `assessment`, `engine`, `session` | Preserved safety behavior |
| `presentations` | Chart profiling, eligibility and saved-presentation validation |
| `renderers.workplane` | Rendering compiler and contract |
| `classification` | Configured text classification |

The inner `decisions` package is renamed to `contracts`. `DecisionEngine` and `JevDecisionSelector`
are removed without aliases. Direct PresentationService calls now use the same runtime engine;
new integrations should use Runtime for all capabilities. Historical extraction manifests record
original paths, not the current module layout. No database, HTTP server or deployment is added.

Install `.[api,presentations]`; run `PYTHONPATH=src python -m pytest -q` and the two examples. Existing
safety tests remain, alongside common API, mixed-batch, classification and malformed-provider tests.
The host retains authentication, trusted context, data access, action execution and retention.

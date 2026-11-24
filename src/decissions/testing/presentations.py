"""Shared evaluation harness: runs any selector through the REAL pipeline (profile -> eligibility ->
engine validation/fallback -> compile) on identical cases, so selectors are compared like for like
(PRD-64 §10: "Also measure the full pipeline")."""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import time
from dataclasses import asdict, dataclass, field
import json
from typing import Any

from decissions.runtime.config import SelectorKind, Settings
from decissions.runtime.core.engine import Engine
from decissions.contracts.models import DecisionContext, Eligibility
from decissions.renderers.workplane.compiler import CompileError, compile_spec
from decissions.presentations.contract import Intent, ResultRef
from decissions.presentations.domain import PresentationDomain, PresentationInput, candidate_of
from decissions.presentations.profile import FieldSpec, ProfileInput, build_profile
from decissions.runtime.persistence.store import LRUCache
from .store import MemoryStore
from decissions.runtime.selectors.base import Selector

@dataclass
class Case:
    case_id: str
    family: str
    result_kind: str
    true_intent: str
    difficulty: str
    intent_text: str
    audience: str
    fields: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    total_rows: int | None
    is_complete: bool
    comparable_runs: bool
    acceptable: list[str] = field(default_factory=list)
    preferred: str = ""
    critical_if: list[str] = field(default_factory=list)  # oracle-prohibited templates
    slices: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def to_request(c: Case) -> tuple[DecisionContext, ResultRef]:
    profile = build_profile(ProfileInput(
        fields=[FieldSpec(**f) for f in c.fields], rows=c.rows, total_rows=c.total_rows,
        is_complete=c.is_complete, comparable_runs=c.comparable_runs))
    h = hashlib.sha256(c.to_json().encode()).hexdigest()
    ref = ResultRef(result_id=c.case_id,
                    revision=1, content_hash=f"sha256:{h}", result_kind=c.result_kind)
    inp = PresentationInput(result_ref=ref, intent=Intent(text=c.intent_text, audience=c.audience), profile=profile)
    ctx = DecisionContext(domain=PresentationDomain.name, request_id=c.case_id, tenant_id="eval",
                          subject=PresentationDomain.subject_for(ref), input=inp)
    return ctx, ref


@dataclass
class Outcome:
    case_id: str
    template: str
    source: str
    fallback: str
    acceptable: bool
    preferred: bool
    critical: bool
    confidence: float | None
    top_prob: float | None
    latency_ms: float
    compile_ok: bool
    slices: list[str] = field(default_factory=list)


async def run(cases: list[Case], kind: SelectorKind, selector: Selector, settings: Settings,
              domain: PresentationDomain, concurrency: int = 8) -> list[Outcome]:
    s = settings.model_copy(update={"serving_selector": kind, "shadow_selectors": [],
                                    "external_inference_tenants": ["eval"]})
    engine = Engine(s, MemoryStore(), {SelectorKind.RULES: selector, kind: selector}, LRUCache(1))
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> Outcome:
        req, ref = to_request(c)
        async with sem:
            t = time.perf_counter()
            d = await engine.decide(domain, req)
            lat = (time.perf_counter() - t) * 1000
        try:
            compile_spec(candidate_of(d.option), c.rows, ref, fields=[FieldSpec(**f) for f in c.fields])
            ok = True
        except CompileError:
            ok = False
        tid = d.option.group
        return Outcome(c.case_id, tid, d.selection_source.value, d.fallback_reason.value,
                       tid in c.acceptable, tid == c.preferred, tid in c.critical_if,
                       d.provider_confidence, (d.probabilities or {}).get(d.option_id), lat, ok, c.slices)

    return list(await asyncio.gather(*(one(c) for c in cases)))


async def permutation_invariance(cases: list[Case], selector: Selector, domain: PresentationDomain,
                                 n: int = 50, seed: int = 7) -> float:
    """Shuffle candidate order: a sound selector picks the same candidate regardless of order."""
    rng, same = random.Random(seed), 0
    sample = [c for c in cases if c.rows][:n]
    for c in sample:
        req, _ = to_request(c)
        elig = domain.eligibility(req.input)
        a = await selector.select(domain, req, elig, 2.0)
        shuffled = list(elig.options)
        rng.shuffle(shuffled)
        b = await selector.select(domain, req, Eligibility(shuffled, elig.ineligible, elig.safe_default_id), 2.0)
        same += a.option_id == b.option_id
    return same / max(1, len(sample))


# ------------------------------------------------------------------ statistics
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def paired_bootstrap(a: list[bool], b: list[bool], iters: int = 2000, seed: int = 1) -> tuple[float, float, float]:
    """Mean(b - a) with a 95% percentile interval over paired resamples."""
    rng = random.Random(seed)
    n = len(a)
    diffs = [int(y) - int(x) for x, y in zip(a, b, strict=True)]
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(iters))
    return sum(diffs) / n, means[int(.025 * iters)], means[int(.975 * iters)]


def brier_and_ece(probs: list[float], correct: list[bool], bins: int = 10) -> tuple[float, float]:
    if not probs:
        return float("nan"), float("nan")
    brier = sum((p - int(y)) ** 2 for p, y in zip(probs, correct, strict=True)) / len(probs)
    ece = 0.0
    for b in range(bins):
        idx = [i for i, p in enumerate(probs) if b / bins <= p < (b + 1) / bins or (b == bins - 1 and p == 1)]
        if idx:
            conf = sum(probs[i] for i in idx) / len(idx)
            acc = sum(correct[i] for i in idx) / len(idx)
            ece += len(idx) / len(probs) * abs(conf - acc)
    return brier, ece


def summarize(outs: list[Outcome]) -> dict[str, Any]:
    n = len(outs)
    if not n:
        raise ValueError("Evaluation requires at least one outcome")
    acc = sum(o.acceptable for o in outs)
    lat = sorted(o.latency_ms for o in outs)
    slices: dict[str, list[Outcome]] = {}
    for o in outs:
        for s in o.slices:
            slices.setdefault(s, []).append(o)
    auto = [o for o in outs if o.source != "fallback"]
    return {
        "n": n,
        "acceptable_rate": acc / n, "acceptable_ci95": wilson(acc, n),
        "preferred_rate": sum(o.preferred for o in outs) / n,
        "critical_errors": sum(o.critical for o in outs),
        "compile_failures": sum(not o.compile_ok for o in outs),
        "coverage_auto": len(auto) / n,
        "fallback_rate": 1 - len(auto) / n,
        "latency_p50_ms": lat[n // 2], "latency_p95_ms": lat[int(n * .95) - 1],
        "slices": {k: {"n": len(v), "acceptable_rate": sum(o.acceptable for o in v) / len(v),
                       "ci95": wilson(sum(o.acceptable for o in v), len(v))} for k, v in sorted(slices.items())},
    }

"""Cheap, deterministic intent signals (0..1). Used by the rules selector and as in-house ranker
features. The hosted model can supply the same signals as Noul probabilities, which is how the
in-house model learns from Jev (distillation) without ever copying its weights."""

from __future__ import annotations

import re

SIGNALS = ("compare", "trend", "share", "inspect", "rank")

_PATTERNS: dict[str, tuple[str, ...]] = {
    "compare": (r"\bcompar", r"\bvs\.?\b", r"\bversus\b", r"\bacross\b", r"\bby platform\b", r"\beach\b",
                r"\bbreak ?down\b", r"\bhow many\b"),
    "trend": (r"\bover time\b", r"\btrend", r"\bchange[ds]?\b", r"\bbetween runs\b", r"\bgrow",
              r"\bsince\b", r"\bhistor", r"\bmonth", r"\bweek", r"\brun[- ]over[- ]run\b"),
    "share": (r"\bshare\b", r"\bpercent", r"\bproportion", r"\bportion\b", r"\bpart of\b", r"%",
              r"\bpie\b", r"\bfraction\b", r"\bsplit\b", r"\bmix\b"),
    "inspect": (r"\bshow me (the )?(list|details|candidates)\b", r"\bevidence\b", r"\bwhich assets\b",
                r"\blist\b", r"\bdetails?\b", r"\binspect", r"\bwhy\b", r"\bcandidates?\b", r"\bexact(ly)?\b"),
    "rank": (r"\btop\b", r"\bhighest\b", r"\blowest\b", r"\bworst\b", r"\bbest\b", r"\brank",
             r"\blargest\b", r"\bbiggest\b", r"\bthe (most|least)\b", r"\bfewest\b"),
}
_COMPILED = {k: [re.compile(p, re.I) for p in v] for k, v in _PATTERNS.items()}


def keyword_signals(text: str) -> dict[str, float]:
    out = {}
    for name, pats in _COMPILED.items():
        hits = sum(1 for p in pats if p.search(text))
        out[name] = min(1.0, hits / 2) if hits else 0.0
    return out


# Same five signals as Jev Nouls (asked in the same request as the Choice).
INTENT_QUESTIONS = {
    "compare": "The user wants to compare a metric across categories such as platforms or assets.",
    "trend": "The user wants to see how something changed over time or across analysis runs.",
    "share": "The user wants proportions, percentages or shares of a whole.",
    "inspect": "The user wants to inspect individual records, candidates or their evidence.",
    "rank": "The user wants the top, bottom, largest or smallest items.",
}

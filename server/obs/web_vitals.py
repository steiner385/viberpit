"""Field Core Web Vitals: thresholds, percentile and segmentation (#1411).

Pure functions over samples. The ingest route (`server/api/web_vitals.py`)
stores rows and this module turns them into the p75s the issue's targets are
written in; nothing here touches the DB, so the arithmetic and the thresholds
are testable — and pinned — without an app.

## Why the thresholds live here and not in the browser

`web-vitals` hands the browser a good/needs-improvement/poor rating with every
sample, and shipping it would have been free. But a stored value has to be
classifiable later too — when the window moves, when a threshold is retuned,
when a row predates the bundle that sent it — so the server needs the table
regardless. Two copies of a threshold is one copy too many (constitution VI), so
the client sends the number and this module is the only thing that judges it.

Values are Google's published Core Web Vitals thresholds: the first bound is the
"good" ceiling, the second the "poor" floor.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

Rating = Literal["good", "needs-improvement", "poor"]

# metric -> (good ceiling, poor floor). LCP/INP/TTFB/FCP in milliseconds; CLS unitless.
WEB_VITAL_THRESHOLDS: dict[str, tuple[float, float]] = {
    "LCP": (2500.0, 4000.0),
    "INP": (200.0, 500.0),
    "CLS": (0.1, 0.25),
    "TTFB": (800.0, 1800.0),
    "FCP": (1800.0, 3000.0),
}

WEB_VITAL_METRICS: tuple[str, ...] = tuple(WEB_VITAL_THRESHOLDS)

# The three #1411's target is stated over. TTFB and FCP are collected because they
# are how you tell a slow LCP apart from a slow server, not because they are graded.
CORE_WEB_VITALS: tuple[str, ...] = ("LCP", "INP", "CLS")

# Below this, a p75 is one unlucky load rather than a measurement. It is still
# reported — hiding it would look like "no data" — but it carries no verdict.
MIN_SAMPLES_FOR_VERDICT = 10

# The dimensions a summary may be segmented by; anything else is a caller bug
# (and, for the API route, an injection of an arbitrary attribute name).
SEGMENTS: tuple[str, ...] = ("route", "device", "browser")


@dataclass(frozen=True, slots=True)
class Sample:
    """One reported measurement, stripped to what may be aggregated."""

    metric: str
    value: float
    route: str
    device: str
    browser: str


def rate(metric: str, value: float) -> Rating | None:
    """Band a value, or None for a metric with no published threshold.

    Boundaries are inclusive at the good end, matching how CrUX reports them: an
    LCP of exactly 2500ms is good, 2500.1ms is not.
    """
    bounds = WEB_VITAL_THRESHOLDS.get(metric)
    if bounds is None:
        return None
    good, poor = bounds
    if value <= good:
        return "good"
    if value <= poor:
        return "needs-improvement"
    return "poor"


def p75(values: Sequence[float] | Iterable[float]) -> float | None:
    """The 75th percentile by nearest rank, or None when there is nothing to rank.

    Nearest rank (rather than an interpolating percentile) is deliberate: it is
    what CrUX and the Web Vitals tooling report, so a number computed here is
    comparable with one read off a Chrome dashboard. It also always returns a
    value that was actually observed, which matters when the answer is quoted as
    "the experience at p75".

    None rather than 0.0 for the empty case: a zero CLS is a perfect score, and
    an absent measurement that renders as a perfect score is worse than no
    measurement at all.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(0.75 * len(ordered)))
    return ordered[rank - 1]


def _aggregate(values: list[float], metric: str) -> dict:
    value = p75(values)
    rating = rate(metric, value) if value is not None else None
    meets: bool | None = None
    if rating is not None and len(values) >= MIN_SAMPLES_FOR_VERDICT:
        meets = rating == "good"
    return {
        "p75": value,
        "count": len(values),
        "rating": rating,
        # None means "not enough samples to say", which is NOT the same as False.
        "meetsTarget": meets,
    }


def summarize(samples: Iterable[Sample]) -> dict[str, dict]:
    """Per-metric p75 over every sample given.

    Every known metric appears in the result even with no samples: a metric that
    silently vanishes from a dashboard reads as "fine" when it actually means the
    collector stopped reporting it.
    """
    buckets: dict[str, list[float]] = {m: [] for m in WEB_VITAL_METRICS}
    for s in samples:
        bucket = buckets.get(s.metric)
        if bucket is not None:
            bucket.append(s.value)
    return {metric: _aggregate(values, metric) for metric, values in buckets.items()}


def summarize_by(samples: Iterable[Sample], dimension: str) -> dict[str, dict[str, dict]]:
    """`summarize`, grouped by one of `SEGMENTS`.

    #1411 asks for p75 "segmented by browser/device/route" because that is where
    the answer differs: an LCP that is fine on a desktop and awful on a phone is
    invisible in the pooled number, and acting on the pooled number is how a
    regression ships to exactly the users who cannot absorb it.
    """
    if dimension not in SEGMENTS:
        raise ValueError(f"cannot segment by {dimension!r}; expected one of {SEGMENTS}")
    grouped: dict[str, list[Sample]] = {}
    for s in samples:
        grouped.setdefault(getattr(s, dimension), []).append(s)
    return {key: summarize(rows) for key, rows in sorted(grouped.items())}


# Ordered because the strings nest: every Edge UA also says Chrome, and every
# Chrome UA also says Safari. First match wins.
_BROWSER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("edge", re.compile(r"\bEdgA?i?o?s?/|\bEdg/")),
    ("opera", re.compile(r"\bOPR/|\bOpera[/ ]")),
    ("firefox", re.compile(r"\bFirefox/|\bFxiOS/")),
    ("chrome", re.compile(r"\bChrome/|\bCriOS/|\bChromium/")),
    ("safari", re.compile(r"\bSafari/")),
)


def browser_family(user_agent: str | None) -> str:
    """The browser family of a User-Agent, and never anything narrower.

    A full UA string is a durable fingerprint and a version number nobody
    aggregates by; the family is the whole of what "segmented by browser" needs.
    Parsing it here rather than in the browser keeps the client payload free of a
    field that would have to be sanitised on arrival anyway.
    """
    if not user_agent:
        return "unknown"
    for family, pattern in _BROWSER_PATTERNS:
        if pattern.search(user_agent):
            return family
    return "other"

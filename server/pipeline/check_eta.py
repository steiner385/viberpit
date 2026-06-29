"""Pure per-check estimate: weight, progress, and a p10/p50/p90 remaining range.

The single source for "remaining time for one check" — consumed by the PR-level
estimator (aggregated) and the per-row drill-down (per running row). Ported from
estimator.compute_progress's inner loop, generalised from p50/p90 to p10/p50/p90.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from server.pipeline.history import Expected, median, percentile

RUNNING_CAP = 0.97
OVERDUE_FACTOR = 1.5
DEFAULT_DURATION = 60.0


@dataclass(frozen=True)
class CheckEstimate:
    weight: float          # p50 used for percent weighting
    progress: float        # 0..1
    rem_low: float         # seconds remaining, optimistic (p10)
    rem_nominal: float     # seconds remaining, nominal (p50)
    rem_high: float        # seconds remaining, pessimistic (p90)
    overdue: bool
    unfinished: bool


def _parse_epoch_s(iso: str | None) -> float:
    if not iso:
        return float("nan")
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


def check_estimate(
    check: dict | None,
    expected: Expected | None,
    samples: list[float],
    now: datetime,
    fallback: float,
    queue_delay: float | None = None,
) -> CheckEstimate | None:
    c = check
    if c is not None and c.get("conclusion") == "SKIPPED":
        return None

    p10 = expected.p10 if expected else fallback
    p50 = expected.p50 if expected else fallback
    p90 = expected.p90 if expected else p50
    weight = p50
    status = c.get("status") if c else None
    overdue = False
    now_s = now.timestamp()

    if status == "COMPLETED":
        return CheckEstimate(weight, 1.0, 0.0, 0.0, 0.0, False, False)

    if status == "IN_PROGRESS" and c and c.get("started_at"):
        raw_elapsed = now_s - _parse_epoch_s(c["started_at"])
        if raw_elapsed != raw_elapsed:  # NaN
            return CheckEstimate(weight, 0.0, p10, p50, p90, False, True)
        elapsed = max(raw_elapsed, 0.0)
        progress = min(elapsed / p50, RUNNING_CAP) if p50 else RUNNING_CAP
        vals = samples or []
        qualifying = [v for v in vals if v > elapsed] if len(vals) >= 5 else None
        if qualifying is not None and len(qualifying) == 0:
            if len(vals) >= 10:
                overdue = True
            elif elapsed > OVERDUE_FACTOR * p50:
                overdue = True
            rem_low = rem_nom = rem_high = 0.0
        elif qualifying is not None and len(qualifying) >= 2:
            sq = sorted(qualifying)
            rem_low = max(percentile(sq, 0.1) - elapsed, 0.0)
            rem_nom = median(sq) - elapsed
            rem_high = percentile(sq, 0.9) - elapsed
            denom = elapsed + rem_nom
            progress = min(elapsed / denom, RUNNING_CAP) if denom else RUNNING_CAP
        else:
            if elapsed > OVERDUE_FACTOR * p50:
                overdue = True
            rem_low = max(p10 - elapsed, 0.0)
            rem_nom = max(p50 - elapsed, 0.0)
            rem_high = max(p90 - elapsed, 0.0)
        return CheckEstimate(weight, progress, rem_low, rem_nom, rem_high, overdue, True)

    # queued, or expected-from-history but not yet in the rollup
    delay = (queue_delay or 0.0)
    return CheckEstimate(weight, 0.0, p10 + delay, p50 + delay, p90 + delay, False, True)

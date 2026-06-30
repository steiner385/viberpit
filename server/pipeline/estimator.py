"""Port of Plotroom's estimator/progress.ts — the CI-progress ETA.

Pure: (checks, expected_set, lookup, now, samples, queue_delay) →
ProgressResult(percent, eta_seconds nominal, eta_low_seconds, eta_high_seconds, overdue).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from server.pipeline.check_eta import (
    DEFAULT_DURATION, check_estimate,
)
from server.pipeline.history import Expected, median


@dataclass(frozen=True)
class ProgressResult:
    percent: int
    eta_seconds: int | None        # nominal (p50) — unchanged meaning
    eta_low_seconds: int | None     # optimistic (p10)
    eta_high_seconds: int | None    # pessimistic (p90)
    overdue: bool


def compute_progress(
    checks: list[dict],
    expected_set: list[str],
    lookup: Callable[[str], Expected | None],
    now: datetime,
    samples: Callable[[str], list[float]] | None = None,
    queue_delay: Callable[[str], float | None] | None = None,
) -> ProgressResult:
    live = {c["name"]: c for c in checks}
    names = set(expected_set) | {c["name"] for c in checks}
    known_p50s = [e.p50 for e in (lookup(n) for n in names) if e is not None]
    fallback = median(known_p50s) if known_p50s else DEFAULT_DURATION

    w_sum = p_sum = eta_low = eta_nom = eta_high = 0.0
    overdue = any_unfinished = False

    for name in names:
        est = check_estimate(
            live.get(name), lookup(name),
            (samples(name) if samples else []) or [],
            now, fallback,
            queue_delay(name) if queue_delay else None,
        )
        if est is None:  # SKIPPED
            continue
        w_sum += est.weight
        p_sum += est.weight * est.progress
        eta_low = max(eta_low, est.rem_low)
        eta_nom = max(eta_nom, est.rem_nominal)
        eta_high = max(eta_high, est.rem_high)
        overdue = overdue or est.overdue
        any_unfinished = any_unfinished or est.unfinished

    percent = round((p_sum / w_sum) * 100) if w_sum else 100
    if not any_unfinished:
        return ProgressResult(100, 0, 0, 0, False)
    if overdue:
        return ProgressResult(percent, None, None, None, True)
    return ProgressResult(percent, round(eta_nom), round(eta_low), round(eta_high), False)

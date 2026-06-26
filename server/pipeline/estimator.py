"""Port of Plotroom's estimator/progress.ts — the CI-progress ETA.

Pure: (checks, expected_set, lookup, now, samples, queue_delay) → (percent, eta, overdue).
Only percent/eta_seconds/overdue are surfaced (failed/etaRange dropped — see spec §2).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from server.pipeline.history import Expected, median, percentile

RUNNING_CAP = 0.97
OVERDUE_FACTOR = 1.5
DEFAULT_DURATION = 60.0
_FAILED = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED"}


@dataclass(frozen=True)
class ProgressResult:
    percent: int
    eta_seconds: int | None
    overdue: bool


def _parse_epoch_s(iso: str | None) -> float:
    if not iso:
        return float("nan")
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


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

    w_sum = p_sum = eta_p50 = eta_p90 = 0.0
    overdue = any_unfinished = False
    now_s = now.timestamp()

    for name in names:
        c = live.get(name)
        if c is not None and c.get("conclusion") == "SKIPPED":
            continue
        exp = lookup(name)
        p50 = exp.p50 if exp else fallback
        p90 = exp.p90 if exp else p50
        status = c.get("status") if c else None
        if status == "COMPLETED":
            progress, rem50, rem90 = 1.0, 0.0, 0.0
        elif status == "IN_PROGRESS" and c.get("started_at"):
            raw_elapsed = now_s - _parse_epoch_s(c["started_at"])
            if raw_elapsed != raw_elapsed:  # NaN
                progress, rem50, rem90 = 0.0, p50, p90
                any_unfinished = True
            else:
                elapsed = max(raw_elapsed, 0.0)
                progress = min(elapsed / p50, RUNNING_CAP) if p50 else RUNNING_CAP
                any_unfinished = True
                vals = (samples(name) if samples else []) or []
                qualifying = [v for v in vals if v > elapsed] if len(vals) >= 5 else None
                if qualifying is not None and len(qualifying) == 0:
                    if len(vals) >= 10:
                        # ≥10 samples and elapsed exceeded every one — overdue regardless of 1.5×p50
                        overdue = True
                    elif elapsed > OVERDUE_FACTOR * p50:
                        # 5–9 samples with zero qualifying: fall back to legacy 1.5×p50 overdue rule
                        overdue = True
                    rem50 = rem90 = 0.0
                elif qualifying is not None and len(qualifying) >= 2:
                    sq = sorted(qualifying)
                    rem50 = median(sq) - elapsed
                    rem90 = percentile(sq, 0.9) - elapsed
                    progress = min(elapsed / (elapsed + rem50), RUNNING_CAP) if (elapsed + rem50) else RUNNING_CAP
                else:
                    # <5 samples total, or exactly 1 qualifying — current p50/p90 logic
                    if elapsed > OVERDUE_FACTOR * p50:
                        overdue = True
                    rem50 = max(p50 - elapsed, 0.0)
                    rem90 = max(p90 - elapsed, 0.0)
        else:
            # queued, or expected-from-history but not yet in the rollup
            delay = (queue_delay(name) if queue_delay else None) or 0.0
            progress, rem50, rem90 = 0.0, p50 + delay, p90 + delay
            any_unfinished = True
        w_sum += p50
        p_sum += p50 * progress
        eta_p50 = max(eta_p50, rem50)
        eta_p90 = max(eta_p90, rem90)

    percent = round((p_sum / w_sum) * 100) if w_sum else 100
    if not any_unfinished:
        return ProgressResult(100, 0, False)
    if overdue:
        return ProgressResult(percent, None, True)
    return ProgressResult(percent, round(eta_p50), False)

"""Check-tier state-machine verdicts (spec Part 3). Pure.

Graduation and demotion thresholds deliberately differ (2% vs 5% sustained) —
hysteresis so a borderline check never oscillates between tiers."""
from __future__ import annotations

from .types import CheckStats, GateThresholds


def check_verdict(stats: CheckStats, *, is_required: bool,
                  th: GateThresholds) -> str | None:
    """One of 'graduate' | 'demote' | 'retire' | None for a single check."""
    if is_required:
        if stats.recent_flake_pct >= th.demote_flake_pct:
            return "demote"
        return None
    if (stats.window_days >= th.min_window_days
            and stats.runs_total >= th.min_runs
            and stats.default_branch_failures == 0
            and stats.flake_pct < th.max_flake_pct
            and stats.p95_duration_s <= th.max_p95_minutes * 60.0
            and not stats.flake_rising):
        return "graduate"
    if stats.window_days >= th.retire_days and stats.flake_pct >= th.max_flake_pct:
        return "retire"
    return None

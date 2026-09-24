"""Check-tier state-machine verdicts (spec Part 3). Pure.

Graduation and demotion thresholds deliberately differ (2% vs 5% sustained) —
hysteresis so a borderline check never oscillates between tiers."""
from __future__ import annotations

from .types import CheckStats, GateThresholds


def check_verdict(stats: CheckStats, *, is_required: bool,
                  th: GateThresholds, folded: bool = False) -> str | None:
    """One of 'graduate' | 'demote' | 'retire' | None for a single check.

    ``folded``: some other job ``needs:`` this one (see ``cicd.needs``), so its
    result is already part of that dependent's — on a repo that gates merges
    through one aggregator (``ci`` -> ``ci-gate``) every child would otherwise
    read as an advisory check and be proposed for graduation or retirement on
    its own. Its tier is decided by the parent. A check that IS required keeps
    the demote path regardless.

    Graduation evidence is PR runs only (``pr_runs_total``): a job that runs on a
    schedule or a ``workflow_run`` can be perfectly stable and still never gate
    a pull request.
    """
    if is_required:
        if stats.recent_flake_pct >= th.demote_flake_pct:
            return "demote"
        return None
    if folded:
        return None
    if (stats.window_days >= th.min_window_days
            and stats.runs_total >= th.min_runs
            and stats.pr_runs_total >= th.min_runs
            and stats.default_branch_failures == 0
            and stats.flake_pct < th.max_flake_pct
            and stats.p95_duration_s <= th.max_p95_minutes * 60.0
            and not stats.flake_rising):
        return "graduate"
    if stats.window_days >= th.retire_days and stats.flake_pct >= th.max_flake_pct:
        return "retire"
    return None

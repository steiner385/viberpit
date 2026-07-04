"""The cicd-doctor automation: system-level CI health — flaky tests, advisory-
check graduation, waste. Proactive/per-system, where pr-doctor is reactive/
per-PR (it never touches an individual PR). v1 is propose-only: every verdict
is a finding row + optional ci-doctor GitHub issue, never a mutation."""
from __future__ import annotations

import logging
from typing import Any

from .base import AutomationStatus, ConfigField

log = logging.getLogger(__name__)


class CicdDoctor:
    id = "cicd-doctor"
    name = "CI/CD Doctor"
    description = ("Watches CI health across repos: flaky tests, advisory-check "
                   "stability and graduation, and workflow waste.")
    modes: tuple[str, ...] = ("off", "propose")
    config_schema = [
        ConfigField(key="enabled_repos", label="Repositories", type="repo-multiselect",
                    default=[], help="Limit to these repos (empty = all ccswitch-managed)."),
        ConfigField(key="file_issues", label="File GitHub issues", type="bool", default=True,
                    help="Actionable findings become ci-doctor labeled issues."),
        ConfigField(key="min_window_days", label="Graduation window (days)", type="int",
                    default=21),
        ConfigField(key="min_runs", label="Graduation min runs", type="int", default=30),
        ConfigField(key="max_flake_pct", label="Graduation max flake %", type="int", default=2),
        ConfigField(key="max_p95_minutes", label="Graduation max p95 (min)", type="int",
                    default=10),
        ConfigField(key="demote_flake_pct", label="Demotion flake %", type="int", default=5,
                    help="Sustained over the demotion window (hysteresis vs graduation)."),
        ConfigField(key="demote_window_days", label="Demotion window (days)", type="int",
                    default=7),
        ConfigField(key="retire_days", label="Retirement age (days)", type="int", default=60),
        ConfigField(key="flaky_test_min_pct", label="Flaky-test threshold %", type="int",
                    default=2),
        ConfigField(key="retention_days", label="History retention (days)", type="int",
                    default=90),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "cicd_doctor_counts", None) if ctx is not None else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        open_n, cand = counts.get("open", 0), counts.get("candidates", 0)
        bits = [f"{open_n} open finding(s)"]
        if cand:
            bits.append(f"{cand} graduation candidate(s)")
        return AutomationStatus(text=", ".join(bits),
                                level="active" if open_n else "idle", counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        # The orchestrator's loop reads mode/config each tick; nothing to start here.
        ...

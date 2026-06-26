"""Assemble the CI-progress ETA for one PR — mirrors poller.ts's ciProgress call site."""
from __future__ import annotations

from datetime import datetime

from server.pipeline.estimator import compute_progress
from server.pipeline.history import HistoryStore
from server.pipeline.required import (
    build_expected_set, effective_prefixes, required_checks, rollup_workflow_for,
)

_EVENT = "pull_request"


def compute_pr_eta(
    store: HistoryStore, repo: str, checks: list[dict], now: datetime
) -> tuple[int | None, int | None, bool]:
    prefixes = effective_prefixes(repo)
    rollup_wf = rollup_workflow_for(repo)
    req = required_checks(checks, prefixes, rollup_wf)
    history_set = store.expected_set(repo, _EVENT, now)
    expected_set = build_expected_set(history_set, checks, prefixes, rollup_wf, now)
    result = compute_progress(
        req, expected_set,
        lookup=lambda n: store.expected(repo, n, _EVENT),
        now=now,
        samples=lambda n: store.samples(repo, n, _EVENT),
        queue_delay=lambda n: store.expected_runner_wait(repo, n, _EVENT),
    )
    return result.eta_seconds, result.percent, result.overdue

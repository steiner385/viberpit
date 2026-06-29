"""Pure builder: raw GitHub check dicts -> CheckGroup/CheckRow for the drill-down.

Reads expected per-check durations from HistoryStore (the same p50 history the ETA
estimator uses) for the overdue heuristic and never re-derives stage state.
"""
from __future__ import annotations

import math
from datetime import datetime

from server.github.pr_status import CheckGroup, CheckRow, _CONCLUSION_MAP

RUNNER_RATE_USD_PER_MIN = 0.008  # GitHub-hosted Linux 2-core list price (2026-06); estimate only.
_EVENT = "pull_request"


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalise_conclusion(raw: str | None) -> str:
    if raw is None:
        return "pending"  # null conclusion = still running
    return _CONCLUSION_MAP.get(raw, str(raw).lower())


def _row(c: dict, store, repo: str, now: datetime) -> CheckRow:
    started = _parse_ts(c.get("started_at"))
    completed = _parse_ts(c.get("completed_at"))
    running = completed is None
    duration: int | None
    if started and completed:
        duration = int((completed - started).total_seconds())
    elif started:  # running: elapsed so far
        duration = max(0, int((now - started).total_seconds()))
    else:
        duration = None  # legacy StatusContext / no timing

    overdue = False
    if running and started is not None and duration is not None:
        exp = store.expected(repo, c.get("name", ""), _EVENT)
        if exp is not None and exp.p50 and duration > exp.p50:
            overdue = True

    return CheckRow(
        name=c.get("name", ""), workflow=c.get("workflowName"),
        status=c.get("status", ""), conclusion=_normalise_conclusion(c.get("conclusion")),
        duration_seconds=duration, required=bool(c.get("isRequired", False)), overdue=overdue,
    )


def _group(kind: str, raw_checks: list[dict], store, repo: str, now: datetime,
           eta_seconds: int | None = None) -> CheckGroup:
    rows = tuple(_row(c, store, repo, now) for c in raw_checks)
    total = sum(r.duration_seconds for r in rows if r.duration_seconds is not None)
    cost = round(math.ceil(total / 60) * RUNNER_RATE_USD_PER_MIN, 4) if total > 0 else None
    return CheckGroup(kind=kind, checks=rows, total_seconds=total, cost_usd=cost,
                      eta_seconds=eta_seconds)


def build_check_groups(
    head_checks: list[dict], merge: dict, store, repo: str, now: datetime,
) -> tuple[CheckGroup, ...]:
    groups = [_group("head", head_checks, store, repo, now)]
    merge_checks = (merge or {}).get("checks") or []
    if merge_checks:
        groups.append(_group("merge", merge_checks, store, repo, now,
                             eta_seconds=(merge or {}).get("eta_seconds")))
    return tuple(groups)

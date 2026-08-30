"""Pure stability statistics over detached CI observations (no I/O)."""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable

from .types import CheckAgg, CheckStats, JobObs, TestObs

# Outcome vocabulary. PUBLIC because the flakiness aggregate also runs in SQL
# (``cicd_doctor.flake_counts_stmt``): a caller that re-listed these strings in a
# WHERE clause would silently drift from the in-memory fold the day a new outcome
# is added. One definition, both call sites (constitution VI).
FAIL_OUTCOMES: frozenset[str] = frozenset({"failed", "error", "failure", "timed_out"})
PASS_OUTCOMES: frozenset[str] = frozenset({"passed", "success"})
# Outcomes that carry flakiness evidence at all; everything else (notably
# "skipped") is not evidence in either direction and must be filtered out
# identically wherever the aggregate is computed.
FLAKE_OUTCOMES: frozenset[str] = PASS_OUTCOMES | FAIL_OUTCOMES

_FAIL = FAIL_OUTCOMES
_PASS = PASS_OUTCOMES
_DAY = 86400.0


def flake_pcts_from_sha_counts(
    rows: Iterable[tuple[str, int, int]],
) -> dict[str, tuple[float, int]]:
    """Per test_id: (flake %, distinct shas) from PRE-AGGREGATED per-test counts.

    *rows* is ``(test_id, distinct_shas, shas_with_both_a_pass_and_a_fail)`` —
    exactly what ``cicd_doctor.flake_counts_stmt`` returns from the database.

    This is the single home of the percentage itself, shared with the in-memory
    ``test_flake_pcts`` below, so the database path and the Python path cannot
    disagree about the arithmetic. What the database necessarily re-expresses is
    only the *predicate* ("this sha shows both a pass and a fail"), which
    ``test_aggregate_form_matches_the_in_memory_form`` pins against this function.
    """
    return {t: (_pct(flaky, shas), shas) for t, shas, flaky in rows}


def _pct(flaky: int, total: int) -> float:
    return 100.0 * flaky / total if total else 0.0


def _flake_pct(sha_outcomes: dict[str, set[str]]) -> float:
    """% of shas whose outcome set contains BOTH a pass and a fail."""
    flaky = sum(1 for o in sha_outcomes.values() if (o & _PASS) and (o & _FAIL))
    return _pct(flaky, len(sha_outcomes))


def test_flake_pcts(obs: list[TestObs]) -> dict[str, tuple[float, int]]:
    """Per test_id: (flake %, distinct shas observed). Skipped runs are ignored.

    In-memory fold, for callers that already hold the observations. Anything
    reading a large window from the database must use ``flake_counts_stmt`` +
    ``flake_pcts_from_sha_counts`` instead: this function's input is one row per
    test *attempt*, which on a busy repo is millions of rows (#TBD).
    """
    by_test: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for o in obs:
        if o.outcome in FLAKE_OUTCOMES:
            by_test[o.test_id][o.head_sha].add(o.outcome)
    return {t: (_flake_pct(shas), len(shas)) for t, shas in by_test.items()}


test_flake_pcts.__test__ = False  # type: ignore[attr-defined]


def check_stats_from_agg(rows: Iterable[CheckAgg]) -> dict[str, CheckStats]:
    """Per check: CheckStats from PRE-AGGREGATED per-check rows.

    *rows* is exactly what ``cicd_doctor.check_stats_stmt`` returns from the
    database — one row per check, the job window already collapsed inside SQL. This
    is the single home of the derived ratios (flake %, window span, rising rule),
    shared with the in-memory ``check_stats`` below through ``_pct`` and the
    second>first>0 rule, so the database path and the Python path cannot disagree
    about the arithmetic. It is the ``flake_pcts_from_sha_counts`` /
    ``test_flake_pcts`` split (#791) applied to the CiJob side (#827): what the
    database re-expresses is only the *shape* of the fold (grouping, the median
    split, the p95 pick), which ``test_check_stats_stmt_matches_the_in_memory_form``
    pins against ``check_stats``.
    """
    out: dict[str, CheckStats] = {}
    for a in rows:
        second_pct = _pct(a.second_flaky, a.second_total)
        out[a.check] = CheckStats(
            check=a.check,
            runs_total=a.runs_total,
            window_days=(a.max_ts - a.min_ts) / _DAY if a.runs_total > 1 else 0.0,
            flake_pct=_pct(a.flaky_shas, a.shas_total),
            recent_flake_pct=_pct(a.recent_flaky, a.recent_total),
            default_branch_failures=a.default_branch_failures,
            p95_duration_s=a.p95_duration_s,
            flake_rising=second_pct > _pct(a.first_flaky, a.first_total) and second_pct > 0.0,
        )
    return out


def check_stats(jobs: list[JobObs], *, default_branch: str, now_ts: float,
                demote_window_days: float) -> dict[str, CheckStats]:
    """In-memory fold over detached job observations.

    No longer on the hot path: ``cicd_doctor._analyze`` reads the same statistics
    from ``check_stats_stmt`` (one row per check, aggregated in SQL) so a 90-day
    job window is never materialized into Python on the event loop (#827). This
    stays as the anti-drift ORACLE — the SQL aggregate is proven equivalent to it
    row-for-row in ``test_cicd_check_stats_aggregate`` — exactly as
    ``test_flake_pcts`` was kept when ``flake_counts_stmt`` took over (#791).
    """
    by_check: dict[str, list[JobObs]] = defaultdict(list)
    for j in jobs:
        if j.conclusion in ("cancelled", "skipped"):
            continue  # not evidence about stability either way
        by_check[j.check].append(j)
    out: dict[str, CheckStats] = {}
    for check, js in by_check.items():
        shas: dict[str, set[str]] = defaultdict(set)
        recent: dict[str, set[str]] = defaultdict(set)
        master_shas: dict[str, set[str]] = defaultdict(set)
        for j in js:
            shas[j.head_sha].add(j.conclusion)
            if j.created_ts > now_ts - demote_window_days * _DAY:
                recent[j.head_sha].add(j.conclusion)
            if j.head_branch == default_branch:
                master_shas[j.head_sha].add(j.conclusion)
        ts = sorted(j.created_ts for j in js)
        durations = sorted(j.duration_s for j in js)
        mid = ts[len(ts) // 2]
        first = {s: o for s, o in shas.items()
                 if min(j.created_ts for j in js if j.head_sha == s) < mid}
        second = {s: o for s, o in shas.items() if s not in first}
        second_pct = _flake_pct(second)
        out[check] = CheckStats(
            check=check,
            runs_total=len(js),
            window_days=(ts[-1] - ts[0]) / _DAY if len(ts) > 1 else 0.0,
            flake_pct=_flake_pct(shas),
            recent_flake_pct=_flake_pct(recent),
            default_branch_failures=sum(
                1 for o in master_shas.values() if (o & _FAIL) and not (o & _PASS)),
            # ceil, not floor: with few samples the slowest run must count
            # (p95 of [60,60,60,120] is 120, not 60). Deliberately NOT
            # server/pipeline/history.percentile — that is a verbatim Plotroom
            # mirror with floor-style indexing; do not "unify" them.
            p95_duration_s=durations[min(len(durations) - 1,
                                         math.ceil(0.95 * (len(durations) - 1)))]
            if durations else 0.0,
            flake_rising=second_pct > _flake_pct(first) and second_pct > 0.0,
        )
    return out

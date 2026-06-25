"""Open-stage classifier: maps ccswitch's PR fields to the Plotroom stage vocabulary.

This is a first-cut approximation using the rollup-based checks field (not per-check
required-prefix machinery). It covers open-stage PRs only (merged/queue/deploy are
handled in later slices).

Plotroom reference: server/estimator/classify.ts (open-stage branch):
  isDraft → parked/draft
  required check COMPLETED with real failure → parked/ci-failed
  required check not COMPLETED → ci
  mergeStateStatus == DIRTY → parked/conflicting
  else → ready

1a approximation (rollup-based, no per-check required-prefix):
  is_draft → parked
  checks == failing → parked
  checks == pending → ci
  else (passing/none): merge_state_status == DIRTY → parked; else → ready

Plotroom reference: server/estimator/classify.ts (merged branch):
  pr.mergedAt → ageDays = (now - mergedAt) / 86400000
  ageDays > retentionDays → null (off the board)
  !deploy.hasDeploy → 'merged'
  (deploy stages qa-deploy/awaiting-prod/prod are out of scope, slice 1d)
"""
from __future__ import annotations

from datetime import datetime, timezone


_OPEN_STAGES = frozenset({"ci", "ready", "parked"})


def derive_open_stage(
    checks: str,
    merge_state_status: str | None,
    is_draft: bool,
) -> str:
    """Return the open-stage string for a non-merged, non-queue PR.

    Args:
        checks: ccswitch rollup — one of 'passing' | 'failing' | 'pending' | 'none'
        merge_state_status: GitHub mergeStateStatus string or None
        is_draft: True when the PR is in draft state

    Returns:
        'parked' | 'ci' | 'ready'
    """
    if is_draft:
        return "parked"
    if checks == "failing":
        return "parked"
    if checks == "pending":
        return "ci"
    # checks == 'passing' or 'none'
    if merge_state_status == "DIRTY":
        return "parked"
    return "ready"


def derive_merged_stage(
    merged_at: str | None,
    now: datetime,
    retention_days: int = 7,
    has_deploy: bool = False,
) -> str | None:
    """Return the stage for a merged PR, or None if off-board / not merged.

    Args:
        merged_at: ISO-8601 timestamp string when the PR was merged, or None.
        now: current UTC datetime (timezone-aware).
        retention_days: how many days after merge a PR stays on the board.
        has_deploy: True when the repo has deploy environments configured.
            For repos without deploy envs (the common case), merged PRs remain
            at 'merged'. Deploy stages (qa-deploy/awaiting-prod/prod) are not
            implemented here — see slice 1d.

    Returns:
        'merged' | None (off-board or not merged)
    """
    if merged_at is None:
        return None
    # Parse ISO-8601; handle trailing Z (Python < 3.11 doesn't parse 'Z' directly)
    ts = merged_at.replace("Z", "+00:00")
    try:
        merged_dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    # Ensure both sides are timezone-aware for subtraction
    if merged_dt.tzinfo is None:
        merged_dt = merged_dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_days = (now - merged_dt).total_seconds() / 86400.0
    if age_days > retention_days:
        return None  # off the board
    if not has_deploy:
        return "merged"
    # TODO slice 1d: deploy stages (qa-deploy / awaiting-prod / prod)
    return None


def derive_queue_stage(
    *,
    queue_state: str | None = None,
    merge_state_status: str | None = None,
    group_failed: bool = False,
    unmergeable: bool = False,
) -> tuple[str, str | None]:
    """Return ``(stage, substate)`` for a PR sitting in the merge queue.

    Faithful port of classify.ts's ``pr.queue`` branch:
      - UNMERGEABLE (the queue-entry's own ``unmergeable`` signal OR entry
        ``state == 'UNMERGEABLE'``) → facing ejection. Split on the PR's OWN
        mergeStateStatus: ``DIRTY`` (genuine base conflict) → ``unmergeable``;
        anything else (a cascade victim poisoned by a conflicting entry ahead) →
        ``queue-blocked``.
      - the merge-group build failed → ``group-failed``.
      - otherwise progressing normally → no substate.

    NOTE: not live-parity-gated on the current oracle repo — it has no GitHub merge
    queue, so no ``queue``-stage PRs exist to compare against. This is a unit-tested
    faithful port; parity is deferred to a merge-queue-configured repo (slice 1b caveat).
    """
    if unmergeable or queue_state == "UNMERGEABLE":
        return "queue", ("unmergeable" if merge_state_status == "DIRTY" else "queue-blocked")
    if group_failed:
        return "queue", "group-failed"
    return "queue", None

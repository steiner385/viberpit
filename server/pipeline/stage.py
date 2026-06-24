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
"""
from __future__ import annotations


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

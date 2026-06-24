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

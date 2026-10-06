"""Policy-skipped runner holds — the pure half (cairnea/KinDash#16652).

The Backlog Runner handed ``forced-deploy-audit`` issues to their owner ("bounced
at opus; fable needs approval") BEFORE the product listed that label in
``skip_labels``. The hold outlived the policy change: the Held inbox kept offering
"Approve escalation" for issues the queue would skip as ``bot`` the moment the hold
ended, and approving dispatched them anyway. ``stale_runner_holds`` names those
holds so the backlog tick can end them; ``skipped_by_policy`` is the predicate the
approval tap refuses on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.pm.policy_skip import skipped_by_policy, stale_runner_holds
from server.pm.queue import QueueRow

NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)
ESCALATION = "bounced at opus; fable needs approval"
AUDIT = ("ci-infrastructure", "forced-deploy-audit")


def _row(**over) -> QueueRow:
    base: dict[str, Any] = dict(
        ref="o/r#1", repo="o/r", number=1, product="kindash", title="t", body="", url="u",
        status="Held", score=None, gh_created_at=NOW, gh_updated_at=NOW, assignees=(),
        author="github-actions", labels=(), open_children=0, open_blockers=(),
        live_session=False, claimed=False)
    base.update(over)
    if "ref" not in over:
        base["ref"] = f"{base['repo']}#{base['number']}"
    return QueueRow(**base)


def test_skipped_by_policy_reads_the_products_skip_labels_and_the_global_filter():
    policy = {"skip_labels": [" Forced-Deploy-Audit "]}        # normalised like the queue
    assert skipped_by_policy(author="github-actions", labels=AUDIT,
                             title="⚠️ Unverified prod deploy b80aa1b03c — 2026-09-21",
                             policy=policy)
    assert not skipped_by_policy(author="github-actions", labels=("ci-infrastructure",),
                                 title="t", policy=policy)
    # The built-in bot-noise filter (``queue.SKIP_ISSUE_LABELS`` & co.) counts with
    # no policy at all — this is ``skip_reason``'s own ``bot`` verdict, not a copy.
    assert skipped_by_policy(author="renovate[bot]", labels=(), title="t", policy={})
    assert skipped_by_policy(author="tony", labels=("dependencies",), title="t", policy=None)
    assert not skipped_by_policy(author="tony", labels=("bug",), title="t", policy=None)


def test_stale_runner_holds_picks_only_the_runners_owner_action_holds_on_skipped_rows():
    policies = {"kindash": {"skip_labels": ["forced-deploy-audit"]},
                "other": {"skip_labels": []}}
    rows = [
        # The runner's approval-gated hand-off on a now-skipped issue: stale.
        _row(number=1, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=AUDIT),
        # Any hand-off the runner wrote, not only escalations (a close-out hold too).
        _row(number=2, hold_kind="owner_action", hold_by="runner", labels=AUDIT,
             hold_reason="session found no PR is needed (not-needed): CI-verdict latch"),
        # A PERSON's park is theirs, whatever the label says.
        _row(number=3, hold_kind="owner_action", hold_by="tony@example.com",
             hold_reason="keep", labels=AUDIT),
        # Not the runner's kind: a session's evidence hold is left alone.
        _row(number=4, hold_kind="evidence", hold_by="session:s1", hold_reason="r",
             labels=AUDIT),
        # Still real work: the label is not skipped.
        _row(number=5, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=("bug",)),
        # Another product that does not skip the label.
        _row(number=6, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=AUDIT, product="other"),
        # Unheld: nothing to release.
        _row(number=7, labels=AUDIT),
        # No policy for the product: only the global filter applies, and it does not fire.
        _row(number=8, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=AUDIT, product="unknown"),
        # No policy, but the global filter fires: stale as well.
        _row(number=9, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=("dependencies",), product="unknown"),
    ]
    assert [r.number for r in stale_runner_holds(rows, policies)] == [1, 2, 9]
    assert stale_runner_holds([], policies) == []

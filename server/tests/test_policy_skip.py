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
        author="app/github-actions", labels=(), open_children=0, open_blockers=(),
        live_session=False, claimed=False)
    base.update(over)
    if "ref" not in over:
        base["ref"] = f"{base['repo']}#{base['number']}"
    return QueueRow(**base)


def test_skipped_by_policy_reads_the_products_skip_labels_and_the_global_filter():
    policy = {"skip_labels": [" Forced-Deploy-Audit "]}        # normalised like the queue
    assert skipped_by_policy(author="app/github-actions", labels=AUDIT,
                             title="⚠️ Unverified prod deploy b80aa1b03c — 2026-09-21",
                             policy=policy)
    assert not skipped_by_policy(author="app/github-actions", labels=("ci-infrastructure",),
                                 title="t", policy=policy)
    # The built-in bot-noise filter (``queue.SKIP_ISSUE_LABELS`` & co.) counts with
    # no policy at all — this is ``skip_reason``'s own ``bot`` verdict, not a copy.
    assert skipped_by_policy(author="renovate[bot]", labels=(), title="t", policy={})
    assert skipped_by_policy(author="tony", labels=("dependencies",), title="t", policy=None)
    assert not skipped_by_policy(author="tony", labels=("bug",), title="t",
                                 policy={"trusted_authors": ["tony"]})


def test_stale_runner_holds_picks_only_the_runners_owner_action_holds_on_skipped_rows():
    # ``forced-deploy-audit`` is a built-in tracker label since #2683, so "other" opts
    # back in to trackers to keep a product for which the label is still work.
    policies = {"kindash": {"skip_labels": ["forced-deploy-audit"]},
                "other": {"skip_labels": [], "skip_self_closing_trackers": False}}
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
        # Another product that does not skip the label (and works its trackers).
        _row(number=6, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=AUDIT, product="other"),
        # Unheld: nothing to release.
        _row(number=7, labels=AUDIT),
        # No policy for the product: the built-in filters apply, and the tracker one
        # fires on ``forced-deploy-audit`` (#2683).
        _row(number=8, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=AUDIT, product="unknown"),
        # No policy, but the global filter fires: stale as well.
        _row(number=9, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
             labels=("dependencies",), product="unknown"),
    ]
    assert [r.number for r in stale_runner_holds(rows, policies)] == [1, 2, 8, 9]
    assert stale_runner_holds([], policies) == []


def test_approval_tap_refuses_an_untrusted_author():
    policy = {"trusted_authors": ["steiner385"]}
    assert skipped_by_policy(author="stranger", labels=(), title="t", policy=policy)
    assert not skipped_by_policy(author="steiner385", labels=(), title="t", policy=policy)


def test_trust_never_makes_a_runner_hold_stale():
    """Trust gates DISPATCH (and the approve tap), not the hold inbox: before a product's
    trusted_authors is seeded, every runner hold on a human-authored issue is
    'untrusted' and releasing them would silently drop the owner's queue."""
    policies = {"kindash": {"trusted_authors": ["steiner385"]}}
    row = _row(number=1, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
               author="stranger", labels=("bug",))
    assert skipped_by_policy(author="stranger", labels=("bug",), title="t",
                             policy=policies["kindash"])
    assert stale_runner_holds([row], policies) == []
    assert stale_runner_holds([row], {}) == []


def test_a_self_closing_tracker_is_not_work_unless_the_product_works_trackers():
    """#2683: the not-work rule is the queue's ``bot`` AND ``tracker`` verdicts, so the
    approve tap, the hold-hygiene pass and the runner's not-needed settle judge a
    tracker exactly as the queue does — including a product's opt-out."""
    from server.pm.policy_skip import not_work_by_policy
    flaky = dict(author="app/github-actions", labels=("flaky",),
                 title="[flaky] tasks/kanban-board.spec.ts › Escape (fp:102b4b8a)")
    assert not_work_by_policy(policy=None, **flaky)
    assert not_work_by_policy(policy={}, **flaky)
    assert not not_work_by_policy(policy={"skip_self_closing_trackers": False}, **flaky)
    assert skipped_by_policy(policy={}, **flaky)
    # The ignore label is built in: no policy lists it, and the opt-out is irrelevant.
    ignored = dict(author="app/github-actions", labels=("viberpit-ignore",), title="t")
    assert not_work_by_policy(policy={"skip_self_closing_trackers": False}, **ignored)
    # Ordinary work stays work.
    assert not not_work_by_policy(author="app/github-actions", labels=("bug",), title="t",
                                  policy={})
    # A person's ``parked`` label is never work, whatever the policy (viberpit#1432).
    assert not_work_by_policy(author="tony", labels=("bug", "parked"), title="t",
                              policy={"skip_self_closing_trackers": False})


def test_runner_holds_on_self_closing_trackers_go_stale():
    """The 'session found no PR is needed' holds the runner wrote on trackers before
    #2683 are released by the hold-hygiene pass — unless the product works trackers."""
    reason = "session found no PR is needed (not-needed): tracker auto-closes"
    rows = [
        _row(number=1, hold_kind="owner_action", hold_by="runner", hold_reason=reason,
             title="[flaky] personas/plus-parent.spec.ts › S4 (fp:be691056)",
             labels=("flaky",)),
        _row(number=2, hold_kind="owner_action", hold_by="runner", hold_reason=reason,
             title="Midday e2e failed — 2026-10-06",
             labels=("bug", "ci-infrastructure", "midday-e2e-failure")),
        _row(number=3, hold_kind="owner_action", hold_by="runner", hold_reason=reason,
             title="🧮 UI Maintenance Cost & Architecture ROI Digest",
             labels=("technical-debt", "viberpit-ignore")),
        # Real work under a not-needed hold stays with its owner.
        _row(number=4, hold_kind="owner_action", hold_by="runner", hold_reason=reason,
             title="ops: actions budget / runner tier", labels=("ops",)),
    ]
    assert [r.number for r in stale_runner_holds(rows, {"kindash": {}})] == [1, 2, 3]
    # Opted back in: only the ignore label still releases.
    works = {"kindash": {"skip_self_closing_trackers": False}}
    assert [r.number for r in stale_runner_holds(rows, works)] == [3]

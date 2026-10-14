"""Policy-skipped runner holds — the pure half (cairnea/KinDash#16652).

The Backlog Runner handed ``forced-deploy-audit`` issues to their owner ("bounced
at opus; fable needs approval") BEFORE the product listed that label in
``skip_labels``. The hold outlived the policy change: the Held inbox kept offering
"Approve escalation" for issues the queue would skip as ``bot`` the moment the hold
ended, and approving dispatched them anyway. ``stale_system_holds`` names those
holds so the backlog tick can end them; ``skipped_by_policy`` is the predicate the
approval tap refuses on.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from server.pm.policy_skip import skipped_by_policy, stale_system_holds
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


def test_stale_system_holds_picks_only_system_owner_action_holds_on_skipped_rows():
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
        # Design 2026-10-14 §2: every SYSTEM actor's owner_action hand-off, not only
        # the runner's — the reconciler, the legacy conversion, an automation.
        _row(number=10, hold_kind="owner_action", hold_by="reconciler", hold_reason="r",
             labels=AUDIT),
        _row(number=11, hold_kind="owner_action", hold_by="migration", hold_reason="r",
             labels=AUDIT),
        _row(number=12, hold_kind="owner_action", hold_by="automation:error-triage",
             hold_reason="r", labels=AUDIT),
        # An agent is not the system: a session's or the assistant's hold stays.
        _row(number=13, hold_kind="owner_action", hold_by="assistant:tony@example.com",
             hold_reason="r", labels=AUDIT),
        # A hold with no recorded placer is nobody's to end.
        _row(number=14, hold_kind="owner_action", hold_by=None, hold_reason="r",
             labels=AUDIT),
        # Never a decision hold, whoever placed it.
        _row(number=15, hold_kind="decision", hold_by="runner", hold_reason="r",
             labels=AUDIT),
    ]
    assert [r.number for r in stale_system_holds(rows, policies, now=NOW)] == [
        1, 2, 8, 9, 10, 11, 12]
    assert stale_system_holds([], policies, now=NOW) == []


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
    assert stale_system_holds([row], policies, now=NOW) == []
    assert stale_system_holds([row], {}, now=NOW) == []


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
    # A person's ``parked`` label is a park, not a not-work verdict: the approve tap
    # refuses it (viberpit#1432), the not-work rule — which ends runner holds — does
    # not name it (#2683 review).
    parked = dict(author="tony", labels=("bug", "parked"), title="t")
    assert not not_work_by_policy(policy={"trusted_authors": ["tony"]}, **parked)
    assert skipped_by_policy(policy={"trusted_authors": ["tony"]}, **parked)


def test_a_parked_label_never_ends_the_runners_give_up_hold():
    rows = [_row(number=1, hold_kind="owner_action", hold_by="runner", hold_reason=ESCALATION,
                 author="tony", labels=("bug", "parked"))]
    assert stale_system_holds(rows, {"kindash": {}}, now=NOW) == []


def _not_needed_hold(number, *, until):
    return _row(number=number, hold_kind="owner_action", hold_by="runner", author="tony",
                hold_reason="session found no PR is needed (not-needed): ops tracking issue",
                labels=("ops",), not_needed_standing=True, not_needed_until=until)


def test_not_needed_holds_clear_inside_the_window_and_drip_once_it_has_ended():
    """#2683 review: a hold whose suppression window is still open is released — the
    issue stays skipped as ``not_needed`` until the window ends. One whose window has
    already ended would be dispatchable at once, so a pass releases at most
    ``EXPIRED_NOT_NEEDED_RELEASES_PER_PASS`` of those: a deploy must not burst-dispatch
    every legacy hold."""
    from datetime import timedelta

    from server.pm.policy_skip import EXPIRED_NOT_NEEDED_RELEASES_PER_PASS
    assert EXPIRED_NOT_NEEDED_RELEASES_PER_PASS == 1
    rows = [
        _not_needed_hold(1, until=NOW - timedelta(days=9)),
        _not_needed_hold(2, until=NOW + timedelta(hours=5)),
        _not_needed_hold(3, until=NOW - timedelta(days=2)),
        _not_needed_hold(4, until=NOW + timedelta(days=3)),
    ]
    assert [r.number for r in stale_system_holds(rows, {"kindash": {}}, now=NOW)] == [1, 2, 4]
    # The next pass (row 1 now released) takes the next expired one.
    assert [r.number for r in stale_system_holds(rows[1:], {"kindash": {}}, now=NOW)] == [
        2, 3, 4]


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
    assert [r.number for r in stale_system_holds(rows, {"kindash": {}}, now=NOW)] == [1, 2, 3]
    # Opted back in: only the ignore label still releases.
    works = {"kindash": {"skip_self_closing_trackers": False}}
    assert [r.number for r in stale_system_holds(rows, works, now=NOW)] == [3]


def test_approval_tap_refuses_an_issue_the_fleet_cannot_run():
    """#2983: the approve tap dispatches without re-ranking, so it must refuse an
    environment-gated issue exactly as the queue skips it."""
    for labels in (("env: windows",), ("real-target", "env: windows")):
        assert skipped_by_policy(author="app/github-actions", labels=labels, title="t",
                                 policy=None)
    assert not skipped_by_policy(author="app/github-actions", labels=("env: linux",),
                                 title="t", policy=None)

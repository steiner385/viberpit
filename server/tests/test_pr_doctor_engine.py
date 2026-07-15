"""Pure PR Doctor engine: classify + plan_recovery + state machine."""
from __future__ import annotations

from server.github.pr_status import PullRequestInfo
from server.automations.pr_doctor_engine import classify


def _pr(**kw) -> PullRequestInfo:
    base = dict(number=1, url="u", state="open", checks="passing", title="t",
                mergeable="MERGEABLE", merge_state_status="CLEAN")
    base.update(kw)
    return PullRequestInfo(**base)  # type: ignore[arg-type]  # **dict into a typed ctor


def test_classify_terminal_and_draft_are_none():
    assert classify(_pr(state="merged")) is None
    assert classify(_pr(state="closed")) is None
    assert classify(_pr(merge_state_status="DRAFT")) is None
    assert classify(_pr(is_draft=True)) is None


def test_classify_blocked_is_review():
    assert classify(_pr(merge_state_status="BLOCKED")) == "blocked"


def test_classify_blocked_with_failing_required_check_is_failing_ci():
    # GitHub reports merge_state_status=BLOCKED for a failing *required* check
    # (branch protection), not only for a review/approval gate. A failing check
    # is fixable, so it must classify as failing_ci — otherwise PR Doctor only
    # ever notifies and never fixes branch-protected PRs (the common case).
    assert classify(_pr(merge_state_status="BLOCKED", checks="failing")) == "failing_ci"
    # BLOCKED with non-failing checks and no auto-merge is a genuine review/approval gate → notify.
    assert classify(_pr(merge_state_status="BLOCKED", checks="passing")) == "blocked"


def test_classify_stuck_enqueue():
    # Armed + BLOCKED + not in queue = GitHub refused to enqueue (stuck-enqueue):
    # a mechanical failure, not a human review gate.
    assert classify(_pr(merge_state_status="BLOCKED", checks="passing",
                        auto_merge_enabled=True, in_merge_queue=False)) == "stuck_enqueue"
    # Armed + in queue already = not stuck.
    assert classify(_pr(merge_state_status="BLOCKED", checks="passing",
                        auto_merge_enabled=True, in_merge_queue=True)) == "blocked"
    # Not armed + BLOCKED = still a review gate.
    assert classify(_pr(merge_state_status="BLOCKED", checks="passing",
                        auto_merge_enabled=False, in_merge_queue=False)) == "blocked"


def test_classify_conflict_via_mergeable_or_dirty():
    assert classify(_pr(mergeable="CONFLICTING")) == "merge_conflict"
    assert classify(_pr(merge_state_status="DIRTY")) == "merge_conflict"
    # UNKNOWN mergeable with no DIRTY → not a conflict yet
    assert classify(_pr(mergeable="UNKNOWN")) is None


def test_classify_dirty_conflict_even_when_mergeable_unknown():
    assert classify(_pr(mergeable="UNKNOWN", merge_state_status="DIRTY")) == "merge_conflict"


def test_classify_out_of_date():
    assert classify(_pr(merge_state_status="BEHIND")) == "out_of_date"


def test_classify_conflict_beats_failing_ci():
    assert classify(_pr(checks="failing", mergeable="CONFLICTING")) == "merge_conflict"


def test_classify_failing_required_ci():
    assert classify(_pr(checks="failing", merge_state_status="CLEAN")) == "failing_ci"


def test_classify_unstable_nonrequired_is_not_failing_ci():
    # UNSTABLE = mergeable, only a non-required check is red → no session fix
    assert classify(_pr(checks="failing", merge_state_status="UNSTABLE")) is None


def test_classify_healthy_is_none():
    assert classify(_pr(checks="passing", merge_state_status="CLEAN")) is None


# ---------------------------------------------------------------------------
# Task 2: RecoveryAction dataclasses + plan_recovery risk-gating
# ---------------------------------------------------------------------------
from server.automations.pr_doctor_engine import (
    plan_recovery, Notify, Propose, Mechanical, SessionFix, GiveUp,
)

_CFG = {
    "enabled_repos": [], "max_attempts": 3, "mechanical_attempts": 2,
    "auto_update_out_of_date": True, "auto_fix_failing_ci": True,
    "auto_resolve_conflicts": False,
}


def test_mode_off_is_notify():
    assert isinstance(plan_recovery("failing_ci", "off", _CFG, {}), Notify)


def test_blocked_is_always_notify():
    assert isinstance(plan_recovery("blocked", "auto", _CFG, {}), Notify)


def test_stuck_enqueue_auto_is_mechanical():
    cfg = {**_CFG, "auto_fix_stuck_enqueue": True}
    a = plan_recovery("stuck_enqueue", "auto", cfg, {})
    assert isinstance(a, Mechanical) and a.kind == "stuck_enqueue"


def test_stuck_enqueue_auto_falls_back_to_propose_when_not_opted_in():
    # Flag absent (or False) → propose but don't auto-act.
    a = plan_recovery("stuck_enqueue", "auto", _CFG, {})
    assert isinstance(a, Propose) and a.kind == "stuck_enqueue"


def test_stuck_enqueue_propose_mode_proposes():
    a = plan_recovery("stuck_enqueue", "propose", _CFG, {})
    assert isinstance(a, Propose) and a.kind == "stuck_enqueue"


def test_propose_mode_proposes_every_mode():
    for fm in ("merge_conflict", "out_of_date", "failing_ci"):
        a = plan_recovery(fm, "propose", _CFG, {})
        assert isinstance(a, Propose) and a.kind == fm


def test_auto_mechanical_out_of_date():
    a = plan_recovery("out_of_date", "auto", _CFG, {})
    assert isinstance(a, Mechanical) and a.kind == "out_of_date"


def test_auto_session_failing_ci():
    a = plan_recovery("failing_ci", "auto", _CFG, {})
    assert isinstance(a, SessionFix) and a.kind == "failing_ci"


def test_auto_conflict_falls_back_to_propose_when_not_opted_in():
    a = plan_recovery("merge_conflict", "auto", _CFG, {})
    assert isinstance(a, Propose)  # auto_resolve_conflicts=False → propose


def test_auto_conflict_session_when_opted_in():
    cfg = {**_CFG, "auto_resolve_conflicts": True}
    a = plan_recovery("merge_conflict", "auto", cfg, {})
    assert isinstance(a, SessionFix) and a.kind == "merge_conflict"


def test_gave_up_state_is_notify():
    a = plan_recovery("failing_ci", "auto", _CFG, {"gave_up": True})
    assert isinstance(a, Notify)


def test_session_attempts_exhausted_gives_up():
    # Give-up keys on the MONOTONIC total, not the re-armable `attempts`.
    a = plan_recovery("failing_ci", "auto", _CFG, {"total_attempts": 3})
    assert isinstance(a, GiveUp)


def test_mechanical_attempts_exhausted_gives_up():
    a = plan_recovery("out_of_date", "auto", _CFG, {"total_mechanical": 2})
    assert isinstance(a, GiveUp)


def test_rearmable_attempts_do_not_drive_give_up():
    # A high re-armable counter with a zero monotonic total must NOT give up:
    # only the total ceiling drives give-up (the async-inject regression guard).
    a = plan_recovery("failing_ci", "auto", _CFG, {"attempts": 99, "total_attempts": 0})
    assert isinstance(a, SessionFix)
    m = plan_recovery("out_of_date", "auto", _CFG, {"mechanical_attempts": 99, "total_mechanical": 0})
    assert isinstance(m, Mechanical)


# ---------------------------------------------------------------------------
# Task 3: Per-target state machine — re-arm, attempt accounting, terminate
# ---------------------------------------------------------------------------
from server.automations.pr_doctor_engine import (
    apply_rearm, record_attempt, target_terminal,
)


def test_rearm_on_external_push_resets():
    state = {"attempts": 2, "gave_up": True, "last_head_sha": "old", "last_action_sha": "act"}
    out = apply_rearm(state, _pr(head_sha="brand-new", checks="failing", merge_state_status="CLEAN"))
    assert out["attempts"] == 0 and out["gave_up"] is False
    assert out["last_head_sha"] == "brand-new"


def test_no_rearm_when_head_is_our_own_action_sha():
    # PR Doctor's own fix pushed "act" → must NOT reset (else gave_up never reached)
    state = {"attempts": 2, "last_head_sha": "old", "last_action_sha": "act", "last_failure": "failing_ci"}
    out = apply_rearm(state, _pr(head_sha="act", checks="failing", merge_state_status="CLEAN"))
    assert out["attempts"] == 2 and not out.get("gave_up")


def test_rearm_on_changed_failure_class():
    state = {"attempts": 2, "last_head_sha": "same", "last_action_sha": "", "last_failure": "failing_ci"}
    out = apply_rearm(state, _pr(head_sha="same", merge_state_status="BEHIND"))
    assert out["attempts"] == 0  # failing_ci → out_of_date is a new problem


def test_record_attempt_increments_right_counter_and_records_sha():
    from server.automations.pr_doctor_engine import SessionFix, Mechanical
    s = record_attempt({}, SessionFix("failing_ci"), "sha1")
    assert s["attempts"] == 1 and s["last_action_sha"] == "sha1"
    m = record_attempt({}, Mechanical("out_of_date"), "sha2")
    assert m["mechanical_attempts"] == 1 and m["last_action_sha"] == "sha2"


def test_record_attempt_increments_both_rearmable_and_monotonic():
    from server.automations.pr_doctor_engine import SessionFix, Mechanical
    s = record_attempt({}, SessionFix("failing_ci"), "sha1")
    assert s["attempts"] == 1 and s["total_attempts"] == 1
    m = record_attempt({}, Mechanical("out_of_date"), "sha2")
    assert m["mechanical_attempts"] == 1 and m["total_mechanical"] == 1


def test_rearm_resets_rearmable_but_preserves_monotonic_total():
    # apply_rearm on an external push resets the re-armable counters but the
    # monotonic totals are the re-arm-IMMUNE ceiling and must survive.
    state = {"attempts": 2, "total_attempts": 2, "mechanical_attempts": 1,
             "total_mechanical": 1, "last_head_sha": "old", "last_action_sha": "act"}
    out = apply_rearm(state, _pr(head_sha="brand-new", checks="failing", merge_state_status="CLEAN"))
    assert out["attempts"] == 0 and out["mechanical_attempts"] == 0
    assert out["total_attempts"] == 2 and out["total_mechanical"] == 1


def test_async_inject_loop_reaches_give_up_despite_rearm():
    """Regression: the async-inject gap. Each fix pushes a NEW (still-failing)
    head, so every poll's apply_rearm sees an "external" push and re-arms
    ``attempts`` to 0 — yet give-up MUST still fire once the monotonic
    ``total_attempts`` reaches max_attempts."""
    from server.automations.pr_doctor_engine import SessionFix
    cfg = _CFG  # max_attempts=3, auto_fix_failing_ci=True
    state: dict = {}
    injects = 0
    for i in range(10):
        info = _pr(head_sha=f"sha{i}", checks="failing", merge_state_status="CLEAN")
        state = apply_rearm(state, info)
        action = plan_recovery("failing_ci", "auto", cfg, state)
        if isinstance(action, GiveUp):
            break
        assert isinstance(action, SessionFix)
        # The re-armable counter keeps getting re-armed to 0 each external-looking push.
        assert state.get("attempts", 0) == 0
        state = record_attempt(state, action, info.head_sha)
        injects += 1
    assert isinstance(action, GiveUp)
    assert injects == cfg["max_attempts"]  # exactly max_attempts injections, then give up
    assert state["total_attempts"] == cfg["max_attempts"]


def test_target_terminal():
    assert target_terminal(_pr(state="merged")) is True
    assert target_terminal(_pr(state="closed")) is True
    assert target_terminal(_pr(state="open")) is False


def test_plan_recovery_total_on_empty_config():
    from server.automations.pr_doctor_engine import plan_recovery, SessionFix
    # auto + empty config: flags default falsy → Propose fallback (no KeyError)
    from server.automations.pr_doctor_engine import Propose
    assert isinstance(plan_recovery("failing_ci", "auto", {}, {}), Propose)
    # with the flag on but caps missing → uses defaults, still no KeyError
    assert isinstance(plan_recovery("failing_ci", "auto", {"auto_fix_failing_ci": True}, {}), SessionFix)

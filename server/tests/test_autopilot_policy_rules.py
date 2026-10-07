import pytest
from server.automations.autopilot_policy import (
    DecisionContext, Decision, pre_decide, post_validate, fingerprint,
)

CONFIG = {
    "allow_tool_approvals": True,
    "max_actions_per_session_per_hour": 12,
    "recovery_max_attempts": 2,
    "loop_repeat_limit": 3,
    "human_defer_seconds": 120,
    "extra_deny_patterns": [],
}


def _ctx(**kw):
    base = dict(session_id="s1", status="waiting_input", account="a", repo=None,
                branch=None, effective_goal="ship it", goal_source="explicit",
                stuck={"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
                transcript_tail=[], ledger={})
    base.update(kw)
    return DecisionContext(**base)


def test_denylisted_permission_is_forced_deny():
    ctx = _ctx(stuck={"kind": "permission", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "deny"


def test_safe_permission_defers_to_llm():
    assert pre_decide(_ctx(), CONFIG, now=1000.0) is None


def test_hourly_budget_exhausted_skips():
    ledger = {"hour_actions": [999.0] * 12}
    d = pre_decide(_ctx(ledger=ledger), CONFIG, now=1000.0)
    assert d is not None and d.action == "skip"


def test_loop_detection_escalates_to_needs_human():
    ctx = _ctx()
    fp = fingerprint(ctx)
    ctx.ledger = {"last_fingerprint": fp, "repeat_count": 3, "hour_actions": []}
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate" and "loop" in d.reason


def test_recovery_cap_escalates():
    ctx = _ctx(status="needs_attention",
               stuck={"kind": "needs_attention"},
               ledger={"recovery_attempts": 2, "hour_actions": []})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate"


def test_post_validate_downgrades_illegal_approve():
    ctx = _ctx(stuck={"kind": "permission", "tool_name": "Bash",
                      "tool_input": {"command": "git push --force"}})
    out = post_validate(Decision(action="approve", reason="llm said so"), ctx, CONFIG)
    assert out.action == "escalate"


def test_post_validate_blocks_approve_when_switch_off():
    cfg = {**CONFIG, "allow_tool_approvals": False}
    out = post_validate(Decision(action="approve"), _ctx(), cfg)
    assert out.action == "escalate"


def test_post_validate_rejects_out_of_range_answer():
    ctx = _ctx(stuck={"kind": "question", "question": "q", "options": ["a", "b"]})
    out = post_validate(Decision(action="answer", answer_index=5), ctx, CONFIG)
    assert out.action == "escalate"


def _ctx_with_facts(pr_facts):
    """Build a DecisionContext wired up with the given PrFacts for gate-downgrade tests."""
    return _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=pr_facts)


def open_unarmed_pr():
    """PrFacts with a single open, unarmed PR — the canonical gate-trigger fixture."""
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, False, "u"),))


def test_post_validate_gate_nudge_carries_no_canned_text():
    # A gate downgrade must NEVER fabricate nudge text — it marks the decision
    # for composition and carries the bar verdict as structured context.
    ctx = _ctx_with_facts(open_unarmed_pr())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge"
    assert d.prompt is None
    assert d.needs_composition is True
    assert d.downgraded_from == "complete"
    assert d.verdict_reason and "not armed" in d.verdict_reason
    assert d.verdict_remedy and "gh pr ready" in d.verdict_remedy


def test_post_validate_passes_gate_nudge_through_unmarked():
    # A decider-authored nudge is not a downgrade: no composition marker forced here
    # (the loop composes all nudges anyway); its prompt survives as the intent hint.
    d = post_validate(Decision(action="nudge", prompt="fix CI"), _ctx(status="done"), CONFIG)
    assert d.action == "nudge" and d.prompt == "fix CI" and d.downgraded_from is None


def test_post_validate_empty_nudge_no_longer_escalates_here():
    # The empty-nudge rule moved to the loop (composer failure -> escalate there).
    d = post_validate(Decision(action="nudge", prompt="  "), _ctx(status="done"), CONFIG)
    assert d.action == "nudge"


from server.automations.completion_bar import PrFact, PrFacts


def _armed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, True, "u"),))


def _unarmed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, False, "u"),))


def test_complete_with_unarmed_pr_is_downgraded_to_nudge():
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_unarmed_facts())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge" and d.prompt is None and d.needs_composition and d.verdict_remedy


def test_complete_with_armed_pr_survives():
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_armed_facts())
    d = post_validate(Decision(action="complete", reason="armed"), ctx, CONFIG)
    assert d.action == "complete"


def test_complete_with_no_pr_facts_is_left_to_decider():
    ctx = _ctx(status="complete", stuck={"kind": "complete"})  # pr_facts defaults None
    d = post_validate(Decision(action="complete", reason="x"), ctx, CONFIG)
    assert d.action == "complete"


# --- Part 1: code goal + no PR is also gated ---

def _no_pr_code_facts():
    """Repo-attached session with zero PRs — code goal, nothing opened."""
    return PrFacts(has_repo=True, prs=())


def _no_pr_no_repo_facts():
    """Non-code / research session: no repo, no PRs."""
    return PrFacts(has_repo=False, prs=())


def test_complete_code_goal_no_pr_is_downgraded_to_nudge():
    """Code goal + repo + zero PRs → completion gate nudges (new behavior)."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"},
               repo="owner/repo",
               effective_goal="ship the refactor",
               goal_source="explicit",
               pr_facts=_no_pr_code_facts())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge"
    assert d.prompt is None and d.needs_composition and d.verdict_remedy
    assert d.reason and "completion-gate" in d.reason


def test_complete_noncode_goal_no_pr_stays_complete():
    """Research/Q&A goal + no repo → NOT gated; session may complete without a PR."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"},
               repo=None,
               effective_goal="investigate the flaky test",
               goal_source="explicit",
               pr_facts=_no_pr_no_repo_facts())
    d = post_validate(Decision(action="complete", reason="research done"), ctx, CONFIG)
    assert d.action == "complete"


def test_complete_unarmed_pr_regression_guard():
    """Regression guard: open-unarmed PR still gets nudged (pre-existing behavior)."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_unarmed_facts())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge" and d.prompt is None and d.needs_composition and d.verdict_remedy


# --- Human deference rule ---

def test_defers_while_human_recently_active():
    ctx = _ctx(seconds_since_human=10.0)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is not None and d.action == "skip" and "human" in d.reason


def test_acts_after_defer_window_elapsed():
    ctx = _ctx(seconds_since_human=130.0)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is None  # falls through to the LLM decider


def test_no_deference_when_human_never_touched():
    ctx = _ctx(seconds_since_human=None)
    d = pre_decide(ctx, {"human_defer_seconds": 120, "max_actions_per_session_per_hour": 12,
                         "loop_repeat_limit": 3, "recovery_max_attempts": 2}, now=1000.0)
    assert d is None


# --- Part 2: pr_resolution bypasses the no-PR nudge ---

def test_complete_not_downgraded_when_pr_resolution_set():
    """Code goal + no PR, but pr_resolution is set → NOT nudged to open a PR."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"},
               repo="owner/repo",
               effective_goal="fix ci",
               goal_source="explicit",
               pr_facts=_no_pr_code_facts(),
               pr_resolution="not-needed")
    out = post_validate(Decision(action="complete"), ctx, CONFIG)
    assert out.action == "complete"  # resolved → NOT nudged to open a PR


def test_complete_downgraded_without_resolution():
    """Code goal + no PR + no pr_resolution → still nudged (unchanged legacy behavior)."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"},
               repo="owner/repo",
               effective_goal="fix ci",
               goal_source="explicit",
               pr_facts=_no_pr_code_facts())
    out = post_validate(Decision(action="complete"), ctx, CONFIG)
    assert out.action == "nudge"  # unchanged legacy behavior


# --- contradiction + dedupe helpers (spec §2, §4) ---
from server.automations.autopilot_policy import (
    claimed_pr_numbers, facts_contradicted, near_duplicate,
)


def no_prs():
    """PrFacts with has_repo=True and zero PRs — the 'code goal, nothing opened' case."""
    return PrFacts(has_repo=True, prs=())


def armed_pr():
    """PrFacts with a single open, armed PR."""
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, True, "u"),))


def test_claimed_pr_numbers_scans_tail_text():
    tail = [{"role": "assistant", "text": "PR #13393 is armed, see also PR#12"},
            {"role": "tool", "text": "gh pr view 99"}]
    assert claimed_pr_numbers(tail) == {13393, 12}


def test_facts_contradicted_when_tail_claims_unknown_pr():
    ctx = _ctx_with_facts(no_prs())          # has_repo=True, prs=()
    ctx.transcript_tail = [{"role": "assistant", "text": "PR #42 is armed"}]
    assert facts_contradicted(ctx) is True


def test_facts_contradicted_on_complete_status_without_armed_pr():
    ctx = _ctx_with_facts(no_prs())
    ctx.status = "complete"
    assert facts_contradicted(ctx) is True


def test_facts_not_contradicted_when_armed_pr_present():
    ctx = _ctx_with_facts(armed_pr())
    ctx.status = "complete"
    assert facts_contradicted(ctx) is False


def test_facts_not_contradicted_without_facts_or_repo():
    ctx = _ctx(status="complete")            # pr_facts is None
    assert facts_contradicted(ctx) is False


def test_near_duplicate_boundary():
    recent = [{"ts": 1000.0, "text": "fix the failing test in test_ws.py then arm the PR"}]
    same = "fix the failing test in test_ws.py then arm the PR"
    assert near_duplicate(same, recent, now=2000.0) is True
    fresh = "PR #99 conflicts with master; rebase onto origin/master and resolve server/app.py"
    assert near_duplicate(fresh, recent, now=2000.0) is False


def test_near_duplicate_outside_window_is_fresh():
    recent = [{"ts": 0.0, "text": "open a PR and arm auto-merge"}]
    assert near_duplicate("open a PR and arm auto-merge", recent,
                          now=7 * 3600.0) is False


# --------------------------------------------------------------------------- #
# Handed-back sessions are the human's, not autopilot's.                       #
#                                                                              #
# `hand_back` (tool or marker) derives to its own `handed_back` status, which   #
# carries no structured card (status.py). Live 2026-09-10/11: autopilot governed#
# those and the composer nudged them on ("no operator reply is coming in this  #
# async channel, so don't wait on it"), which (a) contradicts the tool's       #
# contract, (b) erases the "Answer needed" state the user was meant to see and #
# (c) loops: hand_back → nudge → hand_back (session 0898bcd5, three cycles in  #
# two minutes). The guardrail escalates instead — the decider never sees it.   #
# --------------------------------------------------------------------------- #

def test_handed_back_session_is_escalated_never_nudged():
    ctx = _ctx(status="handed_back",
               stuck={"kind": "handed_back",
                      "hand_back_reason": "Need your call on the 4GB memory limit"})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate"
    assert "handed back" in d.reason and "4GB memory limit" in d.reason


def test_handed_back_is_escalated_even_without_a_reason():
    """After a daemon restart the driver (and its reason) is gone, but the status
    alone still means 'the user is needed'."""
    ctx = _ctx(status="handed_back", stuck={"kind": "handed_back"})
    d = pre_decide(ctx, CONFIG, now=1000.0)
    assert d is not None and d.action == "escalate"


def test_structured_question_still_defers_to_the_decider():
    ctx = _ctx(status="waiting_question",
               stuck={"kind": "question", "question_id": "q1", "question": "Which?",
                      "options": ["a", "b"]})
    assert pre_decide(ctx, CONFIG, now=1000.0) is None


# --------------------------------------------------------------------------- #
# The completion-marker path (_auto_archive_completed / _completion_open_question) #
# already holds a session with an open structured card (question/permission/plan)  #
# instead of archiving it. The autopilot decision path's own `complete` -> archive #
# route had no equivalent: a decider that mis-judges "goal met" while a card is    #
# still open must not win — 7866e6a5 was archived over a live hand-back question   #
# on 2026-09-09 (issue #1756). `pre_decide` already forces `escalate` for a bare   #
# hand-back (`waiting_question` with no card); this closes the same hole for a     #
# structured card, where `pre_decide` intentionally defers to the decider.         #
# --------------------------------------------------------------------------- #

def test_complete_with_open_question_card_is_escalated_not_archived():
    ctx = _ctx(status="waiting_question",
               stuck={"kind": "question", "question_id": "q1", "question": "Which?",
                      "options": ["a", "b"]})
    d = post_validate(Decision(action="complete", reason="goal met"), ctx, CONFIG)
    assert d.action == "escalate"


def test_complete_with_open_permission_card_is_escalated_not_archived():
    ctx = _ctx(status="waiting_input",
               stuck={"kind": "permission", "tool_name": "Bash",
                      "tool_input": {"command": "pytest"}})
    d = post_validate(Decision(action="complete", reason="goal met"), ctx, CONFIG)
    assert d.action == "escalate"


def test_complete_with_open_plan_card_is_escalated_not_archived():
    ctx = _ctx(status="waiting_input",
               stuck={"kind": "plan", "tool_name": "ExitPlanMode", "plan_text": "..."})
    d = post_validate(Decision(action="complete", reason="goal met"), ctx, CONFIG)
    assert d.action == "escalate"


def test_complete_without_an_open_card_is_unaffected():
    """Regression guard: the new guard must not fire for the ordinary self-declared
    complete status (stuck.kind == 'complete')."""
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_armed_facts())
    d = post_validate(Decision(action="complete", reason="armed"), ctx, CONFIG)
    assert d.action == "complete"


def test_hand_back_fingerprint_keys_on_the_reason():
    """A NEW hand-back reason is new information: it must re-surface in the feed,
    so it needs a fresh fingerprint (the escalate de-spam keys on it)."""
    first = _ctx(status="handed_back",
                 stuck={"kind": "handed_back", "hand_back_reason": "first ask"})
    second = _ctx(status="handed_back",
                  stuck={"kind": "handed_back", "hand_back_reason": "second ask"})
    assert fingerprint(first) != fingerprint(second)
    assert fingerprint(first) == fingerprint(first)


def test_missing_new_pr_contradicts_even_with_old_merged_pr():
    from server.automations.autopilot_policy import facts_contradicted
    from server.automations.completion_bar import PrFact, PrFacts
    ctx = _ctx()
    ctx.repo = "o/r"
    ctx.pr_facts = PrFacts(has_repo=True, prs=(PrFact(
        number=1, state="merged", is_draft=False, auto_merge_enabled=False,
        url="https://github.com/o/r/pull/1"),))
    ctx.transcript_tail = [{"text": "PR #2 is now ready"}]
    assert facts_contradicted(ctx)


def test_pr_claim_identity_includes_repository():
    from server.automations.autopilot_policy import facts_contradicted, post_validate
    from server.automations.completion_bar import PrFact, PrFacts
    ctx = _ctx()
    ctx.repo = "o/r"
    ctx.stuck = {"kind": "idle"}
    ctx.pr_facts = PrFacts(has_repo=True, prs=(PrFact(
        number=7, state="merged", is_draft=False, auto_merge_enabled=False,
        url="https://github.com/o/r/pull/7"),))
    ctx.transcript_tail = [{"text": "Opened https://github.com/o/other/pull/7"}]
    assert facts_contradicted(ctx)
    assert post_validate(Decision(action="complete"), ctx, {}).action == "skip"


@pytest.mark.parametrize("text, expected", [
    ("Opened [PR #42](https://github.com/o/other/pull/42)", set()),
    ("PR #42 remains local; [PR #42](https://github.com/o/other/pull/42) is foreign",
     {("o/r", 42)}),
])
def test_qualified_markdown_pr_label_does_not_create_local_claim(text, expected):
    from server.automations.autopilot_policy import missing_claimed_prs
    from server.automations.completion_bar import PrFact, PrFacts
    ctx = _ctx(stuck={"kind": "idle"})
    ctx.repo = "o/r"
    ctx.pr_facts = PrFacts(has_repo=True, prs=(PrFact(
        number=42, state="merged", is_draft=False, auto_merge_enabled=False,
        url="https://github.com/o/other/pull/42"),))
    ctx.transcript_tail = [{"text": text}]
    assert missing_claimed_prs(ctx) == expected


def test_verified_unowned_pr_mention_does_not_block_research_completion():
    from server.automations.completion_bar import PrFacts
    ctx = _ctx(stuck={"kind": "idle"})
    ctx.repo = "o/r"
    ctx.effective_goal = "review PR #42"
    ctx.pr_facts = PrFacts(has_repo=True)
    ctx.transcript_tail = [{"text": "Reviewed PR #42"}]
    ctx.facts_verified = True
    assert post_validate(Decision(action="complete"), ctx, {}).action == "complete"

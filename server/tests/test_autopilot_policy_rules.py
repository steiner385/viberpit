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


def test_post_validate_rejects_empty_nudge():
    out = post_validate(Decision(action="nudge", prompt="  "), _ctx(status="done"), CONFIG)
    assert out.action == "escalate"


from server.automations.completion_bar import PrFact, PrFacts


def _armed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, True, "u"),))


def _unarmed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(1, "open", False, False, "u"),))


def test_complete_with_unarmed_pr_is_downgraded_to_nudge():
    ctx = _ctx(status="complete", stuck={"kind": "complete"}, pr_facts=_unarmed_facts())
    d = post_validate(Decision(action="complete", reason="agent said done"), ctx, CONFIG)
    assert d.action == "nudge" and d.prompt


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
    assert d.prompt
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
    assert d.action == "nudge" and d.prompt


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

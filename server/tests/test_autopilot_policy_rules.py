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

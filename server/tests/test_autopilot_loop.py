# server/tests/test_autopilot_loop.py
import pytest
from server.automations.autopilot import AutopilotLoop
from server.automations.autopilot_policy import DecisionContext, Decision


class FakeCtx:
    """Minimal AutomationContext stand-in: in-memory target-state + emit log."""
    def __init__(self):
        self.states = {}
        self.emits = []

    async def emit(self, level, summary, **kw):
        self.emits.append((level, summary, kw.get("kind")))

    async def get_target_state(self, t):
        return dict(self.states.get(t, {}))

    async def set_target_state(self, t, s):
        self.states[t] = s

    async def delete_target_state(self, t):
        self.states.pop(t, None)

    async def list_target_states(self):
        return dict(self.states)


def _dc(sid="s1"):
    return DecisionContext(session_id=sid, status="waiting_input", account="a", repo=None,
                           branch=None, effective_goal="g", goal_source="explicit",
                           stuck={"kind": "permission", "tool_name": "Bash",
                                  "tool_input": {"command": "pytest"}},
                           transcript_tail=[], ledger={})


def _loop(ctx, *, mode="auto", governed=("s1",), decision=Decision(action="approve", reason="ok"),
          applied=None, config=None):
    cfg = config or {"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                     "max_actions_per_day": 300, "recovery_max_attempts": 2,
                     "loop_repeat_limit": 3, "human_defer_seconds": 120,
                     "extra_deny_patterns": [], "enabled_repos": []}
    applied = applied if applied is not None else []

    async def read_config():
        return mode, cfg

    async def list_governed():
        return list(governed)

    async def get_context(sid):
        return _dc(sid)

    async def decide_fn(dc):
        return decision

    async def apply(sid, d):
        applied.append((sid, d.action))
        return d.action in ("approve", "deny", "answer", "nudge")

    loop = AutopilotLoop(ctx, read_config=read_config, list_governed=list_governed,
                         get_context=get_context, apply=apply, decide_fn=decide_fn,
                         clock=lambda: 1000.0)
    loop._applied = applied
    return loop


@pytest.mark.asyncio
async def test_auto_mode_applies_decision():
    ctx = FakeCtx()
    loop = _loop(ctx)
    await loop.tick()
    assert loop._applied == [("s1", "approve")]


@pytest.mark.asyncio
async def test_off_mode_is_noop():
    ctx = FakeCtx()
    loop = _loop(ctx, mode="off")
    await loop.tick()
    assert loop._applied == []


@pytest.mark.asyncio
async def test_propose_mode_emits_but_does_not_apply():
    ctx = FakeCtx()
    loop = _loop(ctx, mode="propose")
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_denylisted_permission_never_reaches_decider():
    ctx = FakeCtx()
    # decision would approve, but pre_decide forces deny for a force-push
    loop = _loop(ctx, decision=Decision(action="approve", reason="should be overridden"))

    async def dangerous_ctx(sid):
        dc = _dc(sid)
        dc.stuck = {"kind": "permission", "tool_name": "Bash",
                    "tool_input": {"command": "git push --force"}}
        return dc
    loop._get_context = dangerous_ctx
    await loop.tick()
    assert loop._applied == [("s1", "deny")]


@pytest.mark.asyncio
async def test_loop_detection_escalates_after_repeats():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="keep going"))
    for _ in range(4):
        await loop.tick()
    # after loop_repeat_limit (3) identical nudges, it escalates instead of applying
    assert ("s1", "escalate") not in loop._applied[:3]
    st = ctx.states.get("s1", {})
    assert st.get("repeat_count", 0) >= 3

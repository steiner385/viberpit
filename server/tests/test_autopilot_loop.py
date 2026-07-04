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


def _dc(sid="s1", *, no_auto_archive=False):
    return DecisionContext(session_id=sid, status="waiting_input", account="a", repo=None,
                           branch=None, effective_goal="g", goal_source="explicit",
                           stuck={"kind": "permission", "tool_name": "Bash",
                                  "tool_input": {"command": "pytest"}},
                           transcript_tail=[], ledger={}, no_auto_archive=no_auto_archive)


def _loop(ctx, *, mode="auto", governed=("s1",), decision=Decision(action="approve", reason="ok"),
          applied=None, config=None, no_auto_archive=False):
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
        return _dc(sid, no_auto_archive=no_auto_archive)

    async def decide_fn(dc):
        return decision

    async def apply(sid, d, ctx=None):
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


def test_actionable_statuses_includes_complete():
    """A session that emits its completion marker reaches `complete` status — the
    strongest goal-met signal. It MUST be actionable so the decider can judge it and
    archive a goal-met session; otherwise a well-behaved session strands in `complete`
    forever, archived by no one (backlog-runner has a separate path; humans do not)."""
    from server.automations.autopilot_policy import ACTIONABLE_STATUSES
    assert "complete" in ACTIONABLE_STATUSES


@pytest.mark.asyncio
async def test_complete_emits_and_does_not_apply():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"))
    await loop.tick()
    # 'complete' must never mutate — apply is never called ...
    assert loop._applied == []
    # ... and a terminal 'complete' marker is emitted for the feed.
    assert any(kind == "complete" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_non_mutating_nudge_still_escalates():
    """A nudge whose apply() keeps returning False (never lands) must still
    accumulate the fingerprint and eventually escalate — not loop forever."""
    ctx = FakeCtx()
    applied: list = []
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="keep going"), applied=applied)

    async def apply_never_lands(sid, d, ctx=None):
        applied.append((sid, d.action))
        return False  # the nudge never actually mutates
    loop._apply = apply_never_lands

    for _ in range(4):
        await loop.tick()
    # loop detection fired despite apply() always returning False
    assert any(kind == "escalated" for _, _, kind in ctx.emits)
    assert ctx.states.get("s1", {}).get("escalated") is True


@pytest.mark.asyncio
async def test_complete_auto_archives_and_clears_ledger():
    """Goal met in auto mode → the session is archived (via the archive seam,
    never via apply) and its ledger row is deleted (terminal rest)."""
    ctx = FakeCtx()
    ctx.states["s1"] = {"repeat_count": 2}          # pre-existing ledger to clear
    archived: list = []

    async def archive(sid):
        archived.append(sid)
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"))
    loop._archive = archive
    await loop.tick()
    assert archived == ["s1"]
    assert loop._applied == []                       # apply never invoked
    assert "s1" not in ctx.states                    # ledger cleared
    assert any(kind == "complete" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_complete_in_propose_mode_only_proposes_archive():
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid):
        archived.append(sid)
        return True

    loop = _loop(ctx, mode="propose", decision=Decision(action="complete", reason="goal met"))
    loop._archive = archive
    await loop.tick()
    assert archived == []                            # no mutation in propose mode
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_complete_archive_failure_is_best_effort():
    """An archive failure must not crash the tick; the complete marker still lands."""
    ctx = FakeCtx()

    async def archive_boom(sid):
        raise RuntimeError("archive failed")

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"))
    loop._archive = archive_boom
    await loop.tick()                                # must not raise
    assert any(kind == "complete" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_complete_does_not_archive_when_opted_out():
    """A manually-unarchived session (no_auto_archive) whose goal is met must NEVER be
    auto-archived — the loop surfaces the completion as a proposal and stops."""
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid):
        archived.append(sid)
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 no_auto_archive=True)
    loop._archive = archive
    await loop.tick()
    assert archived == []                                # opt-out blocks auto-archive
    assert loop._applied == []                           # and never mutates the session
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_opted_out_complete_is_deduped_across_ticks():
    """The 'not auto-archiving' proposal must surface ONCE for a stable stuck state, not
    every tick — mirrors the escalate de-spam so the feed isn't buried."""
    ctx = FakeCtx()

    async def archive(sid):
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 no_auto_archive=True)
    loop._archive = archive
    await loop.tick()
    await loop.tick()
    proposed = [e for e in ctx.emits if e[2] == "proposed"]
    assert len(proposed) == 1                            # deduped on the fingerprint


@pytest.mark.asyncio
async def test_escalation_emits_once_for_same_stuck_state():
    """A session parked in a terminal escalated state must not spam the activity
    feed: the same stuck-state escalation emits once, not once per tick."""
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="escalate", reason="needs a human"))
    for _ in range(3):
        await loop.tick()
    escalations = [e for e in ctx.emits if e[2] == "escalated"]
    assert len(escalations) == 1


@pytest.mark.asyncio
async def test_escalation_reemits_when_stuck_state_changes():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="escalate", reason="needs a human"))
    await loop.tick()

    async def different_ctx(sid):
        dc = _dc(sid)
        dc.stuck = {"kind": "question", "question": "Deploy to prod?", "options": ["yes", "no"]}
        return dc
    loop._get_context = different_ctx
    await loop.tick()
    escalations = [e for e in ctx.emits if e[2] == "escalated"]
    assert len(escalations) == 2                     # new stuck state → new information


@pytest.mark.asyncio
async def test_applied_action_clears_escalated_flag():
    """Once autopilot successfully acts on a session again, it is no longer
    'handed to a human' — a later escalation for the same state must re-emit."""
    ctx = FakeCtx()
    ctx.states["s1"] = {"escalated": True, "last_fingerprint": "stale", "repeat_count": 1}
    loop = _loop(ctx, decision=Decision(action="approve", reason="safe"))
    await loop.tick()
    assert ctx.states["s1"].get("escalated") is False

# server/tests/test_autopilot_loop.py
import pytest
from server.automations.autopilot import AutopilotLoop
from server.automations.autopilot_policy import DecisionContext, Decision
from server.automations.completion_bar import PrFact, PrFacts


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


def _armed_facts():
    return PrFacts(has_repo=True, prs=(PrFact(
        number=7, state="open", is_draft=False, auto_merge_enabled=True,
        url="u", checks="passing"),))


def _repo_ctx(sid="s1", status="complete", facts=None, tail=None, ledger=None):
    dc = _dc(sid)
    dc.status = status
    dc.repo = "o/r"
    dc.stuck = {"kind": status}
    dc.pr_facts = facts if facts is not None else PrFacts(has_repo=True, prs=())
    dc.transcript_tail = tail or []
    if ledger is not None:
        dc.ledger = ledger
    return dc


def _loop(ctx, *, mode="auto", governed=("s1",), decision=Decision(action="approve", reason="ok"),
          applied=None, config=None, no_auto_archive=False,
          compose=None, refresh_facts=None):
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
                         clock=lambda: 1000.0, compose=compose, refresh_facts=refresh_facts)
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
async def test_nudge_loop_survives_status_oscillation():
    """A rejected nudge (e.g. 'Prompt is too long' on a context-exhausted session)
    churns the momentary status between ticks (needs_attention <-> idle). Loop
    detection MUST still escalate and STOP nudging: the volatile status must not reset
    the 'keep nudging the same goal' counter, or autopilot nudges a stuck session
    forever (prod: PR #11273 session, escalate->nudge->escalate for ~7h, 2026-07-06)."""
    ctx = FakeCtx()
    applied: list = []
    # No pending permission/question — autopilot's only lever is a nudge toward the
    # goal, and the rejected nudge flaps the status every tick.
    flap = iter(["needs_attention", "idle"] * 8)

    async def oscillating_ctx(sid):
        st = next(flap)
        return DecisionContext(session_id=sid, status=st, account="a", repo=None,
                               branch=None, effective_goal="g", goal_source="explicit",
                               stuck={"kind": st}, transcript_tail=[], ledger={})

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="keep going"), applied=applied)
    loop._get_context = oscillating_ctx
    for _ in range(6):
        await loop.tick()
    # Escalated despite the status flapping every tick ...
    assert ctx.states.get("s1", {}).get("escalated") is True
    assert any(kind == "escalated" for _, _, kind in ctx.emits)
    # ... and it stopped nudging once escalated: at most loop_repeat_limit (3) nudges
    # over 6 ticks, not one per tick.
    assert applied.count(("s1", "nudge")) <= 3


@pytest.mark.asyncio
async def test_applied_action_clears_escalated_flag():
    """Once autopilot successfully acts on a session again, it is no longer
    'handed to a human' — a later escalation for the same state must re-emit."""
    ctx = FakeCtx()
    ctx.states["s1"] = {"escalated": True, "last_fingerprint": "stale", "repeat_count": 1}
    loop = _loop(ctx, decision=Decision(action="approve", reason="safe"))
    await loop.tick()
    assert ctx.states["s1"].get("escalated") is False


# ---------------------------------------------------------------------------
# Task 6: verify → compose → dedupe → remember pipeline tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_complete_verifies_and_archives():
    """Spec test 1: stale-facts complete + fresh-facts armed PR -> archive, no nudge."""
    ctx = FakeCtx()
    archived = []

    async def refresh_facts(sid):
        return _armed_facts()

    async def archive(sid):
        archived.append(sid)
        return True

    async def get_context(sid):
        return _repo_ctx(sid, tail=[{"role": "assistant", "text": "PR #7 is armed"}])

    loop = _loop(ctx, decision=Decision(action="complete", reason="marker"),
                 refresh_facts=refresh_facts)
    loop._get_context = get_context
    loop._archive = archive
    await loop.tick()
    assert archived == ["s1"]
    assert loop._applied == []          # no nudge was ever sent


@pytest.mark.asyncio
async def test_composed_nudge_is_sent_and_remembered():
    ctx = FakeCtx()

    async def compose(dc, decision):
        return "Fix the 2 failing tests in test_ws.py."

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="fix tests"),
                 compose=compose)
    await loop.tick()
    assert loop._applied == [("s1", "nudge")]
    nudges = ctx.states["s1"]["recent_nudges"]
    assert nudges[-1]["text"] == "Fix the 2 failing tests in test_ws.py."


@pytest.mark.asyncio
async def test_near_duplicate_composition_is_suppressed_and_counts():
    """Spec test 2: near-identical composed nudge -> suppressed, repeat climbs."""
    ctx = FakeCtx()
    text = "Fix the 2 failing tests in test_ws.py."

    async def compose(dc, decision):
        return text

    async def get_context(sid):
        return _repo_ctx(sid, status="done",
                         ledger={"recent_nudges": [{"ts": 999.0, "text": text}]})

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="fix tests"),
                 compose=compose)
    loop._get_context = get_context
    await loop.tick()
    assert loop._applied == []                            # send suppressed
    assert ctx.states["s1"]["repeat_count"] >= 1          # counted toward escalation


@pytest.mark.asyncio
async def test_composer_failure_escalates_never_sends_template():
    """Spec test 3: composer failure -> escalation, not a canned send."""
    ctx = FakeCtx()

    async def compose(dc, decision):
        return None

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="hint"), compose=compose)
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "escalated" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_refresh_is_rate_limited_per_session():
    ctx = FakeCtx()
    calls = []

    async def refresh_facts(sid):
        calls.append(sid)
        return None

    async def get_context(sid):
        st = await ctx.get_target_state(sid)
        return _repo_ctx(sid, ledger=st)

    async def compose(dc, decision):
        return f"unique direction {len(calls)} referencing file_{len(calls)}.py"

    loop = _loop(ctx, decision=Decision(action="nudge", prompt="hint"),
                 compose=compose, refresh_facts=refresh_facts)
    loop._get_context = get_context
    await loop.tick()
    await loop.tick()                                     # same clock -> within 600s
    assert calls == ["s1"]                                # second tick skipped refresh


@pytest.mark.asyncio
async def test_no_composer_and_no_prompt_escalates():
    ctx = FakeCtx()
    d = Decision(action="nudge", prompt=None, needs_composition=True,
                 downgraded_from="complete")
    loop = _loop(ctx, decision=d)                         # compose=None (legacy)
    await loop.tick()
    assert loop._applied == []
    assert any(kind == "escalated" for _, _, kind in ctx.emits)

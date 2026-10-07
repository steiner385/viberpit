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
        self.reservations = []

    async def reserve_autopilot_action(self, limit, now):
        self.reservations = [t for t in self.reservations if t > now - 86400]
        if len(self.reservations) >= limit:
            return False
        self.reservations.append(now)
        return True

    async def autopilot_action_available(self, limit, now):
        return len([t for t in self.reservations if t > now - 86400]) < limit

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


def _dc(sid="s1", *, no_auto_archive=False, origin=None):
    # stuck has no open card by default: the "complete"-decision tests below feed
    # this through post_validate, which now escalates (never archives) a `complete`
    # arriving on top of an open question/permission/plan card (#1756) — a fixture
    # default of "permission" here would spuriously trip that guard.
    return DecisionContext(session_id=sid, status="waiting_input", account="a", repo=None,
                           branch=None, effective_goal="g", goal_source="explicit",
                           stuck={"kind": "idle"},
                           transcript_tail=[], ledger={}, no_auto_archive=no_auto_archive,
                           origin=origin)


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
          applied=None, config=None, no_auto_archive=False, origin=None,
          compose=None, refresh_facts=None):
    cfg = config or {"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                     "max_actions_per_day": 300, "recovery_max_attempts": 2,
                     "loop_repeat_limit": 3, "human_defer_seconds": 120,
                     "extra_deny_patterns": [], "enabled_repos": [],
                     "auto_archive_completed": True, "auto_archive_origins": []}
    applied = applied if applied is not None else []

    async def read_config():
        return mode, cfg

    async def list_governed():
        return list(governed)

    async def get_context(sid):
        return _dc(sid, no_auto_archive=no_auto_archive, origin=origin)

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

    async def archive(sid, dc=None):
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

    async def archive(sid, dc=None):
        archived.append(sid)
        return True

    loop = _loop(ctx, mode="propose", decision=Decision(action="complete", reason="goal met"))
    loop._archive = archive
    await loop.tick()
    assert archived == []                            # no mutation in propose mode
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_complete_archive_failure_is_best_effort():
    """An archive failure must not crash or publish a false completion."""
    ctx = FakeCtx()

    async def archive_boom(sid, dc=None):
        raise RuntimeError("archive failed")

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"))
    loop._archive = archive_boom
    await loop.tick()                                # must not raise
    assert not any(kind == "complete" for _, _, kind in ctx.emits)
    assert "s1" in ctx.states


@pytest.mark.asyncio
async def test_complete_does_not_archive_when_opted_out():
    """A manually-unarchived session (no_auto_archive) whose goal is met must NEVER be
    auto-archived — the loop surfaces the completion as a proposal and stops."""
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid, dc=None):
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

    async def archive(sid, dc=None):
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 no_auto_archive=True)
    loop._archive = archive
    await loop.tick()
    await loop.tick()
    proposed = [e for e in ctx.emits if e[2] == "proposed"]
    assert len(proposed) == 1                            # deduped on the fingerprint


@pytest.mark.asyncio
async def test_complete_does_not_archive_when_auto_archive_disabled():
    """`auto_archive_completed: False` must stop the loop from archiving a goal-met
    session — same "surface once, never mutate" treatment as the opt-out — while
    leaving every other governance behavior (nudge/escalate/etc.) untouched."""
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid, dc=None):
        archived.append(sid)
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 config={"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                         "max_actions_per_day": 300, "recovery_max_attempts": 2,
                         "loop_repeat_limit": 3, "human_defer_seconds": 120,
                         "extra_deny_patterns": [], "enabled_repos": [],
                         "auto_archive_completed": False, "auto_archive_origins": []})
    loop._archive = archive
    await loop.tick()
    assert archived == []
    assert loop._applied == []
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_complete_archives_when_origin_matches_scope():
    """`auto_archive_origins` scopes archival to sessions spawned by the listed
    automations — a matching origin still archives normally."""
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid, dc=None):
        archived.append(sid)
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 origin="backlog-runner",
                 config={"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                         "max_actions_per_day": 300, "recovery_max_attempts": 2,
                         "loop_repeat_limit": 3, "human_defer_seconds": 120,
                         "extra_deny_patterns": [], "enabled_repos": [],
                         "auto_archive_completed": True,
                         "auto_archive_origins": ["backlog-runner"]})
    loop._archive = archive
    await loop.tick()
    assert archived == ["s1"]


@pytest.mark.asyncio
async def test_complete_does_not_archive_when_origin_outside_scope():
    """A session not spawned by a listed origin (including a human-created session,
    origin=None) is left unarchived when `auto_archive_origins` is non-empty."""
    ctx = FakeCtx()
    archived: list = []

    async def archive(sid, dc=None):
        archived.append(sid)
        return True

    loop = _loop(ctx, decision=Decision(action="complete", reason="goal met"),
                 origin=None,
                 config={"allow_tool_approvals": True, "max_actions_per_session_per_hour": 12,
                         "max_actions_per_day": 300, "recovery_max_attempts": 2,
                         "loop_repeat_limit": 3, "human_defer_seconds": 120,
                         "extra_deny_patterns": [], "enabled_repos": [],
                         "auto_archive_completed": True,
                         "auto_archive_origins": ["backlog-runner"]})
    loop._archive = archive
    await loop.tick()
    assert archived == []
    assert loop._applied == []
    assert any(kind == "proposed" for _, _, kind in ctx.emits)


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
async def test_escalation_reemits_after_a_human_has_steered():
    """A handed-back session escalates once; the human answers (human-deference
    skip); the session hands back again with the SAME reason → that is news to the
    human again, so the feed line must reappear (the de-spam flag is released by
    the human-steering skip, the one signal that the human saw it)."""
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="escalate", reason="handed back to the user"))
    await loop.tick()
    assert len([e for e in ctx.emits if e[2] == "escalated"]) == 1

    async def human_steering(sid):
        dc = _dc(sid)
        dc.last_human_interaction_at = "2026-10-07T12:00:00"
        dc.seconds_since_human = 5.0        # pre_decide → skip "human steering"
        return dc
    loop._get_context = human_steering
    await loop.tick()
    assert ctx.states["s1"].get("escalated") is False

    async def handed_back_again(sid):
        return _dc(sid)
    loop._get_context = handed_back_again
    await loop.tick()
    assert len([e for e in ctx.emits if e[2] == "escalated"]) == 2


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

    async def archive(sid, dc=None):
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


# ---------------------------------------------------------------------------
# Task 6 fix: ledger-clobber regression tests
# (composer-failure escalate + apply-False paths must preserve last_facts_refresh)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_composer_failure_preserves_last_facts_refresh():
    """Regression: composer-failure escalate was passing raw `ledger` (pre-verify
    snapshot) to _escalate, evicting `last_facts_refresh` that _verify_nudge_facts
    had just written into ctx.ledger and persisted.  After the fix, the stored state
    must still carry `last_facts_refresh` so the next tick skips the refresh."""
    ctx = FakeCtx()
    refresh_calls: list[str] = []

    async def refresh_facts(sid: str):
        refresh_calls.append(sid)
        return PrFacts(has_repo=True)  # successful refresh confirms no linked PR

    async def compose(dc, decision):
        return None   # composer fails → escalation path

    async def get_context(sid):
        # status="complete" + has_repo + no armed PR → facts_contradicted=True
        return _repo_ctx(sid, status="complete")

    loop = _loop(ctx,
                 decision=Decision(action="nudge", prompt="hint",
                                   downgraded_from="complete"),
                 refresh_facts=refresh_facts, compose=compose)
    loop._get_context = get_context
    await loop.tick()

    # refresh_facts was called → _verify_nudge_facts stamped last_facts_refresh
    assert refresh_calls == ["s1"], "expected exactly one refresh on first tick"
    # Bug: _escalate(sid, ledger, ...) would clobber ctx.ledger's last_facts_refresh.
    # Fix: _escalate(sid, ctx.ledger, ...) preserves it.
    assert "last_facts_refresh" in ctx.states.get("s1", {}), (
        "last_facts_refresh must survive the escalation write-back")


@pytest.mark.asyncio
async def test_apply_false_preserves_last_facts_refresh():
    """Regression: when apply() returns False, _record_fingerprint was using the raw
    `ledger` snapshot (pre-verify), evicting `last_facts_refresh`.  After the fix,
    the stored state must still carry `last_facts_refresh`."""
    ctx = FakeCtx()
    refresh_calls: list[str] = []

    async def refresh_facts(sid: str):
        refresh_calls.append(sid)
        return PrFacts(has_repo=True)  # successful refresh confirms no linked PR

    async def compose(dc, decision):
        return "Do the thing with file.py to fix the tests."

    async def get_context(sid):
        return _repo_ctx(sid, status="complete")

    applied: list = []
    loop = _loop(ctx,
                 decision=Decision(action="nudge", prompt="hint",
                                   downgraded_from="complete"),
                 applied=applied, refresh_facts=refresh_facts, compose=compose)
    loop._get_context = get_context

    async def apply_returns_false(sid, d, actx=None):
        applied.append((sid, d.action))
        return False  # nudge never lands

    loop._apply = apply_returns_false
    await loop.tick()

    assert refresh_calls == ["s1"], "expected exactly one refresh on first tick"
    # Bug: _record_fingerprint(sid, ledger, ...) clobbered ctx.ledger's last_facts_refresh.
    # Fix: _record_fingerprint(sid, ctx.ledger, ...) preserves it.
    assert "last_facts_refresh" in ctx.states.get("s1", {}), (
        "last_facts_refresh must survive the apply-False write-back")


# ---------------------------------------------------------------------------
# Final-review fix regressions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_composer_decline_increments_repeat_count_and_short_circuits():
    """C1 regression: when compose() returns None, the escalate path must record a
    fingerprint repeat exactly like a suppressed nudge, so pre_decide's loop detection
    (repeat_count >= loop_repeat_limit) stops calling the composer after ~3 declines.
    Before the fix, repeat_count never climbed on this path → ~4,300 uncharged Sonnet
    calls/day per persistently-declining session."""
    ctx = FakeCtx()
    compose_calls: list[str] = []

    async def compose(dc, decision):
        compose_calls.append(dc.session_id)
        return None   # always declines

    async def get_context(sid):
        # Return context carrying the PERSISTED ledger so repeat_count accumulates
        st = await ctx.get_target_state(sid)
        dc = _dc(sid)
        dc.ledger = st
        return dc

    loop_repeat_limit = 3
    loop = _loop(ctx, decision=Decision(action="nudge", prompt="hint"), compose=compose)
    loop._get_context = get_context

    # Tick loop_repeat_limit + 1 times; the extra tick verifies the composer stops
    for _ in range(loop_repeat_limit + 1):
        await loop.tick()

    # repeat_count must have climbed to at least loop_repeat_limit
    st = ctx.states.get("s1", {})
    assert st.get("repeat_count", 0) >= loop_repeat_limit, (
        f"repeat_count={st.get('repeat_count')} must be >= loop_repeat_limit={loop_repeat_limit}")

    # After the limit is tripped, pre_decide short-circuits — compose is NOT called again.
    # Composer was called at most loop_repeat_limit times (limit ticks before trip) not more.
    assert compose_calls.count("s1") <= loop_repeat_limit, (
        f"compose called {compose_calls.count('s1')} times; must stop after loop_repeat_limit")


@pytest.mark.asyncio
async def test_decline_archive_preserves_last_facts_refresh():
    """I1 regression: no_auto_archive + goal-met where _verify_facts just stamped
    last_facts_refresh into ctx.ledger.  _decline_archive must use ctx.ledger (not the
    raw pre-verify snapshot) so last_facts_refresh survives the write-back."""
    ctx = FakeCtx()
    archived: list = []

    # refresh_facts returns armed facts → downgraded decision becomes 'complete'
    async def refresh_facts(sid: str):
        return _armed_facts()

    async def archive(sid: str, dc=None):
        archived.append(sid)
        return True

    async def get_context(sid):
        # _repo_ctx: status="complete", has_repo, no armed PR → facts_contradicted=True
        # no_auto_archive=True → _decline_archive path
        st = await ctx.get_target_state(sid)
        dc = _repo_ctx(sid, status="complete")
        dc.no_auto_archive = True
        # The downgraded_from="complete" nudge triggers _verify_facts which stamps
        # last_facts_refresh.  Carry any persisted ledger so rate-limit doesn't block.
        dc.ledger = {**dc.ledger, **st}
        return dc

    # Decider produces a downgraded nudge (post_validate downgrades complete→nudge when
    # the PR bar isn't met); but refresh_facts returns armed facts → restores complete.
    # With no_auto_archive=True, the loop calls _decline_archive.
    loop = _loop(ctx,
                 decision=Decision(action="complete", reason="goal met"),
                 refresh_facts=refresh_facts)
    loop._get_context = get_context
    loop._archive = archive
    await loop.tick()

    # _verify_facts must have stamped last_facts_refresh
    st = ctx.states.get("s1", {})
    assert "last_facts_refresh" in st, (
        "last_facts_refresh must survive the _decline_archive write-back")
    # The decline-archive emit must have happened (proposal to the user)
    assert any(kind == "proposed" for _, _, kind in ctx.emits), (
        "expected a 'proposed' emit from _decline_archive")


@pytest.mark.asyncio
async def test_escalation_observation_preserves_activity_and_clears():
    from server.automations.autopilot import Autopilot
    from server.automations.context import publish_observation
    from types import SimpleNamespace

    ctx = SimpleNamespace()
    publish_observation(ctx, "autopilot_counts", {"advancing": 2, "escalated": 1})
    status = Autopilot().status(ctx)
    assert status.waiting_reason == "human"
    assert status.level == "active"
    assert status.counts["advancing"] == 2
    publish_observation(ctx, "autopilot_counts", {"advancing": 2, "escalated": 0})
    assert Autopilot().status(ctx).waiting_reason is None


@pytest.mark.asyncio
async def test_human_deference_ticks_never_consume_repetition_budget():
    ctx = FakeCtx()
    loop = _loop(ctx)
    elapsed = 10.0

    async def get_context(sid):
        dc = _dc(sid)
        dc.seconds_since_human = elapsed
        return dc

    loop._get_context = get_context
    for elapsed in (10.0, 30.0, 50.0):
        await loop.tick()
        assert ctx.states.get("s1", {}).get("repeat_count", 0) == 0
    elapsed = 130.0
    await loop.tick()
    assert loop._applied == [("s1", "approve")]
    assert not any(kind == "escalated" for _, _, kind in ctx.emits)


@pytest.mark.asyncio
async def test_hourly_budget_skips_do_not_poison_next_hour():
    ctx = FakeCtx()
    ctx.states["s1"] = {"hour_actions": [999.0] * 12}
    loop = _loop(ctx)
    for _ in range(10):
        await loop.tick()
    assert ctx.states["s1"].get("repeat_count", 0) == 0
    loop._clock = lambda: 4600.0
    await loop.tick()
    assert loop._applied == [("s1", "approve")]


@pytest.mark.asyncio
async def test_human_interaction_resets_once_even_after_deference_expired():
    from server.automations.autopilot_policy import fingerprint
    ctx = FakeCtx()
    ctx.states["s1"] = {
        "accounting_version": 1, "last_fingerprint": fingerprint(_dc()),
        "repeat_count": 3, "recovery_attempts": 2, "escalated": True,
        "hour_actions": [900.0], "last_human_interaction_at": "2026-10-07T12:00:00",
    }
    loop = _loop(ctx)

    async def get_context(sid):
        dc = _dc(sid)
        dc.last_human_interaction_at = "2026-10-07T12:05:00"
        dc.seconds_since_human = 130.0
        return dc

    loop._get_context = get_context
    for _ in range(5):
        await loop.tick()
    assert len(loop._applied) == 3
    state = ctx.states["s1"]
    assert state["repeat_count"] == 3
    assert state["recovery_attempts"] == 0
    assert state["hour_actions"] == [1000.0] * 4
    assert len([e for e in ctx.emits if e[2] == "escalated"]) == 1


@pytest.mark.asyncio
async def test_legacy_repetition_reconciles_only_once_across_restart():
    from server.automations.autopilot_policy import fingerprint
    ctx = FakeCtx()
    ctx.states["s1"] = {
        "last_fingerprint": fingerprint(_dc()), "repeat_count": 90,
        "hour_actions": [900.0], "recovery_attempts": 1,
        "recent_nudges": [{"ts": 900.0, "text": "previous nudge"}],
        "escalated": True, "last_facts_refresh": 800.0,
    }
    loop = _loop(ctx)
    await loop.tick()
    assert len(loop._applied) == 1
    state = ctx.states["s1"]
    assert state["accounting_version"] == 1
    assert state["repeat_count"] == 1
    assert state["recovery_attempts"] == 1
    assert state["hour_actions"] == [1000.0, 1000.0]
    assert state["recent_nudges"] == [{"ts": 1000.0, "text": "previous nudge"}]
    assert "last_facts_refresh" not in state
    for _ in range(5):
        restarted = _loop(ctx)
        await restarted.tick()
    assert ctx.states["s1"]["repeat_count"] == 3
    assert len([e for e in ctx.emits if e[2] == "escalated"]) == 1


@pytest.mark.asyncio
async def test_model_skip_does_not_count_as_attempt():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="skip", reason="waiting"))
    for _ in range(10):
        await loop.tick()
    assert ctx.states.get("s1", {}).get("repeat_count", 0) == 0
    assert loop._applied == []
    assert len([e for e in ctx.emits if e[2] == "waiting"]) == 1


@pytest.mark.asyncio
async def test_rejected_actions_remain_bounded_and_charged():
    ctx = FakeCtx()
    loop = _loop(ctx)
    attempts = []

    async def reject(sid, decision, dc):
        attempts.append(sid)
        return False

    loop._apply = reject
    for _ in range(10):
        await loop.tick()
    assert attempts == ["s1"] * 3
    assert ctx.states["s1"]["repeat_count"] == 3
    assert ctx.states["s1"].get("hour_actions", []) == []
    assert ctx.reservations == [1000.0] * 3
    assert len([e for e in ctx.emits if e[2] == "escalated"]) == 1


@pytest.mark.asyncio
async def test_legacy_reconciliation_preserves_hand_back_and_recovery_cap():
    for kind in ("handed_back", "needs_attention"):
        ctx = FakeCtx()
        ctx.states["s1"] = {"repeat_count": 90, "recovery_attempts": 2}
        loop = _loop(ctx)

        async def get_context(sid):
            dc = _dc(sid)
            dc.stuck = {"kind": kind}
            dc.last_human_interaction_at = "2026-10-07T12:00:00"
            dc.seconds_since_human = 500.0
            return dc

        loop._get_context = get_context
        await loop.tick()
        assert loop._applied == []
        assert ctx.states["s1"]["recovery_attempts"] == 2
        assert ctx.states["s1"]["escalated"] is True


@pytest.mark.asyncio
async def test_missing_composer_attempts_are_bounded():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action="nudge", needs_composition=True))
    decisions = []

    async def decide(dc):
        decisions.append(dc.session_id)
        return Decision(action="nudge", needs_composition=True)

    loop._decide_fn = decide
    for _ in range(8):
        await loop.tick()
    assert decisions == ["s1"] * 3
    assert ctx.states["s1"]["repeat_count"] == 3
    assert loop._applied == []


@pytest.mark.asyncio
async def test_contradicted_facts_refresh_before_escalation_judgment():
    ctx = FakeCtx()
    seen = []

    async def get_context(sid):
        return _repo_ctx(sid, status="idle", tail=[{"text": "PR #7 is armed"}])

    async def refresh(sid):
        return _armed_facts()

    async def decide(dc):
        seen.append(dc.pr_facts)
        return Decision(action="escalate", reason="test")

    loop = _loop(ctx, refresh_facts=refresh)
    loop._get_context = get_context
    loop._decide_fn = decide
    await loop.tick()
    assert seen == [_armed_facts()]


@pytest.mark.asyncio
async def test_failed_facts_refresh_defers_without_model_or_action():
    ctx = FakeCtx()
    calls = []

    async def get_context(sid):
        return _repo_ctx(sid, tail=[{"text": "PR #7 is armed"}])

    async def refresh(sid):
        calls.append(sid)
        return None

    loop = _loop(ctx, refresh_facts=refresh)
    loop._get_context = get_context
    for _ in range(5):
        await loop.tick()
    assert loop._applied == []
    assert calls == ["s1"]
    assert ctx.states["s1"].get("repeat_count", 0) == 0
    assert len([e for e in ctx.emits if e[2] == "waiting"]) == 1


@pytest.mark.asyncio
async def test_hand_back_prevents_contradiction_refresh_and_model_call():
    ctx = FakeCtx()
    calls = []

    async def refresh(sid):
        calls.append(sid)
        return _armed_facts()

    async def get_context(sid):
        dc = _repo_ctx(sid, status="handed_back", tail=[{"text": "PR #7 is armed"}])
        return dc

    loop = _loop(ctx, refresh_facts=refresh)
    loop._get_context = get_context
    await loop.tick()
    assert calls == []
    assert loop._applied == []
    assert ctx.states["s1"]["escalated"]

@pytest.mark.asyncio
async def test_durable_budget_refusal_does_not_apply_or_increment_repetition():
    from unittest.mock import AsyncMock
    ctx = FakeCtx()
    ctx.reserve_autopilot_action = AsyncMock(return_value=False)
    loop = _loop(ctx)
    await loop.tick()
    assert loop._applied == []
    assert ctx.states['s1']['repeat_count'] == 0
    assert ctx.autopilot_counts['budget_blocked'] == 1
    assert ctx.autopilot_counts['waiting'] == 1


@pytest.mark.asyncio
async def test_epoch_migration_is_conservative_and_only_once():
    ctx = FakeCtx()
    ctx.states['s1'] = {'accounting_version': 1, 'hour_actions': [12.0, 13.0],
                        'recent_nudges': [{'ts': 12.0, 'text': 'old'}],
                        'last_facts_refresh': 15.0, 'recovery_attempts': 2}
    loop = _loop(ctx, decision=Decision(action='skip', reason='waiting'))
    epoch = 1_800_000_000.0
    loop._clock = lambda: epoch
    await loop.tick()
    state = ctx.states['s1']
    assert state['clock_version'] == 1
    assert state['hour_actions'] == [epoch, epoch]
    assert state['recent_nudges'] == [{'ts': epoch, 'text': 'old'}]
    assert 'last_facts_refresh' not in state
    assert state['recovery_attempts'] == 2
    epoch += 100
    await loop.tick()
    assert ctx.states['s1']['hour_actions'] == [epoch - 100, epoch - 100]


@pytest.mark.asyncio
async def test_archive_refusal_retains_state_and_context():
    from unittest.mock import AsyncMock
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action='complete', reason='done'))
    loop._archive = AsyncMock(return_value=False)
    await loop.tick()
    assert 's1' in ctx.states
    assert loop._archive.await_args.args[1].session_id == 's1'


@pytest.mark.asyncio
async def test_final_authority_refusal_never_reserves_or_applies():
    from unittest.mock import AsyncMock
    ctx = FakeCtx()
    loop = _loop(ctx)
    loop._may_act = AsyncMock(return_value=False)
    await loop.tick()
    assert loop._applied == []
    assert ctx.reservations == []


@pytest.mark.asyncio
async def test_waiting_reason_transition_emits_once_without_warning():
    ctx = FakeCtx()
    loop = _loop(ctx, decision=Decision(action='skip', reason='waiting'))
    for _ in range(3):
        await loop.tick()
    waiting = [e for e in ctx.emits if e[2] == 'waiting']
    assert len(waiting) == 1
    assert waiting[0][0] == 'info'
    assert ctx.states['s1']['observation']['reason'] == 'waiting'
    assert ctx.autopilot_counts['waiting'] == 1


@pytest.mark.asyncio
async def test_hourly_wait_deadline_ignores_expired_history():
    ctx = FakeCtx()
    ctx.states['s1'] = {'clock_version': 1, 'accounting_version': 1,
                        'hour_actions': [1.0] + [9000.0] * 12}
    loop = _loop(ctx)
    loop._clock = lambda: 10_000.0
    await loop.tick()
    assert ctx.states['s1']['observation']['deadline'] == 12_600.0
    assert loop._applied == []


@pytest.mark.asyncio
async def test_paused_sessions_contribute_waiting_reasons_once():
    ctx = FakeCtx()
    ctx.autopilot_withheld = {'manual': 'paused', 'policy': 'policy_paused'}
    loop = _loop(ctx, governed=())
    await loop.tick()
    await loop.tick()
    assert ctx.autopilot_counts['waiting'] == 2
    assert ctx.autopilot_counts['paused'] == 1
    assert ctx.autopilot_counts['policy_paused'] == 1
    assert len(ctx.emits) == 2
    assert all(level == 'info' and kind == 'waiting' for level, _, kind in ctx.emits)


def test_hand_back_wait_count_retains_human_priority():
    from server.automations.autopilot_observation import autopilot_status
    assert autopilot_status({'waiting': 1, 'handed_back': 1}).waiting_reason == 'human'


@pytest.mark.asyncio
async def test_depleted_durable_budget_skips_model_and_composer_without_loop_charge():
    from unittest.mock import AsyncMock
    ctx = FakeCtx()
    ctx.reservations = [1000.0] * 300
    composer = AsyncMock(return_value=None)
    loop = _loop(ctx, decision=Decision(action='nudge', prompt='continue'), compose=composer)
    loop._decide_fn = AsyncMock(return_value=Decision(action='nudge', prompt='continue'))
    for _ in range(5):
        await loop.tick()
    loop._decide_fn.assert_not_awaited()
    composer.assert_not_awaited()
    assert ctx.states['s1']['repeat_count'] == 0
    assert not any(kind == 'escalated' for _, _, kind in ctx.emits)
    assert ctx.autopilot_counts['budget_blocked'] == 1


@pytest.mark.asyncio
async def test_failed_final_authority_never_overwrites_new_owner_ledger():
    ctx = FakeCtx()
    loop = _loop(ctx)
    replacement = {'clock_version': 1, 'accounting_version': 1, 'hour_actions': [777.0],
                   'repeat_count': 8, 'owner': 'new owner'}

    async def handoff(sid, dc):
        ctx.states[sid] = dict(replacement)
        return False

    loop._may_act = handoff
    await loop.tick()
    assert ctx.states['s1'] == replacement
    assert loop._applied == []
    assert ctx.reservations == []


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [Decision(action='escalate', reason='stale'),
                                      Decision(action='nudge', prompt='continue')])
async def test_owner_change_during_model_never_rewrites_ledger(decision):
    ctx = FakeCtx()
    loop = _loop(ctx)
    current = True
    replacement = {'clock_version': 1, 'accounting_version': 1,
                   'hour_actions': [888.0], 'repeat_count': 7}

    async def still_current(sid, dc):
        return current

    async def decide(dc):
        nonlocal current
        current = False
        ctx.states[dc.session_id] = dict(replacement)
        return decision

    loop._is_current = still_current
    loop._decide_fn = decide
    await loop.tick()
    assert ctx.states['s1'] == replacement
    assert loop._applied == []
    assert not ctx.emits


@pytest.mark.asyncio
async def test_lowered_hourly_cap_deadline_waits_for_enough_expirations():
    ctx = FakeCtx()
    ctx.states['s1'] = {'clock_version': 1, 'accounting_version': 1,
                        'hour_actions': [9000.0 + i for i in range(14)]}
    loop = _loop(ctx)
    loop._clock = lambda: 10_000.0
    await loop.tick()
    assert ctx.states['s1']['observation']['deadline'] == 12_602.0

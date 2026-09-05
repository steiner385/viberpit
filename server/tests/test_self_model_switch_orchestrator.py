"""Orchestrator consumption of a session's self-directed model request.

Task 7 is the integration point: it consumes the request the driver latched at
turn end (Task 3's ``driver.last_model_request``), runs the guards (Task 6), runs
the policy (Task 5), and either applies the switch via ``set_model_for_session``
or parks it for a human (Task 4's ``pending_model_reason``).

Harness: built on ``server/tests/test_orchestrator_model_gate.py``'s proven
pattern — a real in-memory DB via ``init_db``, the fake SDK factory, and
``select_account`` patched — per the task-7 harness ruling (a second
orchestrator-faking pattern is the parallel-implementation anti-pattern the
constitution forbids). A real ``SessionOrchestrator`` is built for every test
here; only ``_spawn`` (so the sync handler's fire-and-forget dispatch can be
observed without an event loop consuming the coroutine) and, per test, the
collaborator under scrutiny (``set_model_for_session`` / ``_park_self_model_
request`` / the policy function) are stubbed on the instance — mirroring how
``test_orchestrator_model_gate.py``'s ``_running_orch`` attaches a fake driver
to a real orchestrator instead of inventing a second harness.

``_provider_of`` is NOT stubbed by default: it is a filesystem check
(``ccswitch.accounts.provider_of``) already isolated to the harness's
``CCSWITCH_HOME``, so "alpha" naturally resolves to "anthropic" with no Copilot
token dir present — only the Copilot test overrides it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from server.sessions.self_model_guards import SelfModelState
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.tests.test_orchestrator_model_gate import _harness, _seed_account


class _FakeDriver:
    """Bare stand-in for a live driver: only the attribute
    ``_maybe_handle_model_request`` reads. A real driver owns a PTY/subprocess
    this unit test has no business spinning up. ``aclose`` is a no-op so the
    teardown regression test can drive the real ``close_session`` path (which
    unconditionally awaits ``driver.aclose()``) without a real driver."""

    def __init__(self, req=None):
        self.last_model_request = req

    async def aclose(self):
        pass


def _attach(orch, *, session_id="s1", model="opus", account="alpha", req=None):
    rec = SessionRecord(
        id=session_id, name="n", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
        cols=80, rows=24, cwd="/tmp", model=model, updated_at=datetime.utcnow())
    orch._records[session_id] = rec
    orch._drivers[session_id] = _FakeDriver(req)
    return rec


def _stub_spawn(orch):
    """Replace ``_spawn`` with a recorder that stores the coroutine WITHOUT
    running it, so the sync-consumption tests can assert on dispatch alone.
    Any unstarted coroutine objects are closed at the end of the test to avoid
    a 'coroutine was never awaited' warning."""
    spawned = []
    orch._spawn = lambda coro, **kw: spawned.append(coro)
    return spawned


def _close_all(coros):
    for c in coros:
        c.close()


# ---------------------------------------------------------------------------
# _maybe_handle_model_request: sync, fire-and-forget dispatch
# ---------------------------------------------------------------------------

def test_no_request_is_a_no_op(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                _attach(orch, req=None)
                spawned = _stub_spawn(orch)
                orch._maybe_handle_model_request("s1", "idle")
                return spawned
            finally:
                await orch.stop()

        spawned = asyncio.run(_run())

    assert spawned == []


def test_mid_turn_status_defers(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, req=req)
                spawned = _stub_spawn(orch)
                orch._maybe_handle_model_request("s1", "in_progress")
                return spawned
            finally:
                await orch.stop()

        spawned = asyncio.run(_run())

    assert spawned == []


def test_a_request_is_consumed_exactly_once(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, req=req)
                spawned = _stub_spawn(orch)
                orch._maybe_handle_model_request("s1", "idle")
                orch._maybe_handle_model_request("s1", "idle")
                n = len(spawned)
                _close_all(spawned)
                return n
            finally:
                await orch.stop()

        n = asyncio.run(_run())

    assert n == 1


def test_a_new_turn_id_is_consumed_again(tmp_path):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, req=req)
                spawned = _stub_spawn(orch)
                orch._maybe_handle_model_request("s1", "idle")
                orch._drivers["s1"].last_model_request = {**req, "turn_id": "t2"}
                orch._maybe_handle_model_request("s1", "idle")
                n = len(spawned)
                _close_all(spawned)
                return n
            finally:
                await orch.stop()

        n = asyncio.run(_run())

    assert n == 2


# ---------------------------------------------------------------------------
# _apply_model_request: guards -> policy -> apply or park
# ---------------------------------------------------------------------------

def _patch_decision(monkeypatch, decision):
    async def _decide(db, *, current_model=None, requested_model=None, account=None):
        return decision
    monkeypatch.setattr(
        "server.sessions.model_policy.resolve_self_directed_model_policy", _decide)


def test_downgrade_applies_with_the_session_identity(tmp_path, monkeypatch):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, model="opus", req=req)
                calls = []

                async def _set(session_id, model, *, when="now", identity="system"):
                    calls.append((session_id, model, when, identity))
                    return False
                orch.set_model_for_session = _set

                await orch._apply_model_request("s1", req)
                return calls, orch._self_model_state["s1"]
            finally:
                await orch.stop()

        calls, state = asyncio.run(_run())

    assert calls == [("s1", "haiku", "after_turn", "session:self")]
    assert state.auto_applied == 1
    assert "opus" in state.tiers_left


def test_guard_cap_forces_a_park_instead_of_applying(tmp_path, monkeypatch):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, model="opus", req=req)
                orch._self_model_state["s1"] = SelfModelState(auto_applied=99)
                parked = []

                async def _park(sid, model, reason):
                    parked.append((sid, model, reason))
                orch._park_self_model_request = _park

                await orch._apply_model_request("s1", req)
                return parked
            finally:
                await orch.stop()

        parked = asyncio.run(_run())

    assert parked == [("s1", "haiku", "trivial")]


def test_copilot_sessions_ignore_the_marker(tmp_path, monkeypatch):
    """Third-party model — a tier request is meaningless there."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            import server.sessions.orchestrator as orch_mod
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, model="opus", req=req)
                monkeypatch.setattr(orch_mod, "_provider_of", lambda acct: "copilot")
                called = []
                orch.set_model_for_session = lambda *a, **k: called.append(a)
                orch._park_self_model_request = lambda *a: called.append(a)

                await orch._apply_model_request("s1", req)
                return called
            finally:
                await orch.stop()

        called = asyncio.run(_run())

    assert called == []


def test_kill_switch_disables_the_feature(tmp_path, monkeypatch):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        class _Cfg:
            self_model_switch_enabled = False
            self_model_switch_cap = 3
        monkeypatch.setattr("server.config.get_settings", lambda: _Cfg())

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, model="opus", req=req)
                called = []
                orch.set_model_for_session = lambda *a, **k: called.append(a)

                await orch._apply_model_request("s1", req)
                return called
            finally:
                await orch.stop()

        called = asyncio.run(_run())

    assert called == []


def test_refused_request_changes_nothing(tmp_path, monkeypatch):
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "refuse")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "fable", "reason": "hard"}
                _attach(orch, model="opus", req=req)
                called = []
                orch.set_model_for_session = lambda *a, **k: called.append(a)
                orch._park_self_model_request = lambda *a: called.append(a)

                await orch._apply_model_request("s1", req)
                return called
            finally:
                await orch.stop()

        called = asyncio.run(_run())

    assert called == []


# ---------------------------------------------------------------------------
# Fix-round-1 findings (Opus mutation review of the first pass)
# ---------------------------------------------------------------------------

def test_persist_status_drives_the_model_request_consumption(tmp_path):
    """C1 (critical): every test above calls ``_maybe_handle_model_request`` /
    ``_apply_model_request`` directly — nothing exercises the ONE line
    (``orchestrator.py``, inside ``_persist_status``) that actually wires this
    feature to a running session's driver ``on_status`` callback. A mutation
    that deletes that line left the whole ``-k model`` suite green. This test
    drives the real production entry point, ``_persist_status``, instead of
    the handler directly.

    ``_persist_status`` legitimately ``_spawn``s several OTHER unrelated
    background tasks for any status (``_persist_sub_agents``, sweep/nudge
    ticks), so stubbing ``_spawn`` itself and counting entries is fragile —
    it over-counts. Instead this stubs ``_apply_model_request`` (the specific
    coroutine the wiring must reach) and awaits an event it sets, proving the
    real, UN-stubbed ``_spawn`` actually scheduled and ran it."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, req=req)
                called = asyncio.Event()

                async def _stub_apply(session_id, r):
                    called.set()
                orch._apply_model_request = _stub_apply

                # "idle" (not a MID_TURN_STATUS) is enough to reach the hook and
                # is simpler than "done" here: "done" additionally runs the
                # parked/waiting-external transcript overlay, which is
                # unrelated to what this test verifies (that _persist_status
                # calls _maybe_handle_model_request at all).
                await orch._persist_status("s1", "idle")
                try:
                    await asyncio.wait_for(called.wait(), timeout=2.0)
                    fired = True
                except asyncio.TimeoutError:
                    fired = False
                return fired
            finally:
                await orch.stop()

        fired = asyncio.run(_run())

    assert fired is True


def test_park_self_model_request_writes_the_real_row(tmp_path, monkeypatch):
    """I1 (important): every other park-adjacent test stubs
    ``_park_self_model_request`` out, so the actual column writes / audit /
    publish it performs were never executed by any test. This one lets it run
    for real against the harness DB and asserts on the persisted row."""
    from sqlalchemy import select
    from server.db import db_session
    from server.db.models import Session as DbSession

    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                sid = "s1"
                async with db_session() as db:
                    db.add(DbSession(id=sid, name="n", account_name="alpha",
                                     engine_session_id="c1", status="idle",
                                     cwd="/tmp", model="opus"))
                _attach(orch, session_id=sid, model="opus", req=None)

                await orch._park_self_model_request(sid, "fable", "needs a human")

                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == sid))).scalar_one()
                return row, orch._records[sid]
            finally:
                await orch.stop()

        row, rec = asyncio.run(_run())

    assert row.pending_model == "fable"
    assert row.pending_model_reason == "needs a human"
    assert row.pending_model_requested_by == "session:self"
    assert row.pending_model_requested_at is not None
    assert rec.pending_model == "fable"
    assert rec.pending_model_reason == "needs a human"
    # I3 (whole-branch finding): _record_to_dict reads the in-memory record, not the
    # DB row, so pendingModelRequestedBy must be set here too or the approval card
    # never renders "the session requested this" until a daemon restart rehydrates it.
    assert rec.pending_model_requested_by == "session:self"


def test_apply_model_request_fails_open_when_a_collaborator_raises(tmp_path, monkeypatch):
    """I2 (important): the existing guard-cap test's park stub used to be a
    SYNC lambda, so ``await <that lambda's return value (None)>`` raised
    TypeError — a bug in the test, not a deliberate probe — and that TypeError
    happened to be what the outer ``except Exception`` swallowed, giving
    false confidence that fail-open was tested. That stub is now async (see
    test_guard_cap_forces_a_park_instead_of_applying). This test purpose-
    builds the fail-open proof: a real collaborator (``set_model_for_session``)
    raises, and no exception may escape ``_apply_model_request``."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")
        _patch_decision(monkeypatch, "apply")

        async def _run():
            orch = await h.make_orch()
            try:
                req = {"turn_id": "t1", "model": "haiku", "reason": "trivial"}
                _attach(orch, model="opus", req=req)

                async def _boom(*a, **k):
                    raise RuntimeError("collaborator exploded")
                orch.set_model_for_session = _boom

                await orch._apply_model_request("s1", req)  # must not raise
                return "no exception escaped"
            finally:
                await orch.stop()

        result = asyncio.run(_run())

    assert result == "no exception escaped"


def test_teardown_clears_self_model_state(tmp_path):
    """Regression for the two new per-session dicts, via the ``close_session``
    teardown site (~orchestrator.py:2035-2036): a mutation that dropped either
    pop there left the whole suite green (nothing else reads these dicts after
    a session ends). Assert directly that both are gone once close_session
    runs. The sibling ``archive_session`` teardown site
    (~orchestrator.py:8917-8918) has its own pop pair and its own regression
    test below — dropping it leaks two dict entries per archived session
    until daemon restart, and this test alone would NOT have caught that."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                sid = "s1"
                _attach(orch, session_id=sid, req={"turn_id": "t1", "model": "haiku"})
                orch._self_model_state[sid] = SelfModelState(auto_applied=1)
                orch._handled_model_requests[sid] = "t1"
                await orch.close_session(sid)
                return (sid in orch._self_model_state,
                        sid in orch._handled_model_requests)
            finally:
                await orch.stop()

        in_state, in_handled = asyncio.run(_run())

    assert in_state is False
    assert in_handled is False


def test_archive_session_teardown_clears_self_model_state(tmp_path):
    """Same regression as ``test_teardown_clears_self_model_state`` above, but
    for the SECOND teardown site inside ``archive_session``
    (~orchestrator.py:8917-8918). ``close_session`` and ``archive_session`` each
    maintain their own independent pop pair for ``_self_model_state`` /
    ``_handled_model_requests`` — dropping the archive-path pops leaves every
    other test in the suite green (nothing else reads these dicts after a
    session is archived), silently leaking two dict entries per archived
    session until the daemon restarts. Assert directly that both are gone
    once ``archive_session`` runs."""
    env, comp, trust, sel, h = _harness(tmp_path)
    with env, comp, trust, sel:
        _seed_account("alpha")

        async def _run():
            orch = await h.make_orch()
            try:
                sid = "s1"
                _attach(orch, session_id=sid, req={"turn_id": "t1", "model": "haiku"})
                orch._self_model_state[sid] = SelfModelState(auto_applied=1)
                orch._handled_model_requests[sid] = "t1"
                await orch.archive_session(sid)
                return (sid in orch._self_model_state,
                        sid in orch._handled_model_requests)
            finally:
                await orch.stop()

        in_state, in_handled = asyncio.run(_run())

    assert in_state is False
    assert in_handled is False

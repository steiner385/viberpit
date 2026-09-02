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
from server.sessions.types import SessionRecord
from server.tests.test_orchestrator_model_gate import _harness, _seed_account


class _FakeDriver:
    """Bare stand-in for a live driver: only the attribute
    ``_maybe_handle_model_request`` reads. A real driver owns a PTY/subprocess
    this unit test has no business spinning up."""

    def __init__(self, req=None):
        self.last_model_request = req


def _attach(orch, *, session_id="s1", model="opus", account="alpha", req=None):
    rec = SessionRecord(
        id=session_id, name="n", account=account, claude_session_id="c1",
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
                orch._park_self_model_request = (
                    lambda sid, model, reason: parked.append((sid, model, reason)))

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

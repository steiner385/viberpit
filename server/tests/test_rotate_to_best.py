"""rotate_to_best — the "Next best account" action and the batch Rotate-stalled button.

A thin caller of _rotate_away's selection + _reassign (the single swap primitive):
picks the next best account via the selector, un-pins, replays the failed prompt for a
stalled session, waits (no replay) for a session parked on a question, and never counts
toward the automatic rotation cap.
"""
import datetime as _dt

import pytest

from viberpit.selector import AllAccountsBusy, Picked
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator, _ROTATE_REPLAY_STATUSES, _REPLAY_STATUSES
from server.sessions.engine import AgentEngine
from server.sessions.model_policy import quota_model
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY


class _Driver:
    def __init__(self): self.identity = SYSTEM_IDENTITY


def _rec(status="needs_attention", **kw):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                cols=80, rows=24, cwd="/tmp", status=status)
    base.update(kw)
    return SessionRecord(**base)


def _orch(monkeypatch, rec, *, pick="b", with_driver=True):
    o = SessionOrchestrator.__new__(SessionOrchestrator)
    o._records = {rec.id: rec}
    o._drivers = {rec.id: _Driver()} if with_driver else {}
    o._clock = lambda: 1000.0
    o.live_session_load = lambda: {}
    o.calls = []
    rotations = []
    o._recovery = type("R", (), {
        "quarantined": lambda self, now: frozenset({"q"}),
        "record_rotation": lambda self, sid, now: rotations.append(sid),
    })()
    o.rotations = rotations

    async def _reassign(record, new, *, reason, replay_last_prompt=True):
        o.calls.append(("reassign", new, reason, replay_last_prompt))
        record.account = new
        if with_driver:
            o._drivers[record.id] = _Driver()
        return "rotated"

    async def _persist_pin(sid, pinned): o.calls.append(("pin", pinned))

    async def _pause_session(record, *, reason, earliest_reset=None):
        o.calls.append(("pause", reason, earliest_reset))

    o._reassign = _reassign
    o._persist_pin = _persist_pin
    o._pause_session = _pause_session
    seen = {}

    def select_account(exclude=frozenset(), load=None, soft_exclude=frozenset(), **k):
        seen["exclude"] = exclude
        seen["soft_exclude"] = soft_exclude
        seen["model"] = k.get("model")
        if isinstance(pick, Exception):
            raise pick
        return Picked(pick)

    monkeypatch.setattr(orch_mod, "select_account", select_account)
    o.seen = seen
    return o


def test_replay_statuses_add_the_two_parked_shapes():
    assert _ROTATE_REPLAY_STATUSES == _REPLAY_STATUSES | {"needs_attention", "limited"}


@pytest.mark.asyncio
async def test_rotate_to_best_selects_off_event_loop(monkeypatch):
    """select_account does blocking oauth usage I/O (#2223) — rotate_to_best must
    run it off the daemon's event loop thread, like every other selection call
    in this class."""
    import threading
    rec = _rec(status="needs_attention", paused_reason="all_accounts_busy")
    o = _orch(monkeypatch, rec)
    loop_thread = threading.get_ident()
    seen_threads = []
    stub = orch_mod.select_account

    def wrapped(*a, **k):
        seen_threads.append(threading.get_ident())
        return stub(*a, **k)

    monkeypatch.setattr(orch_mod, "select_account", wrapped)
    assert await o.rotate_to_best("s1") == "rotated"
    assert seen_threads and seen_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_rotates_stalled_session_replays_and_unpins(monkeypatch):
    # An explicitly tiered model so the expected quota key is deterministic
    # ("sonnet") rather than whatever a host's settings.json resolves "default" to.
    rec = _rec(status="needs_attention", paused_reason="all_accounts_busy",
               account_pinned=True, model="sonnet")
    o = _orch(monkeypatch, rec)
    assert await o.rotate_to_best("s1", "user@x") == "rotated"
    assert ("reassign", "b", "manual_rotate", True) in o.calls
    assert ("pin", False) in o.calls and rec.account_pinned is False
    # The current account is HARD-excluded and the quarantined one only soft-excluded.
    # Merging them let the selection fallback relax the current account back in, and
    # "rotate to best" returned the same account it was asked to move off (2026-09-08).
    assert o.seen["exclude"] == frozenset({"a"})           # current — never relaxed
    assert o.seen["soft_exclude"] == frozenset({"q"})      # quarantined — relaxable
    # The pick is made for the model this session runs: an account that is only
    # out of Fable still serves Sonnet, and must not be skipped for one.
    assert o.seen["model"] == quota_model(rec.model, engine=rec.engine) == "sonnet"
    assert o.rotations == []                                # never counts toward the cap
    assert o._drivers["s1"].identity == "user@x"


@pytest.mark.asyncio
async def test_parked_on_question_resumes_without_replay(monkeypatch):
    o = _orch(monkeypatch, _rec(status="waiting_question"))
    assert await o.rotate_to_best("s1") == "rotated"
    assert ("reassign", "b", "manual_rotate", False) in o.calls


@pytest.mark.asyncio
async def test_mid_turn_session_replays(monkeypatch):
    o = _orch(monkeypatch, _rec(status="in_progress"))
    await o.rotate_to_best("s1")
    assert ("reassign", "b", "manual_rotate", True) in o.calls


@pytest.mark.asyncio
async def test_unknown_and_not_rotatable_skips(monkeypatch):
    o = _orch(monkeypatch, _rec(status="archived"))
    assert await o.rotate_to_best("nope") == "skipped:not-found"
    assert await o.rotate_to_best("s1") == "skipped:archived"
    assert o.calls == []


@pytest.mark.asyncio
async def test_all_busy_on_stalled_session_refreshes_pause(monkeypatch):
    reset = _dt.datetime(2026, 9, 4, 16, 10, tzinfo=_dt.timezone.utc)
    rec = _rec(status="needs_attention", paused_reason="rotation_cap", account_pinned=True)
    o = _orch(monkeypatch, rec, pick=AllAccountsBusy(reset))
    assert await o.rotate_to_best("s1") == "skipped:all_accounts_busy"
    assert ("pause", "all_accounts_busy", reset) in o.calls
    assert ("pin", False) in o.calls                         # the user asked to move
    assert not any(c[0] == "reassign" for c in o.calls)


@pytest.mark.asyncio
async def test_all_busy_on_healthy_session_touches_nothing(monkeypatch):
    rec = _rec(status="idle", account_pinned=True)
    o = _orch(monkeypatch, rec, pick=AllAccountsBusy(None))
    assert await o.rotate_to_best("s1") == "skipped:all_accounts_busy"
    assert o.calls == [] and rec.account_pinned is True


@pytest.mark.asyncio
async def test_no_driver_after_failed_rebuild(monkeypatch):
    o = _orch(monkeypatch, _rec(status="needs_attention"), with_driver=False)
    assert await o.rotate_to_best("s1") == "no_driver"


@pytest.mark.asyncio
async def test_passes_through_a_new_thread_switch_as_success(monkeypatch):
    """account_switch_new_thread (#1275): the session DID move, just without
    thread continuity — the caller (the API route, the batch endpoint) must treat
    this as success, not fall through to the driver-presence check as if nothing
    had happened."""
    rec = _rec(status="needs_attention")
    o = _orch(monkeypatch, rec)

    async def _reassign(record, new, *, reason, replay_last_prompt=True):
        record.account = new
        o._drivers[record.id] = _Driver()
        return "rotated_new_thread"

    o._reassign = _reassign
    assert await o.rotate_to_best("s1") == "rotated_new_thread"


@pytest.mark.asyncio
async def test_refusal_is_never_reported_as_rotated(monkeypatch):
    """_reassign refuses WITHOUT touching the driver when the engine can rotate
    neither in place nor via a fresh thread — the untouched old driver is still
    live, so the pre-existing `driver is None` check alone would misreport this
    refusal as "rotated". The result must short-circuit before that check."""
    rec = _rec(status="needs_attention")
    o = _orch(monkeypatch, rec)  # with_driver=True: the ORIGINAL (untouched) driver

    async def _reassign(record, new, *, reason, replay_last_prompt=True):
        return "unsupported"  # refused; record.account and _drivers untouched

    o._reassign = _reassign
    assert await o.rotate_to_best("s1") == "skipped:unsupported"
    assert rec.account == "a"  # never moved

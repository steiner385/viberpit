"""rotate_to_best — the "Next best account" action and the batch Rotate-stalled button.

A thin caller of _rotate_away's selection + _reassign (the single swap primitive):
picks the next best account via the selector, un-pins, replays the failed prompt for a
stalled session, waits (no replay) for a session parked on a question, and never counts
toward the automatic rotation cap.
"""
import datetime as _dt

import pytest

from ccswitch.selector import AllAccountsBusy, Picked
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator, _ROTATE_REPLAY_STATUSES, _REPLAY_STATUSES
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY


class _Driver:
    def __init__(self): self.identity = SYSTEM_IDENTITY


def _rec(status="needs_attention", **kw):
    base = dict(id="s1", name="n", account="a", claude_session_id="c1",
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

    async def _persist_pin(sid, pinned): o.calls.append(("pin", pinned))

    async def _pause_session(record, *, reason, earliest_reset=None):
        o.calls.append(("pause", reason, earliest_reset))

    o._reassign = _reassign
    o._persist_pin = _persist_pin
    o._pause_session = _pause_session
    seen = {}

    def select_account(exclude=frozenset(), load=None, **k):
        seen["exclude"] = exclude
        if isinstance(pick, Exception):
            raise pick
        return Picked(pick)

    monkeypatch.setattr(orch_mod, "select_account", select_account)
    o.seen = seen
    return o


def test_replay_statuses_add_the_two_parked_shapes():
    assert _ROTATE_REPLAY_STATUSES == _REPLAY_STATUSES | {"needs_attention", "limited"}


@pytest.mark.asyncio
async def test_rotates_stalled_session_replays_and_unpins(monkeypatch):
    rec = _rec(status="needs_attention", paused_reason="all_accounts_busy", account_pinned=True)
    o = _orch(monkeypatch, rec)
    assert await o.rotate_to_best("s1", "user@x") == "rotated"
    assert ("reassign", "b", "manual_rotate", True) in o.calls
    assert ("pin", False) in o.calls and rec.account_pinned is False
    assert o.seen["exclude"] == frozenset({"q", "a"})      # quarantined + current
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

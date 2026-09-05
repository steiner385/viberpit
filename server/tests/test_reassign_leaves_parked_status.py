"""A successful `_reassign` must un-park a session's *status*, not just its pause
columns.

The driver emits no status on `start()`, so nothing moves a parked record
(`limited` / `needs_attention`) off that status once the swap succeeds — a replayed
prompt would, but `_last_prompt` is process-local and empty for exactly the case the
recovery automation cares about (a `limited` session after a daemon restart). Left
parked, `is_usage_stalled` stays true, the stuck-session sweep's `limit_reset` branch
re-fires ≥30 s later and rebuilds the driver again, forever.

These tests drive the REAL `_reassign` (not a spy) with the `_NullCtx` harness from
test_pause_reason_persist / test_reload_session.
"""
from __future__ import annotations

import datetime as _dt
import time

import pytest

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord, SYSTEM_IDENTITY
from server.sessions.usage_stall import is_usage_stalled


class _NullCtx:
    """Stand-in for `db_session()` that records the statements it is handed."""
    def __init__(self, sink): self.sink = sink
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def execute(self, stmt, *a, **k):
        self.sink.append(stmt)
        return None


class _OldDriver:
    cwd = "/tmp"; mode = "interactive"; identity = SYSTEM_IDENTITY; engine_session_id = "c1"
    async def interrupt(self): pass
    async def aclose(self): pass


# NOTE: the harness builders below are deliberately unannotated (like the ones in
# test_pause_reason_persist / test_reload_session). Annotating them makes mypy check
# their bodies, where poking stubs onto a bare `SessionOrchestrator.__new__` instance
# is one `method-assign` error per line — noise, not signal, for a fake harness.
def _new_driver_cls(events):
    class _NewDriver:
        def __init__(self, *a, **k):
            self.cwd = "/tmp"; self.mode = "interactive"; self.identity = k.get("identity")
            self.engine_session_id = "c1"; self.sent = []
            events.append(("driver_built", None))
            _NewDriver.last = self
        async def start(self): pass
        async def set_model(self, m): pass
        async def send_prompt(self, p):
            self.sent.append(p)
            events.append(("send_prompt", p))
    _NewDriver.last = None
    return _NewDriver


def _build(monkeypatch, *, status, paused_reason, last_prompt):
    """Wire a minimal orchestrator around the real `_reassign`.

    Returns (orch, rec, events, driver_cls). `events` is a single ordered log across
    both spies so a test can assert the status persist happened BEFORE the replay.
    """
    events: list = []
    stmts: list = []
    driver_cls = _new_driver_cls(events)
    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver", lambda self, **kw: driver_cls(resume=kw.get("resume"), identity=kw.get("identity")))
    monkeypatch.setattr(orch_mod, "compose", lambda a: "/cfg")
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)
    monkeypatch.setattr(orch_mod, "db_session", lambda: _NullCtx(stmts))

    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", status=status,
                        paused_reason=paused_reason,
                        paused_until=_dt.datetime(2026, 9, 4, 16, 10))
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {"s1": rec}; orch._drivers = {"s1": _OldDriver()}
    orch._last_prompt = {"s1": last_prompt} if last_prompt else {}
    orch._turn_restart_at = {}; orch._swapping = {}
    orch._clock = time.time; orch._registry = None; orch._suggester = None
    orch._session_factory = lambda sid: None
    orch._persist_engine_session_id = lambda *a, **k: None
    orch._handle_recoverable_failure = lambda *a, **k: None
    orch._publish_event = lambda e: None

    async def spy_persist_status(sid, new_status):
        # Mirror the real method's in-memory effect: rec.status moves and the pause
        # columns are cleared for a non-parked status.
        events.append(("persist_status", (sid, new_status)))
        rec.status = new_status
        rec.paused_reason = None
        rec.paused_until = None

    orch._persist_status = spy_persist_status
    return orch, rec, events, driver_cls


def _statuses(events):
    return [payload for kind, payload in events if kind == "persist_status"]


@pytest.mark.asyncio
async def test_limited_without_replay_prompt_persists_idle(monkeypatch):
    """The loop-breaker: a `limited` session with nothing to replay (the normal
    state after a daemon restart) still leaves the parked status."""
    orch, rec, events, driver_cls = _build(
        monkeypatch, status="limited", paused_reason="pinned_account_limited",
        last_prompt=None)

    await orch._reassign(rec, "b", reason="limit_reset")

    assert _statuses(events) == [("s1", "idle")]
    assert rec.status == "idle"
    assert rec.account == "b"
    assert is_usage_stalled(rec.status, rec.paused_reason) is False
    assert driver_cls.last.sent == []


@pytest.mark.asyncio
async def test_needs_attention_persists_idle_before_the_replay(monkeypatch):
    """Order matters: idle must land BEFORE send_prompt so the replayed turn's own
    in_progress status wins instead of being stomped back to idle."""
    orch, rec, events, driver_cls = _build(
        monkeypatch, status="needs_attention", paused_reason="all_accounts_busy",
        last_prompt="go")

    await orch._reassign(rec, "b", reason="limit_reset")

    assert _statuses(events) == [("s1", "idle")]
    kinds = [k for k, _ in events]
    assert kinds.index("persist_status") < kinds.index("send_prompt")
    assert driver_cls.last.sent == ["go"]
    assert is_usage_stalled(rec.status, rec.paused_reason) is False


@pytest.mark.asyncio
async def test_unparked_status_is_left_alone(monkeypatch):
    """A session that is not parked owns its own status — the swap must not touch it."""
    orch, rec, events, _ = _build(
        monkeypatch, status="waiting_question", paused_reason=None, last_prompt=None)

    await orch._reassign(rec, "b", reason="manual_rotate")

    assert _statuses(events) == []
    assert rec.status == "waiting_question"


@pytest.mark.asyncio
async def test_limit_reset_reassign_stops_the_recovery_sweep_reentering(monkeypatch):
    """Regression for the driver-rebuild loop: after a `limit_reset` reassign the
    record no longer satisfies `is_usage_stalled`, so `_maybe_sweep_on_recovery`
    finds no stalled session and returns False on the next poller event."""
    orch, rec, events, _ = _build(
        monkeypatch, status="limited", paused_reason="pinned_account_limited",
        last_prompt=None)
    assert is_usage_stalled(rec.status, rec.paused_reason) is True

    await orch._reassign(rec, "b", reason="limit_reset")

    orch._clock = lambda: 1e9  # past the debounce window, so only staleness can block
    orch._last_recovery_sweep_at = 0.0
    assert orch._maybe_sweep_on_recovery() is False

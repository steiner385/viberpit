"""A reassign refused by another host's session lease backs off; it does not retry.

Incident (2026-09-08 → 2026-09-16, rocky): session ``331f7d98`` was stamped for rocky
while dobby's deploy snapshot held its lease (#1824). Rocky's rotation monitor saw a
live session with no driver, called ``_restart_driverless`` → ``_reassign_locked``, and
``_claim_session`` refused at the lease: ``session.lease_denied {"holder": "dobby",
"reason": "relaunch (weekly limit reached)"}``. The refusal raised ``SessionLeaseHeld``,
which the failure classifier treated as a generic fault: ``log.exception
("reassign.error")`` plus a park under ``reassign_failed:<trigger>`` — the very reason
the stuck-session sweep exists to retry. Every tick re-entered, was refused identically,
and logged another warning: 199 occurrences on one fingerprint (#1778, a regression of
#1738 by volume rather than by cause).

This is the #1767 shape (``automation_ownership_stale``) with one difference that
decides the fix: revoked automation ownership is settled forever, so it is terminal,
whereas a lease LAPSES when its holder stops heartbeating. Parking ``lease_held`` as
terminal would strand a session whose owner died. So the pause reason is retryable and
the two automatic retry loops gate on the live condition instead — the same freshness
rule ``session_lease.acquire`` itself refuses on.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.paused = []

    async def spy_pause(record, reason, earliest_reset=None):
        o.paused.append((record.id, reason))

    o._pause_session = spy_pause
    o._copilot = None
    o._suggester = None
    o._session_factory = lambda sid: lambda *a, **k: None
    o._install_driver = lambda sid, drv: o._drivers.__setitem__(sid, drv)
    return o


def _rec(o, tmp_path, **kw):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd=str(tmp_path))
    for k, v in kw.items():
        setattr(rec, k, v)
    o._records[rec.id] = rec
    return rec


def _stub_env(monkeypatch):
    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)


async def _run_refused_by_lease(o, rec, monkeypatch, tmp_path):
    """Drive a driverless _reassign whose lease claim is refused."""
    async def refuse(session_id, *, why, adopt=False):
        return False

    o._claim_session = refuse
    o._publish_event = lambda e: None
    _stub_env(monkeypatch)
    return await o._reassign(rec, "b", reason="weekly limit reached")


class _Lease:
    """Enough of ``session_lease.LeaseState`` for the backoff predicate."""

    def __init__(self, holder: str, age_s: float):
        self.holder = holder
        self.fence = 1
        self.age_s = age_s


def _lease_row(monkeypatch, state):
    async def current(session_id):
        return state

    monkeypatch.setattr(orch_mod.session_lease, "current", current)


# ---------------------------------------------------------------------------
# Classification: a refusal is a handled outcome, not a fault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_refusal_parks_as_lease_held(monkeypatch, tmp_path):
    """``reassign_failed:<trigger>`` is what fed the sweep; record the real cause."""
    o = _make_orch()
    rec = _rec(o, tmp_path)

    outcome = await _run_refused_by_lease(o, rec, monkeypatch, tmp_path)

    assert outcome == "failed"
    assert o.paused == [("s1", orch_mod._LEASE_HELD_PAUSE_REASON)], (
        f"expected a lease-held pause reason, got {o.paused}")


@pytest.mark.asyncio
async def test_lease_held_is_retryable_not_terminal(monkeypatch, tmp_path):
    """A lease lapses when its holder dies, so this must NOT be terminal — otherwise
    a session whose owning daemon is gone waits for a human forever."""
    assert orch_mod._LEASE_HELD_PAUSE_REASON not in orch_mod._TERMINAL_PAUSE_REASONS


@pytest.mark.asyncio
async def test_lease_refusal_is_not_logged_as_an_error(monkeypatch, tmp_path, caplog):
    """`log.exception("reassign.error")` per refusal is a second auto-filed issue on
    top of the lease_denied one, for an outcome the fence produced by design."""
    o = _make_orch()
    rec = _rec(o, tmp_path)

    with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
        await _run_refused_by_lease(o, rec, monkeypatch, tmp_path)

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [], f"handled refusal logged as ERROR: {[r.message for r in errors]}"
    assert any("reassign.lease_held" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_other_start_failures_stay_retryable(monkeypatch, tmp_path):
    """Guard against widening this into 'never retry a failed reassign'."""
    o = _make_orch()
    rec = _rec(o, tmp_path)

    class _OldDriver:
        engine_session_id = "old-csid"
        cwd = str(tmp_path)
        mode = "interactive"
        identity = "user@example.com"

        async def interrupt(self): pass
        async def aclose(self): pass

    o._drivers[rec.id] = _OldDriver()

    class _NewDriver:
        engine_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw): pass
        async def start(self): raise RuntimeError("CLI exited 1")
        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_create_driver",
                        lambda self, **kw: _NewDriver())

    async def allow(session_id, *, why, adopt=False):
        return True

    o._claim_session = allow
    o._publish_event = lambda e: None
    _stub_env(monkeypatch)

    assert await o._reassign(rec, "b", reason="weekly limit reached") == "failed"
    assert o.paused == [("s1", "reassign_failed:weekly limit reached")]


# ---------------------------------------------------------------------------
# The backoff predicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [
    (_Lease("dobby", 5.0), True),                                # live foreign holder
    (_Lease("dobby", orch_mod.SESSION_LEASE_TTL_S + 1), False),  # lapsed → takeable
    (_Lease("rocky", 5.0), False),                               # ours
    (None, False),                                               # never held
])
async def test_lease_held_elsewhere_matches_the_acquire_rule(
        monkeypatch, state, expected):
    """The predicate must refuse on exactly what ``session_lease.acquire`` refuses on;
    a stricter one strands the session, a looser one keeps the loop spinning."""
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    _lease_row(monkeypatch, state)
    o = _make_orch()
    assert await o._lease_held_elsewhere("s1") is expected


@pytest.mark.asyncio
async def test_lease_held_elsewhere_is_false_on_a_db_error(monkeypatch):
    """A DB blip must not silently park every session: fall back to the old
    behaviour (attempt, be refused), which is what _claim_session does too."""
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")

    async def boom(session_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(orch_mod.session_lease, "current", boom)
    o = _make_orch()
    assert await o._lease_held_elsewhere("s1") is False


# ---------------------------------------------------------------------------
# The two automatic retry loops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_skips_a_lease_held_session_while_the_holder_is_live(
        monkeypatch, tmp_path):
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    _lease_row(monkeypatch, _Lease("dobby", 5.0))
    o = _make_orch()
    rec = _rec(o, tmp_path, status="needs_attention",
               paused_reason=orch_mod._LEASE_HELD_PAUSE_REASON, host="rocky")
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate
    o._enabled_host_names = _hosts("dobby", "rocky")

    await o._sweep_needs_attention()
    await _drain(o)

    assert rotated == [], f"sweep retried a refusal that cannot succeed: {rotated}"


@pytest.mark.asyncio
async def test_sweep_retries_once_the_lease_has_lapsed(monkeypatch, tmp_path):
    """The holder died: this host may legitimately take the session back."""
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    _lease_row(monkeypatch, _Lease("dobby", orch_mod.SESSION_LEASE_TTL_S + 1))
    o = _make_orch()
    rec = _rec(o, tmp_path, status="needs_attention",
               paused_reason=orch_mod._LEASE_HELD_PAUSE_REASON, host="rocky")
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate
    o._enabled_host_names = _hosts("dobby", "rocky")

    await o._sweep_needs_attention()
    await _drain(o)

    assert rotated == [("s1", "stuck_session_sweep")]


@pytest.mark.asyncio
async def test_rotation_monitor_does_not_rebuild_while_the_lease_is_held(
        monkeypatch, tmp_path):
    """The driverless branch selects on account usage alone, so it is the second
    unbounded loop — the one that logged ``relaunch (weekly limit reached)``."""
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    _lease_row(monkeypatch, _Lease("dobby", 5.0))
    o = _make_orch()
    rec = _rec(o, tmp_path, status="needs_attention",
               paused_reason=orch_mod._LEASE_HELD_PAUSE_REASON)
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate

    await o._restart_driverless(rec, reason="weekly limit reached")

    assert rotated == [], f"rebuilt a session another host holds: {rotated}"


@pytest.mark.asyncio
async def test_rotation_monitor_rebuilds_once_the_lease_has_lapsed(
        monkeypatch, tmp_path):
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    _lease_row(monkeypatch, _Lease("dobby", orch_mod.SESSION_LEASE_TTL_S + 1))
    o = _make_orch()
    rec = _rec(o, tmp_path, status="needs_attention",
               paused_reason=orch_mod._LEASE_HELD_PAUSE_REASON)
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate

    await o._restart_driverless(rec, reason="weekly limit reached")

    assert rotated == [("s1", "weekly limit reached")]


@pytest.mark.asyncio
async def test_an_ordinarily_parked_session_never_reads_the_lease(
        monkeypatch, tmp_path):
    """The backoff is gated on the pause reason, so the common path adds no DB read."""
    monkeypatch.setattr(orch_mod, "self_host_name", lambda: "rocky")
    reads = []

    async def current(session_id):
        reads.append(session_id)
        return _Lease("dobby", 5.0)

    monkeypatch.setattr(orch_mod.session_lease, "current", current)
    o = _make_orch()
    rec = _rec(o, tmp_path, status="needs_attention", paused_reason="all_accounts_busy")
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate

    await o._restart_driverless(rec, reason="weekly limit reached")

    assert rotated == [("s1", "weekly limit reached")]
    assert reads == []


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hosts(*names):
    async def enabled():
        return frozenset(names)

    return enabled


async def _drain(o):
    """Let the tasks the sweep spawned run to completion."""
    import asyncio
    pending = [t for t in getattr(o, "_bg_tasks", set()) if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    else:
        await asyncio.sleep(0)

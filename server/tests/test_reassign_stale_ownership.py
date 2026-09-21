"""A reassign that fails on revoked automation ownership is TERMINAL, not retryable.

Incident (2026-09-15, dobby): PR Doctor session a728d538 was dispatched to resolve
PR #1763's merge conflicts. The PR merged fifteen minutes later, which revoked the
automation operation's ownership of that PR. Every driver build after that died at
the automation execution fence with ``StaleOwnership("operation no longer owns its
targets")``; ``_reassign_locked`` recorded it as ``reassign_failed:<trigger>``; and
the stuck-session sweep — which exists to retry exactly that pause — rebuilt the
driver on the next tick. 83 identical failures in under six hours (#1767), plus 14
more from the ``ensure_driver`` resume path (#1776).

The fence is right and the retry is wrong: the operation genuinely no longer owns
its targets, and no account freeing up or time passing gives them back. The defect
is that a terminal refusal was recorded under a reason the sweep treats as
retryable, so it re-parked the session on the identical check forever.

``_TERMINAL_PAUSE_REASONS`` is the single list of pause reasons no retry can clear;
``engine_rotation_unsupported`` (#1275) was already one of them.
"""
from pathlib import Path

import pytest

from server.automations.ownership import StaleOwnership
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


def _stub_env(monkeypatch):
    class _MockDB:
        async def execute(self, *a): pass

    class _FakeDbCtx:
        async def __aenter__(self): return _MockDB()
        async def __aexit__(self, *a): pass

    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDbCtx())
    monkeypatch.setattr(orch_mod, "compose", lambda acct: Path("/fake/cfg"))
    monkeypatch.setattr(orch_mod.configdir, "trust_project", lambda *a, **k: None)


def _old_driver(tmp_path):
    class _OldDriver:
        engine_session_id = "old-csid"
        cwd = str(tmp_path)
        mode = "interactive"
        identity = "user@example.com"

        async def interrupt(self): pass
        async def aclose(self): pass

    return _OldDriver()


async def _run_with_start_error(o, rec, monkeypatch, tmp_path, exc):
    """Drive a full _reassign whose NEW driver fails to start with *exc*."""
    o._drivers[rec.id] = _old_driver(tmp_path)

    class _NewDriver:
        engine_session_id = None
        mode = "interactive"

        def __init__(self, *a, **kw): pass
        async def start(self): raise exc
        async def interrupt(self): pass
        async def aclose(self): pass
        async def set_model(self, m): pass

    monkeypatch.setattr(
        orch_mod.SessionOrchestrator,
        "_create_driver",
        lambda self, **kw: _NewDriver(),
    )
    o._publish_event = lambda e: None
    _stub_env(monkeypatch)
    return await o._reassign(rec, "b", reason="stuck_session_sweep")


def _rec(o, tmp_path):
    rec = SessionRecord(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                        engine_session_id="c1", cols=80, rows=24, cwd=str(tmp_path))
    o._records[rec.id] = rec
    return rec


@pytest.mark.asyncio
async def test_stale_ownership_start_failure_parks_terminally(monkeypatch, tmp_path):
    """Revoked ownership must park under a reason the sweep will not retry.

    Recording it as ``reassign_failed:stuck_session_sweep`` is what produced the
    83-retry storm: that reason is precisely what the sweep re-enters on.
    """
    o = _make_orch()
    rec = _rec(o, tmp_path)

    outcome = await _run_with_start_error(
        o, rec, monkeypatch, tmp_path,
        StaleOwnership("operation no longer owns its targets"))

    assert outcome == "failed"
    assert o.paused == [("s1", "automation_ownership_stale")], (
        f"expected a terminal pause reason, got {o.paused}")
    assert o.paused[0][1] in orch_mod._TERMINAL_PAUSE_REASONS


@pytest.mark.asyncio
async def test_stale_ownership_is_not_logged_as_an_error(monkeypatch, tmp_path, caplog):
    """A handled terminal refusal must not log ERROR-with-traceback.

    `log.exception("reassign.error ...")` on this path is what the error-triage bot
    turned into #1767 (83 occurrences). Closing the retry loops cuts the volume, but
    an ERROR per genuine ownership loss would keep re-filing it — and it is not a
    fault: the fence refused exactly as designed.
    """
    o = _make_orch()
    rec = _rec(o, tmp_path)

    with caplog.at_level("WARNING", logger="server.sessions.orchestrator"):
        await _run_with_start_error(
            o, rec, monkeypatch, tmp_path,
            StaleOwnership("operation no longer owns its targets"))

    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [], f"terminal refusal logged as ERROR: {[r.message for r in errors]}"
    assert any("reassign.ownership_stale" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_genuine_reassign_failures_still_log_an_error(monkeypatch, tmp_path, caplog):
    """The quieter log must be specific to StaleOwnership — a real build failure is
    still an ERROR with its traceback, so error triage keeps surfacing it."""
    o = _make_orch()
    rec = _rec(o, tmp_path)

    with caplog.at_level("WARNING", logger="server.sessions.orchestrator"):
        await _run_with_start_error(
            o, rec, monkeypatch, tmp_path, RuntimeError("CLI exited 1"))

    assert any(r.levelname == "ERROR" and "reassign.error" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_rotation_monitor_does_not_rebuild_a_terminally_parked_session(tmp_path):
    """The rotation monitor is the SECOND unbounded rebuild loop, and the one that
    actually drove the storm.

    It selects sessions by account usage, not by paused_reason, so a driverless
    session parked on a terminal reason was re-picked every time its account crossed
    the ceiling — visible in dobby's journal as repeated `rotation.driverless` /
    `rotation.restart_driverless` for a728d538 long after the sweep had given up on
    it. Fixing only the sweep would have left this loop spinning.
    """
    o = _make_orch()
    rec = _rec(o, tmp_path)
    rec.status = "needs_attention"
    rec.paused_reason = "automation_ownership_stale"
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate

    await o._restart_driverless(rec, reason="weekly limit reached")

    assert rotated == [], f"terminally parked session was rebuilt anyway: {rotated}"


@pytest.mark.asyncio
async def test_rotation_monitor_still_rebuilds_an_ordinarily_parked_session(tmp_path):
    """Guard: a driverless session whose driver merely died still gets restarted.

    Restarting those is the entire reason the driverless branch exists.
    """
    o = _make_orch()
    rec = _rec(o, tmp_path)
    rec.status = "needs_attention"
    rec.paused_reason = "all_accounts_busy"
    rotated = []

    async def spy_rotate(record, reason):
        rotated.append((record.id, reason))

    o._rotate_away = spy_rotate

    await o._restart_driverless(rec, reason="weekly limit reached")

    assert rotated == [("s1", "weekly limit reached")]


@pytest.mark.asyncio
async def test_other_start_failures_stay_retryable(monkeypatch, tmp_path):
    """The terminal classification must be specific to StaleOwnership.

    Every other build failure (a wedged CLI, a torn config read, an account with no
    headroom) IS worth retrying — that is the whole point of the sweep. Guards
    against widening the fix into "never retry a failed reassign".
    """
    o = _make_orch()
    rec = _rec(o, tmp_path)

    outcome = await _run_with_start_error(
        o, rec, monkeypatch, tmp_path, RuntimeError("CLI exited 1"))

    assert outcome == "failed"
    assert o.paused == [("s1", orch_mod._REASSIGN_FAILED_PAUSE_REASON)]
    assert o.paused[0][1] not in orch_mod._TERMINAL_PAUSE_REASONS

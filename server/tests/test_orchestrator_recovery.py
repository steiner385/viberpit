"""Failure-driven recovery dispatch in SessionOrchestrator.

Exercises the decision logic of _recover_session / _rotate_away with the
side-effecting helpers spied, plus the real RecoveryState (quarantine + cap).
"""
import asyncio

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.selection import AllAccountsBusy
from server.sessions.types import SessionRecord
from server.sessions import recovery as R


def _make_orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_replay(record, retry_after):
        o.calls.append(("replay", record.id, retry_after))

    async def spy_rotate(record, reason):
        o.calls.append(("rotate", record.id, reason))

    async def spy_recompose(record):
        o.calls.append(("recompose", record.id))

    async def spy_pause(record, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason))

    def spy_reauth(account):
        o.calls.append(("needs_reauth", account))

    o._replay_same_account = spy_replay
    o._rotate_away = spy_rotate
    o._recompose_same_account = spy_recompose
    o._pause_session = spy_pause
    o._mark_needs_reauth = spy_reauth
    return o


def _rec(o, account="kindash"):
    rec = SessionRecord(id="s1", name="n", account=account, claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x")
    o._records["s1"] = rec
    o._drivers["s1"] = object()
    o._last_prompt["s1"] = "do the thing"
    return rec


def test_429_with_short_retry_after_replays_same_account():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "rate_limit", 5.0))
    assert o.calls == [("replay", "s1", 5.0)]


def test_429_with_long_retry_after_quarantines_and_rotates():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "rate_limit", 600.0))
    assert ("rotate", "s1", "rate_limit") in o.calls
    # the failed account is quarantined for the (capped) retry-after window
    assert "kindash" in o._recovery.quarantined(now=1000.0)


def test_429_with_no_retry_after_quarantines_default_and_rotates():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "rate_limit", None))
    assert ("rotate", "s1", "rate_limit") in o.calls
    assert "kindash" in o._recovery.quarantined(now=1000.0 + R.QUARANTINE_429 - 1)
    assert "kindash" not in o._recovery.quarantined(now=1000.0 + R.QUARANTINE_429 + 1)


def test_401_first_recomposes_same_account():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "auth_401", None))
    assert o.calls == [("recompose", "s1")]
    # no quarantine, no needs-reauth on the first 401 (store creds usually fine)
    assert o._recovery.quarantined(now=1000.0) == frozenset()


def test_401_second_within_ttl_marks_reauth_and_rotates():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "auth_401", None))   # first → recompose
    asyncio.run(o._recover_session(rec, "auth_401", None))   # second → escalate
    assert ("needs_reauth", "kindash") in o.calls
    assert ("rotate", "s1", "auth_401") in o.calls
    assert "kindash" in o._recovery.quarantined(now=1000.0)


def test_rotation_cap_routes_to_pause():
    o = _make_orch(); rec = _rec(o)
    for t in range(R.MAX_ROTATIONS):
        o._recovery.record_rotation("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "rate_limit", None))
    assert ("pause", "s1", "rotation_cap") in o.calls


def test_rotate_away_pauses_when_all_accounts_busy(monkeypatch):
    o = _make_orch(); rec = _rec(o)
    # un-spy the two methods under test; spy only the leaf effects
    o._rotate_away = SessionOrchestrator._rotate_away.__get__(o)
    reassigned = []
    async def spy_reassign(record, new_account, **kw):
        reassigned.append((record.id, new_account))
    o._reassign = spy_reassign
    from datetime import datetime, timezone
    reset = datetime(2026, 6, 23, tzinfo=timezone.utc)
    def boom(exclude=frozenset()):
        raise AllAccountsBusy(reset)
    monkeypatch.setattr(orch_mod, "select_account", boom)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert reassigned == []
    assert ("pause", "s1", "all_accounts_busy") in o.calls


def test_mark_needs_reauth_sets_ledger_status(tmp_path, monkeypatch):
    # Observability fix: a recompose-failed in-session 401 must flip the ledger off
    # 'ok' so the dashboard stops showing a falsely-healthy account.
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path))
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._mark_needs_reauth("kindash")
    from ccswitch import ledger
    with ledger.locked_ledger() as led:
        assert led["accounts"]["kindash"]["status"] == "needs-reauth"


def test_rotate_away_excludes_quarantined_and_reassigns(monkeypatch):
    o = _make_orch(); rec = _rec(o)
    o._rotate_away = SessionOrchestrator._rotate_away.__get__(o)
    reassigned = []
    async def spy_reassign(record, new_account, **kw):
        reassigned.append((record.id, new_account))
    o._reassign = spy_reassign
    o._recovery.quarantine("secondary", until=2000.0)
    seen_exclude = {}
    def pick(exclude=frozenset()):
        seen_exclude["v"] = set(exclude)
        return "primary"
    monkeypatch.setattr(orch_mod, "select_account", pick)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert reassigned == [("s1", "primary")]
    # excludes both the quarantined account and the current account
    assert {"kindash", "secondary"} <= seen_exclude["v"]

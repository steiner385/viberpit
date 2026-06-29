"""Failure-driven recovery dispatch in SessionOrchestrator.

Exercises the decision logic of _recover_session / _rotate_away with the
side-effecting helpers spied, plus the real RecoveryState (quarantine + cap).
"""
import asyncio

from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.selection import AllAccountsBusy, Picked
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
    def boom(exclude=frozenset(), load=None):
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
    def pick(exclude=frozenset(), load=None):
        seen_exclude["v"] = set(exclude)
        return Picked("primary")
    monkeypatch.setattr(orch_mod, "select_account", pick)
    asyncio.run(o._rotate_away(rec, reason="rate_limit"))
    assert reassigned == [("s1", "primary")]
    # excludes both the quarantined account and the current account
    assert {"kindash", "secondary"} <= seen_exclude["v"]


def test_process_crash_reassigns_same_account():
    o = _make_orch(); rec = _rec(o)
    async def spy_reassign(record, new_account, *, reason):
        o.calls.append(("reassign", record.id, new_account, reason))
    o._reassign = spy_reassign
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("reassign", "s1", "kindash", "process_crash") in o.calls


def test_process_crash_guard_pauses_after_cap():
    o = _make_orch(); rec = _rec(o)
    async def spy_reassign(record, new_account, *, reason):
        o.calls.append(("reassign", record.id, new_account, reason))
    o._reassign = spy_reassign
    for _ in range(R.CRASH_CAP):
        o._recovery.record_crash("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("pause", "s1", "crash_cap") in o.calls
    assert ("reassign", "s1", "kindash", "process_crash") not in o.calls


def test_check_stalls_interrupts_wedged_turn():
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        async def interrupt(self):
            interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    asyncio.run(o._check_stalls(now=10_000.0))
    assert interrupted == [True]


def test_check_stalls_escalates_zombie_to_recompose_after_cap():
    """A turn the watchdog already interrupted but that stayed wedged (the SDK ignored
    the interrupt: _turn_active flipped False yet the drain never completed and progress
    is still frozen) must be recomposed in place once interrupts hit the stall cap —
    not silently abandoned."""
    o = _make_orch(); _rec(o)
    reassigned = []
    async def spy_reassign(record, new_account, *, reason):
        reassigned.append((record.id, new_account, reason))
    o._reassign = spy_reassign
    class _Zombie:
        _turn_active = False        # interrupt() already flipped it
        drain_alive = True          # but the drain never completed → subprocess wedged
        last_progress_at = 0.0      # progress frozen
        async def interrupt(self):
            raise AssertionError("must escalate, not interrupt again")
    o._drivers["s1"] = _Zombie()
    for _ in range(R.STALL_CAP):    # interrupts already exhausted
        o._recovery.record_stall("s1", now=10_000.0)
    asyncio.run(o._check_stalls(now=10_000.0))
    assert reassigned == [("s1", "kindash", "stall_recompose")]


def test_check_stalls_zombie_pauses_after_crash_cap():
    """A turn that keeps wedging through repeated recomposes is bounded by crash_cap —
    it ends at needs_attention (paused), not an endless recompose loop."""
    o = _make_orch(); _rec(o)
    async def spy_reassign(record, new_account, *, reason):
        o.calls.append(("reassign", record.id, reason))
    o._reassign = spy_reassign
    class _Zombie:
        _turn_active = False
        drain_alive = True
        last_progress_at = 0.0
        async def interrupt(self): pass
    o._drivers["s1"] = _Zombie()
    for _ in range(R.STALL_CAP):
        o._recovery.record_stall("s1", now=10_000.0)
    for _ in range(R.CRASH_CAP):
        o._recovery.record_crash("s1", now=10_000.0)
    asyncio.run(o._check_stalls(now=10_000.0))
    assert ("pause", "s1", "stall_cap") in o.calls
    assert ("reassign", "s1", "stall_recompose") not in o.calls


def test_check_stalls_leaves_resolved_turn_alone():
    """A turn whose interrupt DID land — drain completed (drain_alive False), turn
    inactive — is genuinely finished; the watchdog must not recompose or interrupt it,
    even with the stall cap already reached."""
    o = _make_orch(); _rec(o)
    reassigned = []
    async def spy_reassign(record, new_account, *, reason):
        reassigned.append(record.id)
    o._reassign = spy_reassign
    class _Done:
        _turn_active = False
        drain_alive = False
        last_progress_at = 0.0
        async def interrupt(self):
            raise AssertionError("resolved turn must be left alone")
    o._drivers["s1"] = _Done()
    for _ in range(R.STALL_CAP):
        o._recovery.record_stall("s1", now=10_000.0)
    asyncio.run(o._check_stalls(now=10_000.0))
    assert reassigned == []


def test_check_stalls_leaves_progressing_turn_alone():
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 9_990.0
        async def interrupt(self):
            interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    asyncio.run(o._check_stalls(now=10_000.0))
    assert interrupted == []


def test_check_stalls_leaves_turn_blocked_on_user_alone():
    """A turn blocked awaiting user input (permission/question/plan) is not wedged —
    last_progress_at is frozen only because the human hasn't answered yet."""
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        awaiting_user_input = True
        async def interrupt(self):
            interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    asyncio.run(o._check_stalls(now=10_000.0))
    assert interrupted == []


def test_check_stalls_stops_after_stall_cap():
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        async def interrupt(self): interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    for _ in range(3):
        o._recovery.record_stall("s1", now=10_000.0)
    asyncio.run(o._check_stalls(now=10_000.0))
    assert interrupted == []   # stall cap reached → no further interrupt


def test_stall_does_not_consume_crash_respawn_budget():
    o = _make_orch(); rec = _rec(o)
    async def spy_reassign(record, new_account, *, reason):
        o.calls.append(("reassign", record.id, new_account, reason))
    o._reassign = spy_reassign
    for _ in range(3):
        o._recovery.record_stall("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("reassign", "s1", "kindash", "process_crash") in o.calls


def test_check_stalls_extends_timeout_for_throttled_turn():
    """A turn whose SDK stream is silent because the API is throttling it (driver
    reports in_api_backoff) is alive, not wedged — the watchdog must give it the
    extended throttle timeout, not interrupt at the base STALL_TIMEOUT. Issue #185."""
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        in_api_backoff = True
        async def interrupt(self): interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    # Silence just past the base timeout but within the extended throttle window.
    asyncio.run(o._check_stalls(now=R.STALL_TIMEOUT + 1.0))
    assert interrupted == []


def test_check_stalls_interrupts_throttled_turn_past_extended_timeout():
    """Throttling buys grace, not immunity: once silence exceeds even the extended
    throttle timeout, a genuinely-hung turn is still interrupted."""
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        in_api_backoff = True
        async def interrupt(self): interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    asyncio.run(o._check_stalls(now=R.STALL_TIMEOUT_THROTTLED + 1.0))
    assert interrupted == [True]


def test_check_stalls_extends_timeout_for_quarantined_account():
    """A turn on an account in recovery quarantine (recently 429'd) is presumed to be
    waiting on the throttled account, not wedged — extend the timeout even if the
    driver itself hasn't flipped in_api_backoff yet."""
    o = _make_orch()
    interrupted = []
    class _StubDriver:
        _turn_active = True
        last_progress_at = 0.0
        account = "kindash"
        async def interrupt(self): interrupted.append(True)
    o._drivers["s1"] = _StubDriver()
    now = R.STALL_TIMEOUT + 1.0
    o._recovery.quarantine("kindash", until=now + R.STALL_TIMEOUT)  # still in cooldown at `now`
    asyncio.run(o._check_stalls(now=now))
    assert interrupted == []


def test_turn_dropped_replays_same_account_immediately():
    o = _make_orch(); rec = _rec(o)
    asyncio.run(o._recover_session(rec, "turn_dropped", None))
    assert o.calls == [("replay", "s1", 0.0)]
    # not an account fault → nothing quarantined
    assert o._recovery.quarantined(now=1000.0) == frozenset()


def test_turn_dropped_stops_after_resend_cap():
    o = _make_orch(); rec = _rec(o)
    for _ in range(R.RESEND_CAP):
        o._recovery.record_resend("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "turn_dropped", None))
    assert o.calls == []  # cap reached → no resend, no pause/rotate


def test_recompose_forces_token_refresh_before_reassign(monkeypatch):
    from unittest import mock
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    order = []

    async def spy_reassign(record, new_account, *, reason):
        order.append(("reassign", new_account, reason))
    o._reassign = spy_reassign

    rec = SessionRecord(id="s1", name="n", account="kindash", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x")
    o._records["s1"] = rec
    o._drivers["s1"] = object()

    ca = mock.MagicMock()
    ca.ensure_valid_token.side_effect = lambda name: order.append(("refresh", name))
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)

    asyncio.run(o._recompose_same_account(rec))

    assert order == [("refresh", "kindash"), ("reassign", "kindash", "recompose")], order


def test_recompose_proceeds_when_token_refresh_raises(monkeypatch):
    """Best-effort contract: token refresh failure must NOT abort recovery.

    When ccswitch_accounts.ensure_valid_token raises (e.g., corrupt grant),
    _recompose_same_account must swallow the exception and proceed to
    _reassign anyway, degrading to today's behavior rather than aborting
    the recovery action. This guards the safety property that the session
    can still be reassigned even if the token can't be refreshed.
    """
    from unittest import mock
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    order = []

    async def spy_reassign(record, new_account, *, reason):
        order.append(("reassign", new_account, reason))
    o._reassign = spy_reassign

    rec = SessionRecord(id="s1", name="n", account="kindash", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x")
    o._records["s1"] = rec
    o._drivers["s1"] = object()

    ca = mock.MagicMock()
    ca.ensure_valid_token.side_effect = RuntimeError("boom")
    monkeypatch.setattr(orch_mod, "ccswitch_accounts", ca)

    # Must not raise despite token refresh failure.
    asyncio.run(o._recompose_same_account(rec))

    # _reassign must still run with the expected arguments.
    assert ("reassign", "kindash", "recompose") in order, order

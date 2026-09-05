"""Self-healing rotation when a session's account becomes dead/unreachable.

Covers two surfaces:
1. _rotation_monitor: when usage=None and consecutive failures ≥ threshold,
   rotate live sessions away (proactive path, fires even before the user touches them).
2. _recover_session process_crash: when the crash happens on a dead account,
   quarantine + rotate instead of retrying the same broken account (reactive path).

The Copilot proxy going down is the canonical trigger: the poller can no longer
fetch usage (returns None / fail_count rises), and driver subprocesses start
crashing when they try to connect.
"""
import asyncio

import pytest

from server.sessions import orchestrator as orch_mod
from server.sessions import recovery as R
from server.sessions.orchestrator import SessionOrchestrator, _ROTATION_CEILING
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.usage.poller import UsagePoller
from ccswitch.oauth import Usage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch():
    """Minimal orchestrator with spied side-effecting helpers."""
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_rotate(record, reason):
        o.calls.append(("rotate", record.id, reason))

    async def spy_pause(record, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason))

    async def spy_reassign(record, new_account, *, reason):
        o.calls.append(("reassign", record.id, new_account, reason))

    async def spy_restart(record, *, reason):
        o.calls.append(("restart_driverless", record.id, reason))

    o._rotate_away = spy_rotate
    o._pause_session = spy_pause
    o._reassign = spy_reassign
    o._restart_driverless = spy_restart
    o._session_uses_setup_token = lambda rec: False
    # Default: not opted into paid extra usage. Stubbed rather than left to the real
    # ledger so these tests never touch the developer's ~/.ccswitch ledger file.
    o._extra_usage_allowed = lambda account: False

    # _rotation_monitor awaits the fleet-registry read (#852) between q.get()
    # and _spawn. The real read suspends on DB I/O only when a previously run
    # test file left the global engine initialised — which breaks the exact
    # yield-counting in _run_monitor_one_event depending on test order. Stub it
    # with a non-suspending coroutine: records here are unstamped (host=None),
    # so the primary owns them whatever the registry says.
    async def fake_enabled_hosts():
        return frozenset()

    o._enabled_host_names = fake_enabled_hosts
    return o


def _rec(o, account="copilot_acct", *, with_driver=True, status="idle"):
    rec = SessionRecord(id="s1", name="n", account=account, engine=AgentEngine.CLAUDE_CODE, engine_session_id="c1",
                        cols=80, rows=24, cwd="/tmp/x", status=status)
    o._records["s1"] = rec
    if with_driver:
        o._drivers["s1"] = object()
    o._last_prompt["s1"] = "do the thing"
    return rec


def _poller_with_failures(fail_count: int, cached_usage=None) -> UsagePoller:
    p = UsagePoller.__new__(UsagePoller)
    p._fail_count = {}
    p._cache = {}
    p._subscribers = []  # rotation_monitor appends its queue here via subscribe()
    if fail_count > 0:
        p._fail_count["copilot_acct"] = fail_count
    if cached_usage is not None:
        p._cache["copilot_acct"] = cached_usage
    return p


# ---------------------------------------------------------------------------
# UsagePoller.consecutive_failures unit tests
# ---------------------------------------------------------------------------

def test_consecutive_failures_zero_before_any_failure():
    p = _poller_with_failures(0)
    assert p.consecutive_failures("copilot_acct") == 0
    assert p.consecutive_failures("unknown") == 0


def test_consecutive_failures_reflects_fail_count():
    p = _poller_with_failures(5)
    assert p.consecutive_failures("copilot_acct") == 5


# ---------------------------------------------------------------------------
# _rotation_monitor: proactive dead-account rotation
# ---------------------------------------------------------------------------

async def _poll(predicate, *, timeout: float = 5.0) -> bool:
    """Async poll-until — the bounded-wait pattern from test_ws_prompt_after_release.

    Replaces exact `await asyncio.sleep(0)` yield-counting, which broke whenever
    the monitor's iteration gained an await (e.g. the #852 fleet-registry read)
    or the loaded `-n auto` CI runner scheduled tasks differently — `.calls`
    was then still empty at assert time (red masters on 3 rotation tests).
    """
    import time
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.005)
    return True


# A benign event the monitor consumes without side effects: usage=None with zero
# consecutive failures for an account no record uses → the `continue` branch.
_SENTINEL_EVENT = {"account": "zz-sentinel-does-not-exist", "usage": None}


async def _run_monitor_one_event(orch, event: dict):
    """Start the rotation monitor, inject one event, wait until it is FULLY
    processed (spawned side-effect tasks included), then stop the monitor.

    Determinism without yield-counting:
    1. poll until the monitor has subscribed (its queue is in _subscribers);
    2. enqueue the real event plus a no-op sentinel — the monitor handles one
       event per loop iteration, so once the sentinel is dequeued (qsize()==0)
       iteration 1, including its synchronous _spawn calls, has completed;
    3. poll until every spawned bg task (spy_rotate etc.) has finished.
    Works for negative tests too: after (2)+(3) the monitor provably decided
    NOT to act, rather than not having run yet.
    """
    task = asyncio.create_task(orch._rotation_monitor())
    assert await _poll(lambda: orch._poller._subscribers), \
        "rotation monitor never subscribed to the poller"
    for q in list(orch._poller._subscribers):
        q.put_nowait(event)
        q.put_nowait(_SENTINEL_EVENT)
    assert await _poll(
        lambda: all(q.qsize() == 0 for q in orch._poller._subscribers)
    ), "rotation monitor never consumed the injected events"
    assert await _poll(
        lambda: all(t.done() for t in orch._bg_tasks)
    ), "a task spawned by the rotation monitor never finished"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_rotation_monitor_rotates_on_account_unreachable():
    """usage=None + fail_count ≥ threshold → rotate all live sessions on that account."""
    o = _make_orch()
    rec = _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(
        fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": None})

    assert ("rotate", "s1", "account_unreachable") in o.calls


@pytest.mark.asyncio
async def test_rotation_monitor_skips_transient_unreachable():
    """usage=None + fail_count < threshold → transient; no rotation."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(
        fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD - 1)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": None})

    assert not any(c[0] == "rotate" for c in o.calls)


@pytest.mark.asyncio
async def test_rotation_monitor_rotates_at_usage_ceiling():
    """session_pct=100 still triggers rotation (existing behaviour preserved)."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    usage = Usage(session_pct=100.0, session_resets_at=None,
                  weekly_pct=50.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert ("rotate", "s1", "session limit reached") in o.calls


@pytest.mark.asyncio
async def test_rotation_monitor_skips_below_ceiling():
    """session_pct < 100 → no rotation."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    usage = Usage(session_pct=80.0, session_resets_at=None,
                  weekly_pct=20.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert not any(c[0] in ("rotate", "restart_driverless") for c in o.calls)


@pytest.mark.asyncio
async def test_rotation_monitor_skips_ended_sessions():
    """Dead-account rotation must not touch ended/archived sessions."""
    from server.db.models import INACTIVE_SESSION_STATUSES
    o = _make_orch()
    o._poller = _poller_with_failures(
        fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD)
    for status in list(INACTIVE_SESSION_STATUSES)[:2]:
        rec = SessionRecord(id=f"s-{status}", name="n", account="copilot_acct",
                            engine=AgentEngine.CLAUDE_CODE,
                            engine_session_id="c1", cols=80, rows=24, cwd="/tmp",
                            status=status)
        o._records[rec.id] = rec
        o._drivers[rec.id] = object()

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": None})

    assert not any(c[0] == "rotate" for c in o.calls)


@pytest.mark.asyncio
async def test_rotation_monitor_restarts_driverless_live_session():
    """A live session with no driver is restarted via _restart_driverless."""
    o = _make_orch()
    _rec(o, account="copilot_acct", with_driver=False, status="idle")
    o._poller = _poller_with_failures(
        fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": None})

    assert ("restart_driverless", "s1", "account_unreachable") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)


# ---------------------------------------------------------------------------
# _recover_session process_crash: reactive dead-account rotation
# ---------------------------------------------------------------------------

def _orch_with_poller(cached_usage, fail_count=0):
    o = _make_orch()
    o._poller = _poller_with_failures(fail_count=fail_count, cached_usage=cached_usage)
    return o


def test_process_crash_retries_same_account_when_healthy():
    """Healthy account → existing behaviour: rebuild driver on same account."""
    usage = Usage(session_pct=50.0, session_resets_at=None,
                  weekly_pct=20.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    rec = _rec(o)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("reassign", "s1", "copilot_acct", "process_crash") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)


def test_process_crash_rotates_when_account_at_ceiling():
    """session_pct=100 at crash time → quarantine + rotate to a new account."""
    usage = Usage(session_pct=_ROTATION_CEILING, session_resets_at=None,
                  weekly_pct=50.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    rec = _rec(o)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("rotate", "s1", "process_crash_account_exhausted") in o.calls
    assert not any(c[0] == "reassign" for c in o.calls)
    assert "copilot_acct" in o._recovery.quarantined(now=1000.0)


def test_process_crash_rotates_when_account_persistently_unreachable():
    """Proxy dead (usage=None, fail_count ≥ threshold) → rotate, not retry."""
    o = _orch_with_poller(cached_usage=None,
                          fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD)
    rec = _rec(o)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("rotate", "s1", "process_crash_account_exhausted") in o.calls
    assert "copilot_acct" in o._recovery.quarantined(now=1000.0)


def test_process_crash_retries_same_when_transient_unreachable():
    """Few consecutive failures → transient glitch; rebuild on same account."""
    o = _orch_with_poller(cached_usage=None,
                          fail_count=orch_mod._UNREACHABLE_ROTATION_THRESHOLD - 1)
    rec = _rec(o)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("reassign", "s1", "copilot_acct", "process_crash") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)


def test_process_crash_cap_still_pauses_even_on_dead_account():
    """Crash cap takes priority over dead-account detection — prevents thrash."""
    usage = Usage(session_pct=_ROTATION_CEILING, session_resets_at=None,
                  weekly_pct=50.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    rec = _rec(o)
    for _ in range(R.CRASH_CAP):
        o._recovery.record_crash("s1", now=1000.0)
    asyncio.run(o._recover_session(rec, "process_crash", None))
    assert ("pause", "s1", "crash_cap") in o.calls
    assert not any(c[0] == "rotate" for c in o.calls)


# ---------------------------------------------------------------------------
# Weekly ceiling counts as dead (Task 4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotation_monitor_rotates_at_weekly_ceiling():
    """weekly_pct=100 with session headroom is just as dead — the next turn 429s."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    usage = Usage(session_pct=0.0, session_resets_at=None,
                  weekly_pct=100.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert ("rotate", "s1", "weekly limit reached") in o.calls


def test_account_is_dead_on_weekly_ceiling():
    usage = Usage(session_pct=10.0, session_resets_at=None,
                  weekly_pct=100.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    assert o._account_is_dead("copilot_acct") is True


def test_account_is_alive_below_both_ceilings():
    usage = Usage(session_pct=99.0, session_resets_at=None,
                  weekly_pct=99.0, weekly_resets_at=None, extra_usage_enabled=False)
    o = _orch_with_poller(cached_usage=usage)
    assert o._account_is_dead("copilot_acct") is False


# ---------------------------------------------------------------------------
# Rotation monitor triggers a recovery sweep on below-ceiling events (Task 5)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rotation_monitor_triggers_recovery_sweep_below_ceiling():
    """An account dropping below ceiling is the recovery signal the periodic sweep
    otherwise waits up to 300 s for."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    fired = []
    o._maybe_sweep_on_recovery = lambda: fired.append(True) or True
    usage = Usage(session_pct=20.0, session_resets_at=None,
                  weekly_pct=20.0, weekly_resets_at=None, extra_usage_enabled=False)

    await _run_monitor_one_event(o, {"account": "copilot_acct", "usage": usage})

    assert fired == [True]
    assert not any(c[0] in ("rotate", "restart_driverless") for c in o.calls)


# ---------------------------------------------------------------------------
# The paid extra-usage escape: at-ceiling is NOT dead when rank_accounts would
# still hand the account out (its final "extra_paid" tier).
# ---------------------------------------------------------------------------

def _at_ceiling_usage(extra_enabled: bool) -> Usage:
    return Usage(session_pct=_ROTATION_CEILING, session_resets_at=None,
                 weekly_pct=20.0, weekly_resets_at=None,
                 extra_usage_enabled=extra_enabled)


@pytest.mark.asyncio
async def test_rotation_monitor_skips_at_ceiling_account_with_extra_usage():
    """Opted in + enabled upstream → the selector still ranks it usable, so rotating
    off it only finds no target and parks the session as needs_attention."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    o._extra_usage_allowed = lambda account: True
    fired = []
    o._maybe_sweep_on_recovery = lambda: fired.append(True) or True

    await _run_monitor_one_event(
        o, {"account": "copilot_acct", "usage": _at_ceiling_usage(True)})

    assert not any(c[0] in ("rotate", "restart_driverless") for c in o.calls)
    assert fired == [True]


@pytest.mark.asyncio
async def test_rotation_monitor_rotates_at_ceiling_when_not_opted_in():
    """Extra usage enabled upstream but the user never opted in → still dead."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    o._extra_usage_allowed = lambda account: False

    await _run_monitor_one_event(
        o, {"account": "copilot_acct", "usage": _at_ceiling_usage(True)})

    assert ("rotate", "s1", "session limit reached") in o.calls


@pytest.mark.asyncio
async def test_rotation_monitor_rotates_at_ceiling_when_provider_disabled_extra():
    """Opted in but Anthropic reports extra usage OFF → opting in conjures nothing."""
    o = _make_orch()
    _rec(o, account="copilot_acct")
    o._poller = _poller_with_failures(0)
    o._extra_usage_allowed = lambda account: True

    await _run_monitor_one_event(
        o, {"account": "copilot_acct", "usage": _at_ceiling_usage(False)})

    assert ("rotate", "s1", "session limit reached") in o.calls


def test_account_is_dead_false_at_ceiling_with_extra_usage():
    o = _orch_with_poller(cached_usage=_at_ceiling_usage(True))
    o._extra_usage_allowed = lambda account: True
    assert o._account_is_dead("copilot_acct") is False


def test_account_is_dead_true_at_ceiling_extra_usage_not_opted_in():
    o = _orch_with_poller(cached_usage=_at_ceiling_usage(True))
    o._extra_usage_allowed = lambda account: False
    assert o._account_is_dead("copilot_acct") is True


def test_account_is_dead_true_at_ceiling_extra_usage_provider_disabled():
    o = _orch_with_poller(cached_usage=_at_ceiling_usage(False))
    o._extra_usage_allowed = lambda account: True
    assert o._account_is_dead("copilot_acct") is True

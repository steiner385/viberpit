"""A single-account engine must self-heal from a crash, not park forever.

Production incident 2026-09-16, session 37e13a96 (host dobby, Codex, account
chatgpt-tonystein85_gmail.com — the only Codex account configured). The Codex
app-server crashed 4x in 29s; the manager gave up and the immediate same-account
`_reassign` lost a race with the manager's own restart (`CodexTransportClosed`).
Control passed to `_rotate_away`, which HARD-excludes the current account by
design (see test_rotate_never_returns_same_account.py) — but with no other
Codex account configured, `select_account` raises
`AllAccountsBusy(kind="no_usable_accounts")` every single time. `all_accounts_busy`
is retried forever by the stuck-session sweep (it is not a terminal pause
reason — the premise is "wait for an account to free up"), so the session sat
parked in an unwinnable retry loop even after the app-server was healthy again
seconds later. A human had to manually POST /sessions/{id}/reload to unstick it.

The fix: when `AllAccountsBusy` reports `kind="no_usable_accounts"` (no ranked
candidate AND no busy-reset — i.e. nothing to wait ON) and no OTHER account is
even configured for this engine, "wait for one to free up" can never happen —
fall back to retrying the current account in place (the same primitive
`switch_account_manual`/`reload_session` already use), bounded by the existing
per-session crash cap so a genuinely still-broken account still parks instead
of being hammered every sweep tick.
"""
import asyncio

from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.sessions import orchestrator as orch_mod
from server.sessions import recovery as R
from ccswitch.selector import AllAccountsBusy


CURRENT = "acct-only-one-for-engine"


def _orch():
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    o._clock = lambda: 1000.0
    o.calls = []

    async def spy_reassign(record, new_account, *, reason, **kw):
        o.calls.append(("reassign", record.id, record.account, new_account, reason))

    async def spy_pause(record, *, reason, earliest_reset=None):
        o.calls.append(("pause", record.id, reason, earliest_reset))

    o._reassign = spy_reassign
    o._pause_session = spy_pause
    o.live_session_load = lambda: {}
    o._recovery.quarantined = lambda now=None: frozenset()
    return o


def _record(o, account=CURRENT, engine=AgentEngine.CODEX):
    rec = SessionRecord(id="sess-1", name="n", account=account, engine=engine,
                        engine_session_id="c1", cols=80, rows=24, cwd="/tmp/x",
                        status="needs_attention", model="gpt-6-astra")
    o._records["sess-1"] = rec
    o._drivers["sess-1"] = object()
    return rec


def _no_usable_accounts_selector(monkeypatch):
    """Every account is excluded — the single-account-engine shape."""
    def fake(exclude=frozenset(), load=None, *, reserve=True,
             soft_exclude=frozenset(), engine=None, model=None):
        raise AllAccountsBusy(None, "no_usable_accounts")

    monkeypatch.setattr(orch_mod, "select_account", fake)


def test_rotate_away_retries_same_account_when_none_other_exists(monkeypatch):
    o = _orch()
    _no_usable_accounts_selector(monkeypatch)
    monkeypatch.setattr(orch_mod, "has_alternative_account", lambda current, *, engine: False)
    rec = _record(o)

    asyncio.run(o._rotate_away(rec, reason="stuck_session_sweep"))

    assert o.calls == [
        ("reassign", "sess-1", CURRENT, CURRENT, "stuck_session_sweep_same_account"),
    ]
    assert not any(c[0] == "pause" for c in o.calls)


def test_rotate_away_still_pauses_when_an_alternative_account_exists(monkeypatch):
    """Same kind="no_usable_accounts" (e.g. the only other account is reserved or
    needs reauth), but a real alternative IS configured — must not retry same
    account; that would mask a reauth problem as a healthy retry loop."""
    o = _orch()
    _no_usable_accounts_selector(monkeypatch)
    monkeypatch.setattr(orch_mod, "has_alternative_account", lambda current, *, engine: True)
    rec = _record(o)

    asyncio.run(o._rotate_away(rec, reason="stuck_session_sweep"))

    assert o.calls == [("pause", "sess-1", "all_accounts_busy", None)]
    assert not any(c[0] == "reassign" for c in o.calls)


def test_rotate_away_does_not_retry_same_account_at_ceiling(monkeypatch):
    """kind="at_ceiling" (busy_resets computable) is the ordinary "wait it out"
    case pinned by test_rotate_never_returns_same_account.py — must stay
    untouched by this fallback even with no alternative account configured."""
    from datetime import datetime, timezone
    reset = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
    o = _orch()

    def fake(exclude=frozenset(), load=None, *, reserve=True,
             soft_exclude=frozenset(), engine=None, model=None):
        raise AllAccountsBusy(reset, "at_ceiling")

    monkeypatch.setattr(orch_mod, "select_account", fake)
    monkeypatch.setattr(orch_mod, "has_alternative_account", lambda current, *, engine: False)
    rec = _record(o)

    asyncio.run(o._rotate_away(rec, reason="stuck_session_sweep"))

    assert o.calls == [("pause", "sess-1", "all_accounts_busy", reset)]


def test_rotate_away_stops_retrying_same_account_once_crash_capped(monkeypatch):
    """The fallback must still park a genuinely thrashing session — the crash
    cap this reuses is the same guard _recover_session's process_crash branch
    already relies on, not a fresh unbounded retry loop."""
    o = _orch()
    _no_usable_accounts_selector(monkeypatch)
    monkeypatch.setattr(orch_mod, "has_alternative_account", lambda current, *, engine: False)
    rec = _record(o)
    for _ in range(R.CRASH_CAP):
        o._recovery.record_crash("sess-1", now=1000.0)

    asyncio.run(o._rotate_away(rec, reason="stuck_session_sweep"))

    assert o.calls == [("pause", "sess-1", "all_accounts_busy", None)]
    assert not any(c[0] == "reassign" for c in o.calls)


def test_rotate_away_does_not_retry_a_dead_account_even_with_no_alternative(monkeypatch):
    """No other account AND the current one is itself exhausted/unreachable:
    retrying in place would just fail again every sweep tick. Park instead."""
    o = _orch()
    _no_usable_accounts_selector(monkeypatch)
    monkeypatch.setattr(orch_mod, "has_alternative_account", lambda current, *, engine: False)
    o._account_is_dead = lambda account, model=None: True
    rec = _record(o)

    asyncio.run(o._rotate_away(rec, reason="stuck_session_sweep"))

    assert o.calls == [("pause", "sess-1", "all_accounts_busy", None)]
    assert not any(c[0] == "reassign" for c in o.calls)

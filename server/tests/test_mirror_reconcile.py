"""Tests for the primary's fleet-mirror reconcile (#895-adjacent).

The primary holds a _records entry for every fleet session. When a session is
permanently deleted, that runs on the OWNING host — which pops its own _records
and the shared DB row — but never the primary's mirror copy. The stale ghost then
drives autopilot to propose for a row that's gone, so every automation_event /
triage_proposal insert fails the session_id foreign key until the next restart.

_reconcile_mirror_once evicts such ghosts: FOREIGN (not-owned) records whose DB row
has vanished, two-strike so a mid-creation worker session is never dropped.
"""
import pytest

from server.sessions.orchestrator import SessionOrchestrator, _MIRROR_EVICT_STRIKES
from server.sessions.types import SessionRecord


def _make_orch(monkeypatch, *, present_ids):
    # Deterministic identity so host!="dobby-test" records read as foreign on this
    # primary regardless of the test machine's hostname.
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby-test")
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)  # default: primary

    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)

    async def enabled_hosts():
        return frozenset({"rocky", "dobby-test"})

    async def existing_ids(ids):
        return {i for i in ids if i in present_ids}

    o._enabled_host_names = enabled_hosts
    o._existing_session_ids = existing_ids
    return o


def _rec(o, sid, *, host):
    rec = SessionRecord(id=sid, name="n", account="acct",
                        claude_session_id=None, cols=80, rows=24, cwd="/tmp")
    rec.status = "idle"
    rec.host = host
    o._records[sid] = rec
    return rec


@pytest.mark.asyncio
async def test_foreign_ghost_evicted_after_two_strikes(monkeypatch):
    """A foreign record whose DB row is gone is evicted on the _MIRROR_EVICT_STRIKES-th pass."""
    o = _make_orch(monkeypatch, present_ids=set())  # ghost absent from DB
    _rec(o, "ghost", host="rocky")

    for _ in range(_MIRROR_EVICT_STRIKES - 1):
        await o._reconcile_mirror_once()
        assert "ghost" in o._records, "must not evict before the strike cap"

    await o._reconcile_mirror_once()
    assert "ghost" not in o._records, "ghost must be evicted at the strike cap"
    assert "ghost" not in o._mirror_missing_strikes


@pytest.mark.asyncio
async def test_present_foreign_session_never_evicted(monkeypatch):
    """A foreign session whose row still exists is left alone, no strikes accrue."""
    o = _make_orch(monkeypatch, present_ids={"live"})
    _rec(o, "live", host="rocky")

    for _ in range(_MIRROR_EVICT_STRIKES + 2):
        await o._reconcile_mirror_once()

    assert "live" in o._records
    assert "live" not in o._mirror_missing_strikes


@pytest.mark.asyncio
async def test_owned_record_missing_row_not_evicted(monkeypatch):
    """An OWNED record (host=None → primary owns it) missing a row is a mid-creation
    race, never a cross-host delete — it must survive so a just-created session on
    this daemon is never dropped before its row commits."""
    o = _make_orch(monkeypatch, present_ids=set())  # no rows at all
    _rec(o, "mine", host=None)

    for _ in range(_MIRROR_EVICT_STRIKES + 2):
        await o._reconcile_mirror_once()

    assert "mine" in o._records, "owned mid-creation record must never be evicted"
    assert "mine" not in o._mirror_missing_strikes


@pytest.mark.asyncio
async def test_reappearing_row_resets_strikes(monkeypatch):
    """A row that reappears between passes clears the ghost's strike count."""
    present: set[str] = set()
    o = _make_orch(monkeypatch, present_ids=present)
    _rec(o, "flap", host="rocky")

    await o._reconcile_mirror_once()               # miss → 1 strike
    assert o._mirror_missing_strikes.get("flap") == 1

    present.add("flap")                            # row comes back
    await o._reconcile_mirror_once()               # hit → reset
    assert "flap" not in o._mirror_missing_strikes
    assert "flap" in o._records

    present.discard("flap")                        # gone again
    await o._reconcile_mirror_once()               # miss → back to 1, not evicted
    assert o._mirror_missing_strikes.get("flap") == 1
    assert "flap" in o._records

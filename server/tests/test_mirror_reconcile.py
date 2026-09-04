"""Tests for the primary's fleet-mirror reconcile (#895-adjacent, #898).

The primary holds a _records entry for every fleet session. When a session is
permanently deleted, that runs on the OWNING host — which pops its own _records
and the shared DB row — but never the primary's mirror copy. The stale ghost then
drives autopilot to propose for a row that's gone, so every automation_event /
triage_proposal insert fails the session_id foreign key until the next restart.

_reconcile_mirror_once evicts such ghosts: FOREIGN (not-owned) records whose DB row
has vanished (two-strike, so a mid-creation worker session is never dropped) or whose
row is present but inactive/archived (one strike — a committed row rules the
mid-creation race out; #1033).
"""
import asyncio

import pytest

from server.sessions.orchestrator import SessionOrchestrator, _MIRROR_EVICT_STRIKES
from server.sessions.types import SessionRecord


def _make_orch(monkeypatch, *, present_ids, inactive_ids=()):
    # Deterministic identity so host!="dobby-test" records read as foreign on this
    # primary regardless of the test machine's hostname.
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "dobby-test")
    monkeypatch.delenv("CCSWITCH_DAEMON_ROLE", raising=False)  # default: primary

    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)

    async def enabled_hosts():
        return frozenset({"rocky", "dobby-test"})

    async def liveness(ids):
        return ({i for i in ids if i in present_ids},
                {i for i in ids if i in inactive_ids})

    o._enabled_host_names = enabled_hosts
    o._session_row_liveness = liveness
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


@pytest.mark.asyncio
async def test_archived_foreign_session_evicted_on_the_first_pass(monkeypatch):
    """A foreign session whose DB row EXISTS but is archived is evicted immediately.

    The two-strike debounce exists for one ambiguity only: a MISSING row could be a
    worker mid-creating a session whose row has not committed yet. An archived row is
    not ambiguous — it committed, then the owner archived it. Waiting a second pass
    there only lengthens the window in which the dashboard shows a card whose every
    action 404s (#1033 measured ~110s in the field).
    """
    o = _make_orch(monkeypatch, present_ids=set(),
                   inactive_ids={"archived-on-rocky"})
    _rec(o, "archived-on-rocky", host="rocky")

    await o._reconcile_mirror_once()
    assert "archived-on-rocky" not in o._records, "archived foreign session must be evicted"
    assert "archived-on-rocky" not in o._mirror_missing_strikes


@pytest.mark.asyncio
async def test_owned_archived_record_is_never_evicted(monkeypatch):
    """One-strike eviction stays scoped to FOREIGN records.

    The primary archives its own sessions in-process and pops _records itself; an
    owned record that is merely archived in the DB must survive reconcile, or an
    archived-then-unarchived local session would lose its record mid-flight.
    """
    o = _make_orch(monkeypatch, present_ids=set(), inactive_ids={"mine-archived"})
    _rec(o, "mine-archived", host=None)  # host=None → the primary owns it

    for _ in range(_MIRROR_EVICT_STRIKES + 2):
        await o._reconcile_mirror_once()

    assert "mine-archived" in o._records


@pytest.mark.asyncio
async def test_deleted_row_still_waits_the_full_strike_cap(monkeypatch):
    """The mid-creation guard survives the one-strike archived path.

    A worker dispatched a session and its row has not committed yet: no row at all,
    so reconcile must still wait _MIRROR_EVICT_STRIKES passes rather than dropping a
    session that is about to appear.
    """
    o = _make_orch(monkeypatch, present_ids=set())  # no row, not archived
    _rec(o, "mid-create-on-rocky", host="rocky")

    for _ in range(_MIRROR_EVICT_STRIKES - 1):
        await o._reconcile_mirror_once()
        assert "mid-create-on-rocky" in o._records

    await o._reconcile_mirror_once()
    assert "mid-create-on-rocky" not in o._records


@pytest.mark.asyncio
async def test_active_foreign_session_not_evicted_after_fix(monkeypatch):
    """A foreign session that is still ACTIVE (in present_ids) is never evicted.

    Regression guard: the row-status split must not affect active sessions — only
    archived/ended ones land in the inactive set.
    """
    o = _make_orch(monkeypatch, present_ids={"active-on-rocky"})
    _rec(o, "active-on-rocky", host="rocky")

    for _ in range(_MIRROR_EVICT_STRIKES + 2):
        await o._reconcile_mirror_once()

    assert "active-on-rocky" in o._records
    assert "active-on-rocky" not in o._mirror_missing_strikes


@pytest.mark.asyncio
async def test_session_row_liveness_partitions_against_a_real_db(tmp_path):
    """The DB seam itself: active / inactive-but-present / no-row-at-all.

    The unit tests above stub _session_row_liveness, so this pins the one query that
    feeds them — in particular that an archived row lands in the INACTIVE set (which
    evicts on one pass) and NOT in the missing bucket (which waits two).
    """
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession

    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'liveness.db'}")
    async with db_session() as db:
        db.add(DbSession(id="live", name="L", account_name="a",
                         claude_session_id=None, status="idle", cwd="/tmp"))
        db.add(DbSession(id="arch", name="A", account_name="a",
                         claude_session_id=None, status="archived", cwd="/tmp"))
        db.add(DbSession(id="over", name="E", account_name="a",
                         claude_session_id=None, status="ended", cwd="/tmp"))

    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)
    active, inactive = await o._session_row_liveness(["live", "arch", "over", "gone"])

    assert active == {"live"}
    assert inactive == {"arch", "over"}, "a present-but-inactive row is not 'missing'"
    assert await o._session_row_liveness([]) == (set(), set())


# ---------------------------------------------------------------------------
# Loop wiring: reconcile is NOT paced by the autopilot tick (#1033)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_autopilot_tick_loop_no_longer_drives_the_reconcile(monkeypatch):
    """Reconcile must not ride the autopilot loop body.

    That body is sequential — reconcile → tick() → sleep(interval) — so an LLM-bound
    tick() stretched the effective eviction period to (tick duration + interval), and
    the two-strike cap doubled it: ~110s in the field for a card the strike cap says
    should be gone in ≤40s.
    """
    o = _make_orch(monkeypatch, present_ids=set())
    calls: list[str] = []

    async def _boom():
        calls.append("reconcile")

    async def _tick():
        calls.append("tick")

    o._reconcile_mirror_once = _boom
    o._autopilot_loop.tick = _tick

    task = asyncio.create_task(o._autopilot_tick_loop(interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert "tick" in calls
    assert "reconcile" not in calls, "reconcile must have its own task, not the autopilot's"


@pytest.mark.asyncio
async def test_mirror_reconcile_loop_ticks_on_its_own_cadence(monkeypatch):
    """The dedicated loop keeps sweeping, and one failing pass never stalls it."""
    o = _make_orch(monkeypatch, present_ids=set())
    calls: list[int] = []

    async def _flaky():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("DB blip")

    o._reconcile_mirror_once = _flaky

    task = asyncio.create_task(o._mirror_reconcile_loop(interval=0.01))
    await asyncio.sleep(0.06)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert len(calls) >= 3, f"loop stalled after {len(calls)} pass(es)"

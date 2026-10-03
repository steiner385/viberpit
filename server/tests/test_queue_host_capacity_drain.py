"""The queue drain considers HOST capacity, and a queued session starts on
whichever host frees a slot.

Before this, ``_maybe_start_queued`` gated only on the fleet-wide cap and always
spawned on the draining daemon: a session queued on a full host stayed queued
while another host had room, and a drain on a full host could over-subscribe
it. Three rules now:

* a daemon never starts a queued session past its own ``Host.max_sessions``;
* a daemon with headroom ADOPTS a queued entry whose stamped owner is known to be
  full (or down / disabled), re-stamping the row to itself — the session lease
  (#1379) is taken first so two daemons cannot both start it;
* a lease refusal mid-spawn means another host is starting it: skip, never end.

The daemon's ``create_session(queue_only=True)`` is the edge's way of saying
"every host is full — persist this as queued" (test_fleet_queue_on_host_capacity).
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select as sa_select, update as sa_update

from server.db import db_session
from server.db.models import Host, Session as DbSession
from server.hostname import self_host_name
from server.sessions import session_queue as sq
from server.tests.test_session_queue_drain import _make_orch, _stop_orch

ME = "alpha-host"


async def _add_hosts(orch, *rows: Host) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(r)
    # The ownership snapshot is cached for a minute; the rows above must be seen now.
    orch._ownership_hosts_cache = None


def _host(name: str, *, max_sessions: int | None, active: int = 0,
          healthy: bool | None = True, enabled: bool = True) -> Host:
    return Host(name=name, endpoint=f"https://{name}:9443", enabled=enabled,
                healthy=healthy, max_sessions=max_sessions, active_sessions=active)


async def _row(sid: str) -> DbSession | None:
    async with db_session() as db:
        return (await db.execute(
            sa_select(DbSession).where(DbSession.id == sid))).scalar_one_or_none()


async def _restamp(sid: str, host: str | None) -> None:
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == sid).values(host=host))


@pytest.fixture
def me(monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", ME)
    assert self_host_name() == ME
    return ME


# --- queue_only ----------------------------------------------------------------

def test_queue_only_persists_a_queued_row_under_the_cap(tmp_path, me):
    """The fleet-wide cap is nowhere near reached; the EDGE said every host is
    full, and that is reason enough to persist the session as queued."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'q.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            rec = await orch.create_session("waiting", queue_only=True)
            assert rec.status == "queued"
            row = await _row(rec.id)
            assert row is not None and row.status == "queued"
            assert (await sq.positions()) == {rec.id: 1}
            entry = await sq.next_entry()
            assert entry is not None and entry.params.get("queue_reason") == "host_capacity"
            assert orch._active_session_count() == 0
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_queue_only_entry_starts_on_a_later_drain(tmp_path, me):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'q2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            rec = await orch.create_session("waiting", queue_only=True)
            assert await orch._maybe_start_queued() == 1
            assert orch._records[rec.id].status != "queued"
            assert (await sq.positions()) == {}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


# --- the per-host cap -----------------------------------------------------------

def test_drain_never_exceeds_this_hosts_own_cap(tmp_path, me):
    """Fleet cap 6 (1 + 5), this host capped at 1 with one live session: the drain
    must not start the queued entry here even though the GLOBAL gate would allow
    it. Freeing the slot lets it start."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'c.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=1), _host("other", max_sessions=5))
            live = await orch.create_session("one")
            waiting = await orch.create_session("two", queue_only=True)
            assert orch._active_session_count() == 1
            assert await orch._maybe_start_queued() == 0
            assert orch._records[waiting.id].status == "queued"
            assert (await sq.positions()) == {waiting.id: 1}

            # close_session schedules its own drain (queue-drain-on-close), which may
            # win the race with this explicit call — assert the outcome, not who did it.
            await orch.close_session(live.id)
            await orch._maybe_start_queued()
            assert orch._records[waiting.id].status != "queued"
            assert (await sq.positions()) == {}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


# --- adoption: a slot frees on ANOTHER host --------------------------------------

def test_a_host_with_headroom_adopts_an_entry_whose_owner_is_full(tmp_path, me):
    """The session was queued on (and stamped for) a host the registry shows at
    4/4. This daemon has room: it takes the entry, re-stamps the row to itself and
    starts the session — the "slot frees → dispatched" half of the fix."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'a.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5),
                             _host("other", max_sessions=4, active=4))
            rec = await orch.create_session("stuck", queue_only=True)
            # Simulate "queued on the other daemon": its row stamp and no record here.
            await _restamp(rec.id, "other")
            orch._records.pop(rec.id)

            assert await orch._maybe_start_queued() == 1

            row = await _row(rec.id)
            assert row is not None and row.status != "queued" and row.host == ME
            assert orch._records[rec.id].host == ME
            assert (await sq.positions()) == {}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_an_entry_whose_owner_has_room_is_left_to_that_owner(tmp_path, me):
    """Ownership (#852) still holds when the stamped host can start it itself:
    stealing would race that daemon's own drain for nothing."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'l.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5),
                             _host("other", max_sessions=4, active=1))
            rec = await orch.create_session("theirs", queue_only=True)
            await _restamp(rec.id, "other")
            orch._records.pop(rec.id)

            assert await orch._maybe_start_queued() == 0

            row = await _row(rec.id)
            assert row is not None and row.status == "queued" and row.host == "other"
            assert (await sq.positions()) == {rec.id: 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_an_entry_whose_owner_is_down_is_adopted(tmp_path, me):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5),
                             _host("other", max_sessions=4, active=0, healthy=False))
            rec = await orch.create_session("orphaned", queue_only=True)
            await _restamp(rec.id, "other")
            orch._records.pop(rec.id)

            assert await orch._maybe_start_queued() == 1
            row = await _row(rec.id)
            assert row is not None and row.host == ME and row.status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_a_lease_held_elsewhere_skips_the_entry_without_ending_it(tmp_path, me):
    """The owner is full per the (≤30 s stale) registry but is in fact starting the
    session right now and holds its lease: this daemon must leave the entry alone —
    and must NOT take the drain's start-failure path, which ends the session."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'h.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5),
                             _host("other", max_sessions=4, active=4))
            rec = await orch.create_session("racing", queue_only=True)
            await _restamp(rec.id, "other")
            orch._records.pop(rec.id)
            from server.sessions import session_lease
            from server.sessions.orchestrator import SESSION_LEASE_TTL_S
            assert await session_lease.acquire(rec.id, "other", ttl_s=SESSION_LEASE_TTL_S) is not None

            assert await orch._maybe_start_queued() == 0

            row = await _row(rec.id)
            assert row is not None and row.status == "queued" and row.host == "other"
            assert (await sq.positions()) == {rec.id: 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_the_primary_resyncs_a_queued_mirror_the_row_says_moved(tmp_path, me):
    """After another host adopts the entry, the primary's in-memory 'queued' copy
    still says host=me. The mirror reconcile re-reads the row so the record is
    foreign (evictable) rather than a zombie card the dashboard keeps forever."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5), _host("other", max_sessions=4))
            rec = await orch.create_session("moved", queue_only=True)
            async with db_session() as db:
                await db.execute(sa_update(DbSession).where(DbSession.id == rec.id)
                                 .values(host="other", status="idle"))
            await orch._reconcile_mirror_once()
            mirror = orch._records[rec.id]
            assert mirror.host == "other"
            assert mirror.status == "idle"
            assert mirror.detached is True
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_a_worker_drops_a_queued_record_the_row_says_moved(tmp_path, me):
    """A worker holds only its own sessions. A stale copy kept as a mirror would
    be served by list_sessions, and the fleet merge prefers a worker's record
    over the primary's — shadowing the live session on the host that adopted it."""
    from unittest.mock import patch

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'w.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=10)
        try:
            await _add_hosts(orch, _host(ME, max_sessions=5), _host("other", max_sessions=4))
            rec = await orch.create_session("moved", queue_only=True)
            async with db_session() as db:
                await db.execute(sa_update(DbSession).where(DbSession.id == rec.id)
                                 .values(host="other", status="idle"))
            with patch("server.sessions.orchestrator.daemon_role", return_value="worker"):
                await orch._reconcile_mirror_once()
            assert rec.id not in orch._records
        finally:
            await _stop_orch(orch)

    asyncio.run(run())

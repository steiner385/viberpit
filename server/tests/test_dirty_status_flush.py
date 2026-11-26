"""FR-307 (spec 002): a failed status persist marks the in-memory record dirty and a
periodic flusher retries it, so DB-shadow divergence is bounded by one flush interval.

Before this, a DB failure inside ``_persist_status`` propagated to the driver's
``on_status`` hook (logged ``driver.on_status failed``) and the WS status frame that
follows the write was silently lost: the live view and the polled shadow both went
stale at once. Now the write is the only thing that fails — the record, the publish
and the push still happen — and ``_flush_dirty_status`` heals the row later.
"""
from __future__ import annotations

import asyncio
import logging
import types

import pytest
from sqlalchemy import select as sa_select

from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.sessions import orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _build_orch(tmp_path) -> SessionOrchestrator:
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'dirty.db'}"
    await init_db(db_url=db_url)
    async with db_session() as db:
        db.add(DbSession(id="s1", name="New Session", account_name="acct1",
                         engine_session_id="claude-s1", status="idle", cwd="/repo/s1"))
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    await orch.rehydrate()
    orch._drivers["s1"] = types.SimpleNamespace(last_hand_back_reason=None)
    return orch


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(
            sa_select(DbSession).where(DbSession.id == sid))).scalar_one()


class _RaiseOnce:
    """Stand-in for ``orchestrator.db_session``: the first ``async with`` raises as a
    lost DB connection would; every later one delegates to the real factory."""

    def __init__(self, real):
        self._real = real
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            return self._boom()
        return self._real()

    @staticmethod
    def _boom():
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm():
            raise RuntimeError("simulated: database connection lost")
            yield  # pragma: no cover - unreachable, keeps it a generator

        return _cm()


def _status_frames(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("type") == "status":
            out.append(ev)
    return out


def test_db_failure_marks_dirty_publishes_anyway_and_flusher_heals(tmp_path, monkeypatch, caplog):
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            q = orch.subscribe_events()
            fake = _RaiseOnce(db_session)
            monkeypatch.setattr(orch_mod, "db_session", fake)
            with caplog.at_level(logging.WARNING, logger="server.sessions.orchestrator"):
                final = await orch._persist_status("s1", "waiting_input")
            monkeypatch.setattr(orch_mod, "db_session", db_session)
            rec = orch.record("s1")
            row_before = await _row("s1")
            frames = _status_frames(q)
            dirty_before = set(orch._dirty_status)

            healed = await orch._flush_dirty_status()
            row_after = await _row("s1")
            dirty_after = set(orch._dirty_status)
            return (final, fake.calls, rec, row_before, frames, dirty_before,
                    healed, row_after, dirty_after)
        finally:
            await orch.stop()
            await dispose_db()

    (final, calls, rec, row_before, frames, dirty_before,
     healed, row_after, dirty_after) = asyncio.run(run())

    assert calls >= 1, "the fake never saw the status write"
    # The in-memory record moved, the row did not: that gap is what "dirty" names.
    assert final == "waiting_input"
    assert rec.status == "waiting_input" and rec.last_turn_at is not None
    assert row_before.status == "idle" and row_before.last_turn_at is None
    assert dirty_before == {"s1"}
    assert any("persist.status_db_failed" in r.getMessage() for r in caplog.records)
    # The live frame still went out — the whole point of catching the write.
    assert [f["status"] for f in frames] == ["waiting_input"]
    assert frames[0]["sessionId"] == "s1"
    # One flusher tick heals the row from the record and clears the mark.
    assert healed == 1
    assert row_after.status == "waiting_input"
    assert row_after.last_turn_at == rec.last_turn_at
    assert dirty_after == set()


def test_successful_persist_discards_a_stale_dirty_mark(tmp_path):
    """A dirty id whose next persist succeeds is clean again — the flusher must not
    re-write a row the pump has since caught up on."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._dirty_status.add("s1")
            await orch._persist_status("s1", "in_progress")
            return set(orch._dirty_status), (await _row("s1")).status
        finally:
            await orch.stop()
            await dispose_db()
    dirty, status = asyncio.run(run())
    assert dirty == set()
    assert status == "in_progress"


def test_flusher_drops_a_dirty_id_with_no_record(tmp_path):
    """A session closed/archived between the failed write and the tick has nothing
    in memory to heal from; the mark is dropped silently, nothing is counted."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._dirty_status.add("gone")
            healed = await orch._flush_dirty_status()
            return healed, set(orch._dirty_status), (await _row("s1")).status
        finally:
            await orch.stop()
            await dispose_db()
    healed, dirty, status = asyncio.run(run())
    assert healed == 0
    assert dirty == set()
    assert status == "idle"      # the unrelated row is untouched


def test_flusher_mirrors_every_column_the_status_write_carries(tmp_path):
    """The heal is the whole status write, not just ``status``: a failed
    ``handed_back`` persist also lost the ask, the parked overlay and the catalog."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            rec = orch.record("s1")
            rec.status = "handed_back"
            rec.hand_back_reason = "need a scope decision"
            rec.suspected_complete_reason = "PR #1 merged"
            rec.background_work = {"shells": [{"id": "b1"}]}
            rec.command_catalog = [{"name": "/review"}]
            orch._dirty_status.add("s1")
            healed = await orch._flush_dirty_status()
            return healed, await _row("s1")
        finally:
            await orch.stop()
            await dispose_db()
    healed, row = asyncio.run(run())
    assert healed == 1
    assert row.status == "handed_back"
    assert row.hand_back_reason == "need a scope decision"
    assert row.suspected_complete_reason == "PR #1 merged"
    assert row.background_work == {"shells": [{"id": "b1"}]}
    assert row.command_catalog == [{"name": "/review"}]
    assert row.paused_reason is None and row.paused_until is None


def test_flusher_keeps_the_mark_when_the_record_moves_mid_write(tmp_path):
    """Snapshot → UPDATE → re-check: if a _persist_status assigns the record while
    the flusher's write is in flight, the flusher's row may be the stale one. It
    must NOT discard the mark on that tick; the next tick mirrors the new value."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            rec = orch.record("s1")
            rec.status = "in_progress"
            orch._dirty_status.add("s1")
            real_update = orch._update_session_row

            async def racing_update(session_id, values):
                # The pump moves the record after the flusher took its snapshot.
                rec.status = "done"
                await real_update(session_id, values)
            orch._update_session_row = racing_update  # type: ignore[method-assign]
            first = await orch._flush_dirty_status()
            row_mid = (await _row("s1")).status
            dirty_mid = set(orch._dirty_status)

            orch._update_session_row = real_update  # type: ignore[method-assign]
            second = await orch._flush_dirty_status()
            return first, row_mid, dirty_mid, second, (await _row("s1")).status, set(orch._dirty_status)
        finally:
            await orch.stop()
            await dispose_db()
    first, row_mid, dirty_mid, second, row_end, dirty_end = asyncio.run(run())
    assert first == 0 and row_mid == "in_progress" and dirty_mid == {"s1"}
    assert second == 1 and row_end == "done" and dirty_end == set()


def test_flusher_drops_a_record_this_daemon_does_not_own(tmp_path, monkeypatch):
    """A session moved to another host: its row is that host's daemon's to write.
    Ownership is ``_owns_record`` (live driver here OR the host stamp), not
    ``detached`` — a completed session whose driver was released is still ours."""
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
            rec = orch.record("s1")
            rec.status = "done"
            rec.host = "some-other-runner"
            orch._drivers.pop("s1", None)          # no live driver here either
            orch._dirty_status.add("s1")
            healed = await orch._flush_dirty_status()
            foreign = healed, set(orch._dirty_status), (await _row("s1")).status

            # Same record, driver released, but stamped for US: still healed.
            monkeypatch.delenv("VIBERPIT_DAEMON_ROLE")
            rec.host = None
            orch._dirty_status.add("s1")
            healed = await orch._flush_dirty_status()
            return foreign, (healed, set(orch._dirty_status), (await _row("s1")).status)
        finally:
            await orch.stop()
            await dispose_db()
    foreign, ours = asyncio.run(run())
    assert foreign == (0, set(), "idle")
    assert ours == (1, set(), "done")


def test_flusher_keeps_the_mark_when_the_retry_fails_too(tmp_path, monkeypatch):
    async def run():
        orch = await _build_orch(tmp_path)
        try:
            orch._dirty_status.add("s1")
            orch.record("s1").status = "done"
            monkeypatch.setattr(orch_mod, "db_session", _RaiseOnce(db_session))
            healed = await orch._flush_dirty_status()
            monkeypatch.setattr(orch_mod, "db_session", db_session)
            return healed, set(orch._dirty_status), (await _row("s1")).status
        finally:
            await orch.stop()
            await dispose_db()
    healed, dirty, status = asyncio.run(run())
    assert healed == 0
    assert dirty == {"s1"}       # still owed; the next tick tries again
    assert status == "idle"


@pytest.mark.asyncio
async def test_start_launches_the_flush_loop_on_every_host(tmp_path, monkeypatch):
    """The flusher is per-host (each daemon owns its own records), so it is NOT in
    ``_PRIMARY_ONLY_LOOP_ATTRS``; ``stop()`` reaps it (pinned by name in
    test_orchestrator_stop_completeness)."""
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'c.db'}")
    try:
        for role in ("primary", "worker"):
            monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", role)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            await orch.start()
            task = orch._dirty_flush_task
            assert task is not None and task.get_name() == "dirty-status-flush", role
            await orch.stop()
            assert orch._dirty_flush_task is None and task.done(), role
    finally:
        await dispose_db()


def test_flush_interval_defaults_to_ten_seconds():
    """FR-307's 'default 10 s'. The env override path is env_float's own contract."""
    assert orch_mod.DIRTY_FLUSH_INTERVAL_S == 10.0

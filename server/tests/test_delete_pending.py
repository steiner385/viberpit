"""FR-404 (spec 002, #947): a permanent delete that KEEPS the row says so.

``_permanent_delete_once`` already keeps the DB row when the worktree / archive dir
cannot be removed (stale sshfs handle, containment guard), so a later attempt can
reclaim it instead of stranding the directory. Before this, that outcome was
indistinguishable from "no such session": the orchestrator returned False either way
and the API 404'd — the card vanished optimistically while the row stayed.

Now the kept row is tracked in memory (``_pending_deletes`` + the cap purge's strike
counter), a ``delete_retry`` timeline event is recorded per attempt, ``delete_state()``
answers ``pending`` / ``stuck`` / None, a per-host loop retries the non-stuck ones, and
the API answers 202 ``{"state": …}`` instead of 404. Archived rows also carry their
``archived_by`` provenance (FR-405) and the delete state so a refetch keeps the chip.
"""
from __future__ import annotations

import asyncio
import os
import types
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select as sa_select

from server.app import create_app
from server.db import db_session, dispose_db, init_db
from server.db.models import Session as DbSession
from server.db.models import SessionHold
from server.sessions import provenance
from server.sessions.orchestrator import _ARCHIVE_PURGE_MAX_STRIKES, SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_IDENT = {"Tailscale-User-Login": "deltest@example.com"}
_STALE = "ESTALE: [Errno 116] Stale file handle"
_RMTREE = "server.sessions.orchestrator._rmtree_best_effort"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_row(sid: str, arch_root: Path, **extra) -> dict:
    """A legacy (moved-into-archive-home) archived row whose dir exists, so the real
    ``_delete_legacy_archive_dir`` path runs and the rmtree outcome decides the row."""
    (arch_root / sid).mkdir(parents=True, exist_ok=True)
    return dict(id=sid, name=sid.upper(), account_name="acc1", engine_session_id=None,
                status="archived", cwd=f"/old/{sid}", archive_root=str(arch_root), **extra)


async def _seed(rows: list[dict]) -> None:
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _row(sid: str) -> DbSession | None:
    async with db_session() as db:
        return (await db.execute(
            sa_select(DbSession).where(DbSession.id == sid))).scalars().first()


async def _retry_events(sid: str) -> list[dict]:
    return [e for e in await provenance.list_events(sid)
            if e["kind"] == provenance.DELETE_RETRY]


def _orch() -> SessionOrchestrator:
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'delete.db'}"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_delete_retry_is_in_the_provenance_vocabulary():
    assert provenance.DELETE_RETRY == "delete_retry"
    assert provenance.DELETE_RETRY in provenance.KINDS


# ---------------------------------------------------------------------------
# Orchestrator: kept row → pending → stuck → cleared
# ---------------------------------------------------------------------------


def test_kept_row_is_pending_with_one_delete_retry_event(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch")])
        orch = _orch()
        with patch(_RMTREE, return_value=_STALE):
            ok = await orch.permanent_delete("a1")
        return ok, await _row("a1"), orch.delete_state("a1"), await _retry_events("a1")

    ok, row, state, events = asyncio.run(run())
    assert ok is False
    assert row is not None, "the row must be KEPT when the dir could not be removed"
    assert state == "pending"
    assert [e["detail"] for e in events] == [{"strike": 1, "error": _STALE, "stuck": False}]


def test_three_failures_mark_it_stuck(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch")])
        orch = _orch()
        states = []
        with patch(_RMTREE, return_value=_STALE):
            for _ in range(_ARCHIVE_PURGE_MAX_STRIKES):
                await orch.permanent_delete("a1")
                states.append(orch.delete_state("a1"))
        events = await _retry_events("a1")
        return states, events, sorted(orch._archive_purge_poisoned)

    states, events, poisoned = asyncio.run(run())
    assert states == ["pending", "pending", "stuck"]
    assert poisoned == ["a1"]
    # newest first
    assert [e["detail"]["strike"] for e in events] == [3, 2, 1]
    assert [e["detail"]["stuck"] for e in events] == [True, False, False]


def test_successful_retry_clears_pending_strikes_and_poison(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch")])
        orch = _orch()
        with patch(_RMTREE, return_value=_STALE):
            await orch.permanent_delete("a1")
        assert orch.delete_state("a1") == "pending"
        # The mount "recovers": the retry tick deletes it for real.
        healed = await orch._retry_pending_deletes()
        return (healed, await _row("a1"), orch.delete_state("a1"),
                dict(orch._pending_deletes), dict(orch._archive_purge_strikes),
                set(orch._archive_purge_poisoned), (tmp_path / "arch" / "a1").exists())

    healed, row, state, pending, strikes, poisoned, dir_exists = asyncio.run(run())
    assert healed == 1
    assert row is None and not dir_exists
    assert state is None
    assert pending == {} and strikes == {} and poisoned == set()


def test_retry_tick_skips_stuck_ids_and_a_manual_delete_still_tries(tmp_path):
    """The loop never hammers a poisoned id (the cap purge skips it for the same
    reason); a user re-issuing the delete does try again, and success un-sticks it."""
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch")])
        orch = _orch()
        with patch(_RMTREE, return_value=_STALE):
            for _ in range(_ARCHIVE_PURGE_MAX_STRIKES):
                await orch.permanent_delete("a1")
        assert orch.delete_state("a1") == "stuck"
        calls = []
        real = orch.permanent_delete

        async def counting(sid):
            calls.append(sid)
            return await real(sid)

        orch.permanent_delete = counting  # type: ignore[method-assign]
        healed = await orch._retry_pending_deletes()
        skipped = (healed, list(calls), orch.delete_state("a1"))
        ok = await orch.permanent_delete("a1")  # the user clicks delete again
        return skipped, ok, orch.delete_state("a1"), await _row("a1")

    skipped, ok, state, row = asyncio.run(run())
    assert skipped == (0, [], "stuck")
    assert ok is True and state is None and row is None


def test_unknown_session_is_not_pending(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        orch = _orch()
        return await orch.permanent_delete("nope"), orch.delete_state("nope")

    assert asyncio.run(run()) == (False, None)


def test_pending_id_whose_row_vanished_is_dropped(tmp_path):
    """Another path (a cap purge on a sibling daemon, a manual DB fix) removed the
    row: the retry finds nothing and the stale pending mark goes with it."""
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch")])
        orch = _orch()
        with patch(_RMTREE, return_value=_STALE):
            await orch.permanent_delete("a1")
        async with db_session() as db:
            from sqlalchemy import delete as sa_delete
            await db.execute(sa_delete(DbSession).where(DbSession.id == "a1"))
        healed = await orch._retry_pending_deletes()
        return healed, orch.delete_state("a1"), dict(orch._archive_purge_strikes)

    healed, state, strikes = asyncio.run(run())
    assert healed == 0
    assert state is None
    assert strikes == {}


def test_cap_purge_counts_one_strike_per_kept_row(tmp_path):
    """The cap purge used to count the kept row itself; now the delete path does, so a
    purge attempt must not double-count (three sweeps = three strikes, not six)."""
    async def run():
        with patch.dict(os.environ, {"VIBERPIT_HOME": str(tmp_path)}):
            await init_db(db_url=_db_url(tmp_path))
            await _seed([_legacy_row("a1", tmp_path / "arch")])
            orch = _orch()
            with patch(_RMTREE, return_value=_STALE):
                await orch.purge_oldest_archived(1)
            return dict(orch._archive_purge_strikes), orch.delete_state("a1")

    strikes, state = asyncio.run(run())
    assert strikes == {"a1": 1}
    assert state == "pending"


def test_start_launches_the_retry_loop_and_stop_ends_it(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        orch = _orch()
        before = set(asyncio.all_tasks())
        await orch.start()
        try:
            names = {t.get_name() for t in asyncio.all_tasks() - before}
        finally:
            await orch.stop()
        return names, orch._delete_retry_task

    names, task = asyncio.run(run())
    assert "delete-retry" in names
    assert task is None


# ---------------------------------------------------------------------------
# list_archived carries provenance + delete state (FR-405 / FR-404 refetch)
# ---------------------------------------------------------------------------


def test_list_archived_carries_archived_by_reason_and_delete_state(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([
            _legacy_row("a1", tmp_path / "arch", archived_by="completion",
                        archive_context={"reason": "goal met", "pr_state": "merged"}),
            dict(id="a2", name="A2", account_name="acc1", engine_session_id=None,
                 status="archived", cwd="/old/a2"),
        ])
        orch = _orch()
        with patch(_RMTREE, return_value=_STALE):
            await orch.permanent_delete("a1")
        rows = {r["id"]: r for r in await orch.list_archived()}
        return rows

    rows = asyncio.run(run())
    assert rows["a1"]["archivedBy"] == "completion"
    assert rows["a1"]["archiveReason"] == "goal met"
    assert rows["a1"]["deleteState"] == "pending"
    assert rows["a2"]["archivedBy"] is None
    assert rows["a2"]["archiveReason"] is None
    assert rows["a2"]["deleteState"] is None


# ---------------------------------------------------------------------------
# API: 202 {"state"} for a kept row, 404 only for a missing one; batch `pending`
# ---------------------------------------------------------------------------


def _seed_sync(db_url: str, rows: list[dict]) -> None:
    async def run():
        await init_db(db_url=db_url)
        await _seed(rows)
        await dispose_db()
    asyncio.run(run())


def _client(tmp_path):
    orch = _orch()
    app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_permanent_delete_route_answers_202_pending_then_stuck(tmp_path):
    db_url = _db_url(tmp_path)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed_sync(db_url, [_legacy_row("a1", tmp_path / "arch")])
        with _client(tmp_path) as client, patch(_RMTREE, return_value=_STALE):
            codes = []
            for _ in range(_ARCHIVE_PURGE_MAX_STRIKES):
                r = client.delete("/api/sessions/a1/permanent", headers=_IDENT)
                codes.append((r.status_code, r.json()))
            archived = client.get("/api/sessions/archived", headers=_IDENT).json()
    assert codes == [(202, {"state": "pending"}), (202, {"state": "pending"}),
                     (202, {"state": "stuck"})]
    assert [(a["id"], a["deleteState"]) for a in archived] == [("a1", "stuck")]


def test_archived_route_overlays_fleet_wide_delete_states(tmp_path):
    """Fleet mode: the archived list is served by the primary, whose memory knows
    nothing of a row a WORKER could not delete. The route overlays the fleet-wide
    ``pending_delete_states`` union so that row keeps its chip after a refetch."""
    db_url = _db_url(tmp_path)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed_sync(db_url, [
            dict(id="w1", name="W1", account_name="acc1", engine_session_id=None,
                 status="archived", cwd="/old/w1", host="rocky"),
            dict(id="p1", name="P1", account_name="acc1", engine_session_id=None,
                 status="archived", cwd="/old/p1"),
        ])
        # The listing host has no local state for either row; the fleet union says
        # the worker is still trying to delete w1.
        with patch.object(SessionOrchestrator, "pending_delete_states",
                          return_value={"w1": "pending"}), _client(tmp_path) as client:
            rows = client.get("/api/sessions/archived", headers=_IDENT).json()
    assert {r["id"]: r["deleteState"] for r in rows} == {"w1": "pending", "p1": None}


def test_pending_delete_states_maps_every_kept_row_on_this_host(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("a1", tmp_path / "arch"), _legacy_row("a2", tmp_path / "arch")])
        orch = _orch()
        assert orch.pending_delete_states() == {}
        with patch(_RMTREE, return_value=_STALE):
            await orch.permanent_delete("a1")
            for _ in range(_ARCHIVE_PURGE_MAX_STRIKES):
                await orch.permanent_delete("a2")
        return orch.pending_delete_states()

    assert asyncio.run(run()) == {"a1": "pending", "a2": "stuck"}


def test_permanent_delete_route_404s_only_for_a_missing_row(tmp_path):
    db_url = _db_url(tmp_path)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed_sync(db_url, [])
        with _client(tmp_path) as client:
            r = client.delete("/api/sessions/ghost/permanent", headers=_IDENT)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_batch_permanent_delete_reports_pending_apart_from_failed(tmp_path):
    db_url = _db_url(tmp_path)
    with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url}):
        _seed_sync(db_url, [
            _legacy_row("kept", tmp_path / "arch"),
            dict(id="gone", name="G", account_name="acc1", engine_session_id=None,
                 status="archived", cwd="/old/gone"),
        ])
        with _client(tmp_path) as client:
            # Only the "kept" dir fails; "gone" has no legacy dir and deletes cleanly.
            def rmtree(path: str):
                return _STALE if path.endswith("kept") else None
            with patch(_RMTREE, side_effect=rmtree):
                r = client.post("/api/sessions/batch/permanent-delete",
                                json={"ids": ["kept", "gone", "ghost"]}, headers=_IDENT)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "succeeded": ["gone"],
        "failed": [{"id": "ghost", "error": "not found"}],
        "pending": [{"id": "kept", "state": "pending"}],
    }


# ---------------------------------------------------------------------------
# S4 hand-off: archiving a session in a hold status resolves its open hold
# ---------------------------------------------------------------------------


def test_archiving_a_session_in_a_hold_status_resolves_the_hold(tmp_path):
    questions = [{
        "question_id": "q1", "turn_id": "t1", "header": "Pick", "question": "Which?",
        "options": [{"label": "A"}], "multi_select": False, "parent_tool_use_id": None,
    }]

    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([dict(id="s1", name="S1", account_name="acct1",
                          engine_session_id="claude-s1", status="idle", cwd="/repo/s1")])
        orch = _orch()
        await orch.rehydrate()
        orch._drivers["s1"] = types.SimpleNamespace(
            last_hand_back_reason=None, pending_questions=lambda: list(questions))
        try:
            await orch._persist_status("s1", "waiting_question")
            async with db_session() as db:
                before = list((await db.execute(
                    sa_select(SessionHold).where(SessionHold.session_id == "s1")
                )).scalars().all())
            await orch.archive_session("s1")
            await asyncio.gather(*getattr(orch, "_bg_tasks", ()), return_exceptions=True)
            async with db_session() as db:
                after = list((await db.execute(
                    sa_select(SessionHold).where(SessionHold.session_id == "s1")
                )).scalars().all())
            return ([h.resolved_at for h in before], [h.resolved_at for h in after],
                    dict(orch._open_hold_kind))
        finally:
            await orch.stop()

    before, after, open_kinds = asyncio.run(run())
    assert before == [None], "the question hold must be open before the archive"
    assert len(after) == 1 and after[0] is not None, "archive must resolve the hold"
    assert open_kinds == {}


# ---------------------------------------------------------------------------
# Delete state survives a daemon restart (FR-404 "never resurrects silently")
# ---------------------------------------------------------------------------


async def _record_retry(sid: str, *, strike: int, stuck: bool) -> None:
    await provenance.record(sid, provenance.DELETE_RETRY,
                            {"strike": strike, "error": _STALE, "stuck": stuck})


def test_rehydrate_seeds_pending_and_stuck_from_the_newest_delete_retry_event(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("p1", tmp_path / "arch"), _legacy_row("s1", tmp_path / "arch"),
                     _legacy_row("ok", tmp_path / "arch")])
        await _record_retry("p1", strike=1, stuck=False)
        await _record_retry("s1", strike=2, stuck=False)
        await _record_retry("s1", strike=3, stuck=True)          # newest wins
        await _record_retry("gone", strike=1, stuck=False)       # row no longer exists
        orch = _orch()                                           # a fresh process
        await orch.start()
        try:
            await orch.rehydrate()
            return (orch.delete_state("p1"), orch.delete_state("s1"), orch.delete_state("ok"),
                    orch.delete_state("gone"), dict(orch._archive_purge_strikes),
                    orch.pending_delete_states())
        finally:
            await orch.stop()

    p1, s1, ok, gone, strikes, states = asyncio.run(run())
    assert (p1, s1, ok, gone) == ("pending", "stuck", None, None)
    assert strikes == {"p1": 1, "s1": 3}
    assert states == {"p1": "pending", "s1": "stuck"}


def test_seeded_pending_delete_is_retried_and_cleared_by_the_loop_tick(tmp_path):
    async def run():
        await init_db(db_url=_db_url(tmp_path))
        await _seed([_legacy_row("p1", tmp_path / "arch")])
        await _record_retry("p1", strike=1, stuck=False)
        orch = _orch()
        await orch.rehydrate()
        healed = await orch._retry_pending_deletes()          # the dir removes fine now
        return healed, orch.delete_state("p1"), await _row("p1")

    healed, state, row = asyncio.run(run())
    assert healed == 1 and state is None and row is None

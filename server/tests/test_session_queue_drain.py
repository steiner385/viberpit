"""Queue drain: slot-free → head starts; failure path; restart rehydrate."""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

from server.db import init_db, dispose_db
from server.db.models import AppSettings
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    from viberpit import paths
    from viberpit.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


async def _make_orch(
    *,
    db_url: str,
    tmp_path,
    max_active_sessions: int,
    reuse_db: bool = False,
) -> SessionOrchestrator:
    """Build a minimal fake-SDK orchestrator with max_active_sessions set in AppSettings."""
    store_home = tmp_path / "store"
    store_home.mkdir(exist_ok=True)
    shared = tmp_path / "dot-claude"
    shared.mkdir(exist_ok=True)
    (shared / "settings.json").write_text("{}")
    (shared / "projects").mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    repo_root = tmp_path / "reporoot"
    repo_root.mkdir(exist_ok=True)

    env_patch = patch.dict(os.environ, {
        "VIBERPIT_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared),
        "VIBERPIT_DB_URL": db_url,
    })
    env_patch.start()

    _seed_account("alpha")

    await init_db(db_url=db_url)

    if not reuse_db:
        # Seed AppSettings with the requested cap (only on first creation)
        from server.db import db_session
        async with db_session() as db:
            db.add(AppSettings(id="singleton", max_active_sessions=max_active_sessions))

    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch(
        "server.sessions.orchestrator.select_account",
        return_value=__import__("ccswitch.selector", fromlist=["Picked"]).Picked("alpha"),
    )

    compose_patch.start()
    trust_patch.start()
    select_patch.start()

    orch = SessionOrchestrator(
        sdk_factory=fake_sdk_factory(),
        poll_interval=9999.0,
        usage_fn=_fake_usage,
        repo_root=str(repo_root),
    )
    await orch.start()

    # Store patches on orch for cleanup
    orch._test_patches = [env_patch, compose_patch, trust_patch, select_patch]  # type: ignore[attr-defined]
    return orch


async def _stop_orch(orch: SessionOrchestrator) -> None:
    await orch.stop()
    await dispose_db()
    for p in getattr(orch, "_test_patches", []):
        try:
            p.stop()
        except RuntimeError:
            pass


def test_close_drains_head_in_priority_order(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")     # queued pos 1
            c = await orch.create_session("three")   # queued pos 2
            from server.sessions import session_queue as sq
            await sq.move(c.id, "top")               # c overtakes b
            await orch.close_session(a.id)
            await orch._maybe_start_queued()         # deterministic in tests
            assert orch._records[c.id].status != "queued"   # c started
            assert orch._records[b.id].status == "queued"   # b still waits
            assert (await sq.positions()) == {b.id: 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_start_failure_ends_session_and_continues(tmp_path):
    """A failed-to-start session must be marked ended (not needs_attention).

    ended ∈ CAPACITY_EXEMPT_STATUSES, so it does not consume a cap slot and
    the drain loop can start the next queued entry within the cap on the very
    next iteration — no skip_capacity_check bypass required.
    """
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd2.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad")
            ok = await orch.create_session("ok")
            # Sabotage the head's start (monkeypatch _spawn_session to raise once
            # for bad.id — e.g. wrap the real method).
            real = orch._spawn_session
            async def flaky(session_id, **kw):
                if session_id == bad.id:
                    raise RuntimeError("insufficient disk headroom at /x")
                return await real(session_id, **kw)
            orch._spawn_session = flaky
            await orch.close_session(a.id)
            # The close fires a background drain task; let it complete then also
            # call the explicit drain to cover the "idempotent: empty queue → 0" path.
            # Yield to the event loop so the background task runs first.
            await asyncio.sleep(0)
            started = await orch._maybe_start_queued()
            # The background drain already processed bad (→ ended) and ok
            # (→ started), so the explicit call finds an empty queue and returns 0.
            # What matters: bad is ended, ok started, queue is empty.
            assert started == 0                                  # background task did the work
            assert orch._records[bad.id].status == "ended"       # failed start → ended, not needs_attention
            assert orch._records[ok.id].status != "queued"       # ok started within the cap
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {}                  # both rows gone from queue
            # Cap invariant: active count must never exceed max_active_sessions (1).
            # Call the production counter directly rather than re-deriving the cap
            # math here — a re-derivation using a stale/wrong status set would keep
            # passing silently as long as no fixture in this file reaches that
            # status, which is exactly what happened with the old
            # NON_ACTIVE_SESSION_STATUSES before CAPACITY_EXEMPT_STATUSES existed.
            active = orch._active_session_count()
            assert active <= 1, f"cap breached: {active} active sessions with max=1"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def _raise_branch_gone(branch: str):
    """Raise BranchSwitchError with a BranchRefUnresolvableError cause, exactly
    as _spawn_session produces when create_worktree finds no ref (issue #836)."""
    from viberpit import worktrees
    from server.sessions.orchestrator import BranchSwitchError
    try:
        raise worktrees.BranchRefUnresolvableError(
            f"git worktree add failed for {branch!r}: "
            f"fatal: invalid reference: {branch}")
    except worktrees.BranchRefUnresolvableError as exc:
        raise BranchSwitchError(str(exc)) from exc


def test_spawn_race_with_merged_pr_ends_benignly(tmp_path, caplog):
    """Queued session whose branch was auto-deleted when its PR merged (#836):
    the spawn failure is a benign completion — one INFO line, no traceback,
    ended with an 'obsolete' pr_resolution note."""
    import logging

    from server.github.pr_status import PullRequestInfo

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd6.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            gone = await orch.create_session(
                "gone", cwd=str(tmp_path), repo="steiner385/x", branch="feat-1")
            # The daemon's per-branch PR cache knows feat-1's PR merged.
            merged = PullRequestInfo(
                number=832, url="https://github.com/steiner385/x/pull/832",
                state="merged", checks="passing", title="landed", merged=True)
            orch._pr_poller.cached = (  # type: ignore[method-assign]
                lambda repo, branch: merged
                if (repo, branch) == ("steiner385/x", "feat-1") else None)

            real = orch._spawn_session
            async def flaky(session_id, **kw):
                if session_id == gone.id:
                    _raise_branch_gone("feat-1")
                return await real(session_id, **kw)
            orch._spawn_session = flaky

            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                await orch.close_session(a.id)
                await asyncio.sleep(0)
                await orch._maybe_start_queued()

            rec = orch._records[gone.id]
            assert rec.status == "ended"
            assert rec.pr_resolution == "obsolete: PR #832 merged before start"
            assert rec.pr_resolution_source == "queue-drain"
            # Persisted, not just in-memory.
            from sqlalchemy import select as sa_select
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = (await db.execute(
                    sa_select(DbSession).where(DbSession.id == gone.id)
                )).scalar_one()
            assert row.status == "ended"
            assert row.pr_resolution == "obsolete: PR #832 merged before start"
            # Benign: one INFO line, never the traceback-carrying warning.
            assert "session_queue.start_failed" not in caplog.text
            assert "session_queue.obsolete_branch_merged" in caplog.text
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_spawn_unresolvable_ref_without_merged_pr_keeps_error_path(tmp_path, caplog):
    """An unresolvable ref with NO merged PR behind it is a real problem —
    today's warning (with traceback) and plain 'ended' must be preserved."""
    import logging

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd7.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session(
                "bad", cwd=str(tmp_path), repo="steiner385/x", branch="feat-2")
            orch._pr_poller.cached = lambda repo, branch: None  # type: ignore[method-assign]

            real = orch._spawn_session
            async def flaky(session_id, **kw):
                if session_id == bad.id:
                    _raise_branch_gone("feat-2")
                return await real(session_id, **kw)
            orch._spawn_session = flaky

            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                await orch.close_session(a.id)
                await asyncio.sleep(0)
                await orch._maybe_start_queued()

            rec = orch._records[bad.id]
            assert rec.status == "ended"
            assert rec.pr_resolution is None
            assert "session_queue.start_failed" in caplog.text
            assert "session_queue.obsolete_branch_merged" not in caplog.text
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_rehydrate_preserves_queued_and_drains(tmp_path):
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd3.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")            # queued
            # Close 'a' without triggering drain (to simulate a clean closed state
            # before restart) — we just call close_session normally; the slot is freed
            # but b remains queued so the next rehydrate sees it.
            await orch.close_session(a.id)
        finally:
            await _stop_orch(orch)

        # Simulate daemon restart: fresh orchestrator over the SAME DB.
        orch2 = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                 max_active_sessions=1, reuse_db=True)
        try:
            await orch2.rehydrate()
            assert orch2._records[b.id].status == "queued"  # survived as queued
            await orch2._maybe_start_queued()
            assert orch2._records[b.id].status != "queued"  # drained into the free slot
        finally:
            await _stop_orch(orch2)

    asyncio.run(run())


def test_queued_status_not_touched_by_midturn_normalization(tmp_path):
    """rehydrate must NOT rewrite 'queued' to 'idle' (only working/in_progress/condensing)."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd4.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            b = await orch.create_session("two")  # queued
        finally:
            await _stop_orch(orch)

        # Second orchestrator over same DB
        orch2 = await _make_orch(db_url=db_url, tmp_path=tmp_path,
                                 max_active_sessions=1, reuse_db=True)
        try:
            await orch2.rehydrate()
            # 'queued' must not be normalized to 'idle'
            assert orch2._records[b.id].status == "queued"
        finally:
            await _stop_orch(orch2)

    asyncio.run(run())


async def _set_cap(n: int) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    async with db_session() as db:
        await db.execute(sa_update(AppSettings).values(max_active_sessions=n))


async def _row_status(session_id: str) -> str:
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        return (await db.execute(
            sa_select(DbSession.status).where(DbSession.id == session_id))).scalar_one()


def test_worker_drain_skips_entries_it_does_not_own(tmp_path):
    """Fleet incident 2026-09-04: the session_queue table is shared by every daemon,
    but _records is per-process (a worker's rehydrate loads only its own host's rows,
    and a session enqueued on the primary never reaches a worker's memory at all).
    The drain treated "no in-memory record" as "stale entry" and DELETED it — so a
    worker with a free local slot ate every queue entry the primary created, leaving
    'queued' rows the UI lists but nothing can move, cancel, or start ("Session …
    is not in the queue"). A daemon must leave entries it does not own untouched."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd8.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            await orch.create_session("one")
            b = await orch.create_session("two")   # queued on the primary: host=NULL
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {b.id: 1}

            # Same shared DB, seen from a WORKER daemon on host "rocky" that has a
            # free slot and no in-memory record of the primary's session.
            orch._records.pop(b.id)
            await _set_cap(2)
            with patch.dict(os.environ, {"VIBERPIT_DAEMON_ROLE": "worker",
                                         "VIBERPIT_HOST_NAME": "rocky"}):
                started = await orch._maybe_start_queued()
            assert started == 0
            assert (await sq.positions()) == {b.id: 1}, "worker must not delete a foreign entry"
            assert await _row_status(b.id) == "queued"
            assert b.id not in orch._drivers, "worker must not spawn a foreign entry"

            # The owner (primary) still drains it, even though its record is gone
            # from memory — the shared row + entry are the truth, not the process.
            started = await orch._maybe_start_queued()
            assert started == 1
            assert (await sq.positions()) == {}
            assert orch._records[b.id].status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_primary_drain_skips_entries_stamped_for_live_worker(tmp_path, monkeypatch):
    """Mirror image: the primary's _records holds EVERY row (fleet dashboard), so a
    worker-stamped queued entry looks perfectly drainable to it. It must skip that
    entry while the worker is enabled in the registry, and adopt it only once the
    worker has left the registry (same ownership rule as _owns_record)."""
    async def run():
        from sqlalchemy import update as sa_update
        from server.db import db_session
        from server.db.models import Host, Session as DbSession
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd9.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            await orch.create_session("one")
            b = await orch.create_session("two")   # queued
            # Pretend the dispatcher routed b to worker "worker-b". Pin this
            # daemon's own name too: the kernel hostname of the box running the
            # suite must never be mistaken for the foreign stamp.
            async with db_session() as db:
                db.add(Host(name="worker-b", endpoint="https://worker-b:9443", enabled=True))
                await db.execute(sa_update(DbSession).where(DbSession.id == b.id)
                                 .values(host="worker-b"))
            orch._records[b.id].host = "worker-b"
            from server.sessions import session_queue as sq
            await _set_cap(2)

            # The registry snapshot is cached for _OWNERSHIP_HOSTS_TTL_S; start()
            # may have taken one before the Host row above existed. Disable the
            # cache rather than clearing it: a background read in flight across a
            # clear can store its stale snapshot back afterwards (#2726).
            from server.sessions import orchestrator
            monkeypatch.setattr(orchestrator, "_OWNERSHIP_HOSTS_TTL_S", 0.0)
            with patch.dict(os.environ, {"VIBERPIT_HOST_NAME": "primary-a"}):
                started = await orch._maybe_start_queued()
                assert started == 0
                assert (await sq.positions()) == {b.id: 1}, "primary must not eat a worker's entry"
                assert await _row_status(b.id) == "queued"
                assert b.id not in orch._drivers

                # Worker leaves the registry → its rows are orphans the primary adopts.
                async with db_session() as db:
                    await db.execute(sa_update(Host).where(Host.name == "worker-b")
                                     .values(enabled=False))
                started = await orch._maybe_start_queued()
                assert started == 1
                assert (await sq.positions()) == {}
                assert orch._records[b.id].status != "queued"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_maybe_start_queued_tolerates_missing_lock_attribute(tmp_path):
    """_maybe_start_queued must not raise AttributeError if _queue_drain_lock is absent.

    Regression guard for issue #662: a live SessionOrchestrator created by an older
    code path (mid-session code update without daemon restart) won't have
    _queue_drain_lock set in __init__. Deleting the attribute here simulates that
    scenario; the method must lazy-init the lock and complete without error.
    """
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd5.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=2)
        try:
            # Simulate the old-instance state by removing the attribute after init.
            del orch._queue_drain_lock
            assert not hasattr(orch, "_queue_drain_lock"), "precondition: attr must be gone"

            # Must not raise — should lazy-init the lock and return 0 (empty queue).
            result = await orch._maybe_start_queued()
            assert result == 0
            # The lock should have been lazily created.
            assert hasattr(orch, "_queue_drain_lock")
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_drain_leaves_entry_whose_cwd_is_missing_on_this_host(tmp_path, caplog):
    """Issue #3516: a queued entry whose host-local cwd does not exist on the
    draining host must stay queued, never adopted, never marked ended, and never
    reach the spawn (which used to fail with 'Working directory does not exist'
    and end the session with a WARNING traceback)."""
    import logging

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd10.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            workdir = tmp_path / "moved-clone"
            workdir.mkdir()
            a = await orch.create_session("one")
            b = await orch.create_session("two", cwd=str(workdir))   # queued
            # The clone moves away after enqueue (namespace migration): the path
            # is only checked at enqueue time, so it is stale by drain time.
            workdir.rmdir()
            from server.sessions import session_queue as sq

            spawned: list[str] = []
            real = orch._spawn_session
            async def tracking(session_id, **kw):
                spawned.append(session_id)
                return await real(session_id, **kw)
            orch._spawn_session = tracking

            with caplog.at_level(logging.DEBUG, logger="server.sessions.orchestrator"):
                await orch.close_session(a.id)
                await asyncio.sleep(0)
                started = await orch._maybe_start_queued()

            assert started == 0
            assert b.id not in spawned, "a missing-cwd entry must not reach the spawn"
            assert (await sq.positions()) == {b.id: 1}, "entry must stay queued"
            assert await _row_status(b.id) == "queued"
            assert "session_queue.start_failed" not in caplog.text
            assert "session_queue.cwd_unserved id=" in caplog.text
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_cwd_vanishing_during_spawn_leaves_entry_queued(tmp_path, caplog):
    """The drain's cwd gate passes, but the directory is gone by the time the
    spawn runs. That is a refusal to start on this host, not a start failure: the
    entry stays queued (no ended row, no traceback warning)."""
    import logging

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'd11.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            workdir = tmp_path / "vanishing-clone"
            workdir.mkdir()
            a = await orch.create_session("one")
            b = await orch.create_session("two", cwd=str(workdir))   # queued
            from server.sessions import session_queue as sq

            real = orch._spawn_session
            async def vanishing(session_id, **kw):
                if session_id == b.id:
                    workdir.rmdir()
                    raise RuntimeError("Working directory does not exist")
                return await real(session_id, **kw)
            orch._spawn_session = vanishing

            with caplog.at_level(logging.INFO, logger="server.sessions.orchestrator"):
                await orch.close_session(a.id)
                await asyncio.sleep(0)
                await orch._maybe_start_queued()

            assert orch._records[b.id].status == "queued"
            assert await _row_status(b.id) == "queued"
            assert (await sq.positions()) == {b.id: 1}
            assert "session_queue.start_failed" not in caplog.text
            assert "session_queue.cwd_unserved_at_spawn" in caplog.text
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_start_failure_is_typed_on_the_record_row_and_timeline(tmp_path):
    """#947 G6: a permanent start failure ends the session WITH its typed reason —
    on the record (the card), the row (survives a restart) and the timeline."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'typed.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad")
            calls = 0
            real = orch._spawn_session

            async def broken(session_id, **kw):
                nonlocal calls
                if session_id == bad.id:
                    calls += 1
                    raise RuntimeError("insufficient disk headroom at /x")
                return await real(session_id, **kw)
            orch._spawn_session = broken
            await orch.close_session(a.id)
            for _ in range(5):
                if orch._records[bad.id].status == "ended":
                    break
                await orch._maybe_start_queued()
            assert calls == 1                        # permanent: never retried
            failure = orch._records[bad.id].last_spawn_failure
            assert failure["reason"] == "start_error"
            assert failure["transient"] is False and failure["requeued"] is False
            assert failure["message"] == "insufficient disk headroom at /x"
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = await db.get(DbSession, bad.id)
                assert row.last_spawn_failure == failure
            from server.sessions import provenance
            events = await provenance.list_events(bad.id)
            assert [e["kind"] for e in events] == [provenance.SPAWN_FAILED]
            assert events[0]["detail"] == failure
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_transient_start_failure_requeues_exactly_once(tmp_path):
    """#947 G6: a network failure says nothing about the session, so the drain puts
    it back ONCE (a later drain retries); a second failure ends it."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'transient.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad")
            calls = 0
            real = orch._spawn_session

            async def offline(session_id, **kw):
                nonlocal calls
                if session_id == bad.id:
                    calls += 1
                    raise RuntimeError("fatal: unable to access 'https://github.com/o/r.git/': "
                                       "Could not resolve host: github.com")
                return await real(session_id, **kw)
            orch._spawn_session = offline
            await orch.close_session(a.id)
            for _ in range(6):
                if orch._records[bad.id].status == "ended":
                    break
                await orch._maybe_start_queued()
            assert calls == 2                        # first try + exactly one retry
            assert orch._records[bad.id].status == "ended"
            failure = orch._records[bad.id].last_spawn_failure
            assert (failure["reason"], failure["transient"], failure["attempt"],
                    failure["requeued"]) == ("network", True, 2, False)
            from server.sessions import provenance
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {}
            events = await provenance.list_events(bad.id)        # newest first
            assert [(e["detail"]["attempt"], e["detail"]["requeued"]) for e in events] == [
                (2, False), (1, True)]
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


# --- credential preflight + rotated-account retry (FR-303/FR-304, #947) -------------

def _write_creds(cfg_dir, *, expired: bool) -> None:
    import json
    cfg_dir.mkdir(parents=True, exist_ok=True)
    blob = {"accessToken": "tok", "refreshToken": "ref",
            "expiresAt": int(time.time() * 1000) + 3_600_000}
    if expired:
        # Lapsed, and nothing to refresh it with: the CLI would start and 401.
        blob = {"accessToken": "tok", "expiresAt": 1}
    (cfg_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": blob}))


async def _events(session_id: str) -> list[dict]:
    from server.sessions import provenance
    return await provenance.list_events(session_id)       # newest first


def test_unpinned_preflight_failure_rotates_to_another_account(tmp_path):
    """An expired composed credential on the picked account is a typed, transient
    failure: the drain re-enqueues once with THAT account hard-excluded and records
    ROTATED_ACCOUNT, so the retry lands on a different account."""
    from server.sessions.selection import Picked

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'rotate.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            _seed_account("beta")
            excludes: list[frozenset] = []

            def picking(exclude=frozenset(), **kw):
                excludes.append(frozenset(exclude))
                return Picked("beta" if "alpha" in exclude else "alpha")

            with patch("server.sessions.orchestrator.select_account", side_effect=picking), \
                 patch("server.sessions.orchestrator.compose",
                       side_effect=lambda account: tmp_path / "cfg" / account):
                a = await orch.create_session("one")
                bad = await orch.create_session("bad")              # queued
                _write_creds(tmp_path / "cfg" / "alpha", expired=True)
                _write_creds(tmp_path / "cfg" / "beta", expired=False)
                await orch.close_session(a.id)
                for _ in range(6):
                    if orch._records[bad.id].status != "queued":
                        break
                    await orch._maybe_start_queued()

            rec = orch._records[bad.id]
            assert rec.status == "idle"
            assert rec.account == "beta"
            assert any("alpha" in ex for ex in excludes), excludes
            assert "alpha" in excludes[-1]
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {}
            events = await _events(bad.id)
            assert [e["kind"] for e in events] == ["rotated_account", "spawn_failed"]
            assert events[0]["detail"] == {"from": "alpha", "reason": "credentials_expired",
                                           "attempt": 1}
            failed = events[1]["detail"]
            assert (failed["reason"], failed["transient"], failed["requeued"],
                    failed["attempt"]) == ("credentials_expired", True, True, 1)
            # The retry succeeded: the failure is history, not the record's state.
            assert rec.last_spawn_failure is None
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_pinned_preflight_failure_ends_with_typed_reason(tmp_path):
    """The user chose this account; moving them silently is wrong. A pinned entry
    whose credentials fail preflight ends at once with the typed reason — no
    retry, no rotation event."""
    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'pinned.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad", account="alpha")   # queued, pinned
            calls = 0
            real = orch._spawn_session

            async def counting(session_id, **kw):
                nonlocal calls
                if session_id == bad.id:
                    calls += 1
                return await real(session_id, **kw)
            orch._spawn_session = counting
            _write_creds(tmp_path / "cfg" / "alpha", expired=True)
            await orch.close_session(a.id)
            for _ in range(6):
                if orch._records[bad.id].status == "ended":
                    break
                await orch._maybe_start_queued()

            assert calls == 1
            rec = orch._records[bad.id]
            assert rec.status == "ended"
            failure = rec.last_spawn_failure
            assert (failure["reason"], failure["transient"], failure["requeued"]) == (
                "credentials_expired", False, False)
            from server.sessions import session_queue as sq
            assert (await sq.positions()) == {}
            assert [e["kind"] for e in await _events(bad.id)] == ["spawn_failed"]
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_unpinned_start_timeout_retries_on_a_rotated_account(tmp_path):
    """FR-303: a start that overran its deadline is retried ONCE with the account it
    ran on hard-excluded from the pick."""
    from server.sessions import spawn_failure
    from server.sessions.orchestrator import DriverStartTimeout

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'timeout.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad")
            seen: list[frozenset] = []
            real = orch._spawn_session

            async def slow_once(session_id, **kw):
                if session_id == bad.id:
                    seen.append(kw["exclude_accounts"])
                    if len(seen) == 1:
                        # What the real spawn's rollback stamps before re-raising.
                        exc = DriverStartTimeout(session_id, 1.0)
                        spawn_failure.note_account(exc, "alpha")
                        raise exc
                return await real(session_id, **kw)
            orch._spawn_session = slow_once
            await orch.close_session(a.id)
            for _ in range(6):
                if orch._records[bad.id].status != "queued":
                    break
                await orch._maybe_start_queued()

            assert seen == [frozenset(), frozenset({"alpha"})]
            assert orch._records[bad.id].status == "idle"
            events = await _events(bad.id)
            assert [e["kind"] for e in events] == ["rotated_account", "spawn_failed"]
            assert events[0]["detail"] == {"from": "alpha", "reason": "start_timeout",
                                           "attempt": 1}
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_pinned_start_timeout_retries_the_same_account(tmp_path):
    """Pinned + start_timeout keeps today's behaviour: one more try on the SAME
    account, nothing excluded, no rotation event."""
    from server.sessions import spawn_failure
    from server.sessions.orchestrator import DriverStartTimeout

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'pinned-timeout.db'}"
        orch = await _make_orch(db_url=db_url, tmp_path=tmp_path, max_active_sessions=1)
        try:
            a = await orch.create_session("one")
            bad = await orch.create_session("bad", account="alpha")
            seen: list[tuple] = []
            real = orch._spawn_session

            async def slow_once(session_id, **kw):
                if session_id == bad.id:
                    seen.append((kw["account"], kw["exclude_accounts"]))
                    if len(seen) == 1:
                        exc = DriverStartTimeout(session_id, 1.0)
                        spawn_failure.note_account(exc, "alpha")
                        raise exc
                return await real(session_id, **kw)
            orch._spawn_session = slow_once
            await orch.close_session(a.id)
            for _ in range(6):
                if orch._records[bad.id].status != "queued":
                    break
                await orch._maybe_start_queued()

            assert seen == [("alpha", frozenset()), ("alpha", frozenset())]
            assert orch._records[bad.id].status == "idle"
            assert [e["kind"] for e in await _events(bad.id)] == ["spawn_failed"]
        finally:
            await _stop_orch(orch)

    asyncio.run(run())

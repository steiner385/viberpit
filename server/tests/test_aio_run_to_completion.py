"""Issue #2726: work that must not be interrupted finishes, THEN the cancel propagates.

The flake: ``test_reattach_reemits_pending_question`` failed with ``database is
locked`` on two unrelated writers (the answer audit INSERT and ``_persist_status``),
each after the full 30 s busy timeout. Neither held the lock. A third writer did:
``quick_replies.observe_reply`` runs on the WS prompt chain, and socket teardown
cancelled it while a statement of its open write transaction was in flight (8 of
12 local runs). SQLAlchemy invalidates a connection cancelled mid-statement, and
in the failing runs that abandoned handle still held the write lock 30 s later.
"""
import asyncio
import time

import pytest
from sqlalchemy import text

from server.aio import run_to_completion


@pytest.mark.asyncio
async def test_finishes_work_then_propagates_callers_cancel():
    finished = asyncio.Event()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    caller = asyncio.create_task(run_to_completion(work(), name="t"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert finished.is_set(), "the shielded work must run to completion"


@pytest.mark.asyncio
async def test_repeated_cancels_cannot_strand_the_work():
    finished = asyncio.Event()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.05)
        finished.set()

    caller = asyncio.create_task(run_to_completion(work(), name="t"))
    await started.wait()
    for _ in range(3):
        caller.cancel()
        await asyncio.sleep(0.005)
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert finished.is_set()


@pytest.mark.asyncio
async def test_returns_result_and_raises_work_error_when_not_cancelled():
    async def ok():
        return 7

    async def boom():
        raise ValueError("x")

    assert await run_to_completion(ok(), name="ok") == 7
    with pytest.raises(ValueError):
        await run_to_completion(boom(), name="boom")


@pytest.mark.asyncio
async def test_cancelled_quick_reply_write_does_not_strand_the_sqlite_lock(isolated_db):
    """The #2726 holder, made deterministic. The cancel lands exactly where CI's
    did: on the commit flush's UPDATE, after the profile INSERT already took the
    write lock. The write must still land, and another writer must then get the
    lock promptly. (Pre-fix, the sample is silently lost here; the lock stranding
    itself also needs the test harness's NullPool + GC timing to show.)"""
    from sqlalchemy import event

    import server.db as db_mod
    from server import quick_replies as qr
    from server.db import db_session
    from server.db.models import QuickReplyProfile

    caller: asyncio.Task | None = None

    def _cancel_on_flush(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().startswith("UPDATE quick_reply_profiles") and caller is not None:
            caller.cancel()  # lands on this UPDATE's in-flight aiosqlite future

    engine = db_mod._engine.sync_engine
    event.listen(engine, "before_cursor_execute", _cancel_on_flush)
    try:
        caller = asyncio.create_task(qr.observe_reply("alice@example.com", "continue"))
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        event.remove(engine, "before_cursor_execute", _cancel_on_flush)

    started = time.monotonic()
    async with db_session() as db:
        await db.execute(text("PRAGMA busy_timeout=3000"))
        await db.execute(text("UPDATE quick_reply_profiles SET replies = replies"))
    assert time.monotonic() - started < 2.0, "a cancelled writer kept the write lock"
    async with db_session() as db:
        row = await db.get(QuickReplyProfile, "alice@example.com")
    assert row is not None and [s["text"] for s in row.samples] == ["continue"]

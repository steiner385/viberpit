"""A reaped queue orphan must replay its initial prompt, not discard it (#1100).

The live loss (2026-09-04, host ``dobby``): 16 backlog dispatches were reaped in one
day.  A ``queued`` sessions row whose ``session_queue`` entry was lost got marked
``ended`` by :meth:`SessionOrchestrator._reconcile_orphan_queued`, throwing away an
intact ``initial_prompt``.  The card then read as a normal idle session with an empty
conversation, the user typed ``continue`` into it, and the model had no idea what work
it was continuing — while the backlog runner escalated the issue to ``needs-human``
for a session that never ran a single turn.

Three windows are pinned here:

1. **Replay, don't discard** — an orphan with a non-null ``initial_prompt`` and a NULL
   ``engine_session_id`` (proof no turn ever ran, the same durable evidence
   ``_recover_undelivered_prompts`` relies on) is re-enqueued from the row rather than
   ended.
2. **Ordering** — ``_maybe_start_queued``'s failure path persists ``ended`` BEFORE it
   drops the queue entry, so a kill in the window leaves a replayable state (row still
   ``queued``, entry intact) instead of manufacturing the orphan this bug is about.
3. **No escalation for a session that never ran** — the backlog runner releases the
   claim instead of stamping ``needs-human`` when its session recorded no
   ``engine_session_id``.
"""
from __future__ import annotations

import asyncio

from server.db.models import Session as DbSession
from server.db.models import SessionQueue


async def _seed_orphan(db_url: str | None = None, *, session_id: str,
                       initial_prompt: str | None = "Implement the following GitHub issue",
                       engine_session_id: str | None = None,
                       status: str = "queued", **over) -> None:
    """A 'queued' row with NO session_queue entry — the orphan shape."""
    from server.db import db_session, init_db
    if db_url is not None:
        await init_db(db_url=db_url)
    values = dict(
        id=session_id, name="backlog acme/repo#1", account_name="acct",
        engine_session_id=engine_session_id, status=status, pid=None,
        cwd="/tmp/clone", repo="acme/repo", branch=None,
        permission_mode="full-auto", name_is_auto=True,
        origin="backlog-runner", origin_ref="acme/repo#1", model="opus",
        initial_prompt=initial_prompt)
    values.update(over)
    async with db_session() as db:
        db.add(DbSession(**values))


async def _queue_params(session_id: str) -> dict | None:
    from sqlalchemy import select

    from server.db import db_session
    async with db_session() as db:
        row = (await db.execute(select(SessionQueue).where(
            SessionQueue.session_id == session_id))).scalar_one_or_none()
    return dict(row.params) if row is not None else None


async def _status(session_id: str) -> str:
    from sqlalchemy import select

    from server.db import db_session
    async with db_session() as db:
        return (await db.execute(select(DbSession.status).where(
            DbSession.id == session_id))).scalar_one()


# ---------------------------------------------------------------------------
# 1. Replay instead of discard
# ---------------------------------------------------------------------------

def test_orphan_with_an_undelivered_prompt_is_requeued_not_ended(tmp_path):
    """The whole bug: an intact prompt + a never-run session must be replayed."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}",
                           session_id="orphan-replay")

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 1

        assert await _status("orphan-replay") == "queued", (
            "the row was ended — its 838-byte initial_prompt is now unreachable and "
            "the backlog item is silently lost")
        params = await _queue_params("orphan-replay")
        assert params is not None, "no queue entry — the drain still cannot see the row"
        assert params["initial_prompt"] == "Implement the following GitHub issue"
        # Rebuilt from the row, so the replayed spawn lands in the right clone with
        # the right account/model/permission mode rather than guessed defaults.
        assert params["cwd"] == "/tmp/clone"
        assert params["repo"] == "acme/repo"
        assert params["account"] == "acct"
        assert params["model"] == "opus"
        assert params["permission_mode"] == "full-auto"
        assert params["origin"] == "backlog-runner"
        assert params["origin_ref"] == "acme/repo#1"
        assert params["attachment_ids"] is None  # documented, unrecoverable

    asyncio.run(run())


def test_orphan_that_already_ran_a_turn_is_still_ended(tmp_path):
    """A non-NULL engine_session_id proves turn one ran — replay would duplicate work."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'ran.db'}",
                           session_id="orphan-ran", engine_session_id="cs-123")

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 1
        assert await _status("orphan-ran") == "ended"
        assert await _queue_params("orphan-ran") is None

    asyncio.run(run())


def test_orphan_without_a_prompt_is_still_ended(tmp_path):
    """Nothing to replay → the last-resort reap stands."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'noprompt.db'}",
                           session_id="orphan-bare", initial_prompt="   ")

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 1
        assert await _status("orphan-bare") == "ended"
        assert await _queue_params("orphan-bare") is None

    asyncio.run(run())


def test_requeued_orphan_is_not_reprocessed_on_the_next_pass(tmp_path):
    """Idempotence: a replayed row now HAS an entry, so it is no longer an orphan."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}",
                           session_id="orphan-idem")

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 1
        assert await orch._reconcile_orphan_queued() == 0

        from sqlalchemy import select

        from server.db import db_session
        async with db_session() as db:
            rows = (await db.execute(select(SessionQueue).where(
                SessionQueue.session_id == "orphan-idem"))).scalars().all()
        assert len(rows) == 1, "reconcile double-enqueued the same session"

    asyncio.run(run())


def test_requeued_orphan_can_actually_be_drained(tmp_path):
    """End to end: the rebuilt params must satisfy _spawn_session's signature.

    A params blob that reconstructs the wrong keys would blow up in the drain's
    ``_spawn_session(sid, preexisting_row=True, **params)`` splat — which the drain
    turns into 'ended', i.e. exactly the loss this fix removes, one restart later.
    """
    async def run():
        import inspect

        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'drain.db'}",
                           session_id="orphan-drain")

        orch = SessionOrchestrator()
        await orch._reconcile_orphan_queued()

        seen: dict = {}

        async def _spawn(sid, **kwargs):
            # Bind against the REAL signature so a stale/extra key fails loudly here.
            inspect.signature(SessionOrchestrator._spawn_session).bind(
                orch, sid, **kwargs)
            seen.update(kwargs)
            seen["sid"] = sid

        orch._spawn_session = _spawn  # type: ignore[method-assign]
        started = await orch._maybe_start_queued()

        assert started == 1, "the replayed entry never drained"
        assert seen["sid"] == "orphan-drain"
        assert seen["initial_prompt"] == "Implement the following GitHub issue"

    asyncio.run(run())


def test_assistant_orphan_is_never_replayed(tmp_path):
    """Mirrors _recover_undelivered_prompts: an assistant thread is never re-driven."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'asst.db'}",
                           session_id="orphan-asst", kind="assistant", origin=None,
                           origin_ref=None)

        orch = SessionOrchestrator()
        assert await orch._reconcile_orphan_queued() == 1
        assert await _status("orphan-asst") == "ended"
        assert await _queue_params("orphan-asst") is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. The failure path must persist 'ended' before dropping the entry
# ---------------------------------------------------------------------------

def test_start_failure_persists_ended_before_removing_the_queue_entry(tmp_path):
    """Non-atomic two-step: a kill between remove() and UPDATE manufactures the orphan.

    Ordering the DB write first means a kill in the window leaves a 'queued' row WITH
    its entry — replayable — instead of a 'queued' row with none.
    """
    async def run():
        from server.db import init_db
        from server.sessions import session_queue as sq
        from server.sessions.orchestrator import SessionOrchestrator
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'order.db'}")
        await _seed_orphan(session_id="fail-order")
        await sq.enqueue("fail-order", {
            "name": "backlog acme/repo#1", "account": "acct",
            "initial_prompt": "do it", "cols": 80, "rows": 24, "cwd": "/tmp/clone",
            "branch": None, "repo": "acme/repo", "identity": "system",
            "permission_mode": "full-auto", "attachment_ids": None,
            "name_is_auto": True, "origin": "backlog-runner",
            "origin_ref": "acme/repo#1", "model": "opus"})

        orch = SessionOrchestrator()
        status_when_entry_dropped: list[str] = []
        real_remove = sq.remove

        async def _remove(session_id):
            status_when_entry_dropped.append(await _status(session_id))
            return await real_remove(session_id)

        async def _boom(*a, **k):
            raise RuntimeError("boom")

        orch._spawn_session = _boom  # type: ignore[method-assign]
        sq.remove = _remove  # type: ignore[assignment]
        try:
            await orch._maybe_start_queued()
        finally:
            sq.remove = real_remove  # type: ignore[assignment]

        assert status_when_entry_dropped == ["ended"], (
            "the queue entry was dropped while the row still read 'queued' — a kill "
            "in that window strands the session as an unreplayable orphan")
        assert await sq.positions() == {}

    asyncio.run(run())


def _orphan_shaped_params() -> dict:
    return {
        "name": "backlog acme/repo#1", "account": "acct",
        "initial_prompt": "do it", "cols": 80, "rows": 24, "cwd": "/tmp/clone",
        "branch": None, "repo": "acme/repo", "identity": "system",
        "permission_mode": "full-auto", "attachment_ids": None,
        "name_is_auto": True, "origin": "backlog-runner",
        "origin_ref": "acme/repo#1", "model": "opus"}


async def _status_when_entry_dropped(orch_call, session_id: str) -> list[str]:
    """Run *orch_call* with sq.remove instrumented; return the row status at removal."""
    from server.sessions import session_queue as sq
    seen: list[str] = []
    real_remove = sq.remove

    async def _remove(sid):
        seen.append(await _status(sid))
        return await real_remove(sid)

    sq.remove = _remove  # type: ignore[assignment]
    try:
        await orch_call()
    finally:
        sq.remove = real_remove  # type: ignore[assignment]
    return seen


def test_close_persists_ended_before_removing_the_queue_entry(tmp_path):
    """Sibling of the drain's ordering bug: a deliberate close must not look replayable.

    Removing the entry first leaves a 'queued' row with no entry if the daemon dies
    in the window — indistinguishable from the crash-orphan the reaper now REPLAYS,
    so a session the user explicitly closed would come back and run.
    """
    async def run():
        from server.db import init_db
        from server.sessions import session_queue as sq
        from server.sessions.orchestrator import SessionOrchestrator
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'close.db'}")
        await _seed_orphan(session_id="close-1")
        await sq.enqueue("close-1", _orphan_shaped_params())

        orch = SessionOrchestrator()
        seen = await _status_when_entry_dropped(
            lambda: orch.close_session("close-1"), "close-1")

        assert seen == ["ended"], (
            "the queue entry was dropped while the row still read 'queued' — a kill "
            "in that window leaves a closed session the reaper would replay")

    asyncio.run(run())


def test_archive_persists_archived_before_removing_the_queue_entry(tmp_path):
    """Same window on the archive path."""
    async def run():
        from server.db import init_db
        from server.sessions import session_queue as sq
        from server.sessions.orchestrator import SessionOrchestrator
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'arch.db'}")
        await _seed_orphan(session_id="arch-1")
        await sq.enqueue("arch-1", _orphan_shaped_params())

        orch = SessionOrchestrator()
        seen = await _status_when_entry_dropped(
            lambda: orch.archive_session("arch-1"), "arch-1")

        assert seen == ["archived"]

    asyncio.run(run())


def test_queue_cancel_persists_ended_before_removing_the_queue_entry(tmp_path):
    """Same window on the explicit cancel path, and it still reports success/failure."""
    async def run():
        from server.db import init_db
        from server.sessions import session_queue as sq
        from server.sessions.orchestrator import SessionOrchestrator
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
        await _seed_orphan(session_id="cancel-1")
        await sq.enqueue("cancel-1", _orphan_shaped_params())

        orch = SessionOrchestrator()
        out: list[bool] = []

        async def _cancel():
            out.append(await orch.session_queue_cancel("cancel-1"))

        seen = await _status_when_entry_dropped(_cancel, "cancel-1")

        assert seen == ["ended"]
        assert out == [True]
        assert await sq.positions() == {}
        # A session that was never queued is still reported as a miss (API 404).
        assert await orch.session_queue_cancel("cancel-1") is False

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. The runner must not escalate a session that never ran a turn
# ---------------------------------------------------------------------------

def _runner(*, ran: bool | None, calls: dict):
    """A BacklogRunnerLoop wired with stub seams, tracking release/needs-human."""
    from server.automations.backlog_runner import BacklogRunnerLoop

    class _Ctx:
        async def emit(self, *a, **k):
            calls.setdefault("emits", []).append(k.get("kind"))

        async def delete_target_state(self, target):
            calls["deleted"] = target

        async def set_target_state(self, target, st):
            calls["state"] = st

    async def _release(target):
        calls["released"] = target

    async def _needs_human(target):
        calls["needs_human"] = target

    async def _pr_status_for(_target):
        return None

    async def _session_ended(_sid):
        return True

    async def _unused(*a, **k):  # seams the sweep never reaches
        raise AssertionError("refill seam called from a sweep test")

    async def _pr_doctor_active(_repo):
        return False

    kwargs: dict = {}
    if ran is not None:
        async def _session_ran(_sid):
            return ran
        kwargs["session_ran"] = _session_ran

    return BacklogRunnerLoop(
        _Ctx(),  # type: ignore[arg-type]  # only ctx.emit / *_target_state are reached
        select_batch=_unused, claim=_unused, spawn=_unused,
        count_in_flight=_unused, healthy_account=_unused,
        pr_status_for=_pr_status_for, pr_doctor_active=_pr_doctor_active,
        read_config=_unused, clock=lambda: 0.0, release=_release,
        needs_human=_needs_human, session_ended=_session_ended, **kwargs)


def test_give_up_releases_instead_of_escalating_when_the_session_never_ran():
    """A session with no engine_session_id never ran — needs-human parks it for days."""
    async def run():
        calls: dict = {}
        loop = _runner(ran=False, calls=calls)
        await loop._sweep_one("acme/repo#1", {"session_id": "s1", "attempts": 2},
                              {"max_attempts": 3}, set())

        assert "needs_human" not in calls, (
            "escalated an issue whose session never ran a single turn — it is now "
            "parked for needs_human_retry_days for no reason")
        assert calls.get("released") == "acme/repo#1"
        assert calls.get("deleted") == "acme/repo#1"

    asyncio.run(run())


def test_give_up_still_escalates_when_the_session_actually_ran():
    """The normal give-up path is untouched: a real attempt still reaches a human."""
    async def run():
        calls: dict = {}
        loop = _runner(ran=True, calls=calls)
        await loop._sweep_one("acme/repo#2", {"session_id": "s2", "attempts": 2},
                              {"max_attempts": 3}, set())

        assert calls.get("needs_human") == "acme/repo#2"
        assert "released" not in calls

    asyncio.run(run())


def test_give_up_escalates_when_no_ran_probe_is_wired():
    """Unknown → escalate: the probe must never silently disable the give-up path."""
    async def run():
        calls: dict = {}
        loop = _runner(ran=None, calls=calls)
        await loop._sweep_one("acme/repo#3", {"session_id": "s3", "attempts": 2},
                              {"max_attempts": 3}, set())

        assert calls.get("needs_human") == "acme/repo#3"

    asyncio.run(run())


def test_backlog_session_ran_reads_the_engine_session_id(tmp_path):
    """The probe's truth source: a NULL engine_session_id means no turn ever ran."""
    async def run():
        from server.sessions.orchestrator import SessionOrchestrator
        await _seed_orphan(f"sqlite+aiosqlite:///{tmp_path / 'ran_probe.db'}",
                           session_id="never-ran", status="ended")
        await _seed_orphan(session_id="did-run", status="ended",
                           engine_session_id="cs-9")

        orch = SessionOrchestrator()
        assert await orch._backlog_session_ran("never-ran") is False
        assert await orch._backlog_session_ran("did-run") is True
        # A vanished row cannot prove a session never ran — fail safe (escalate).
        assert await orch._backlog_session_ran("gone") is True

    asyncio.run(run())

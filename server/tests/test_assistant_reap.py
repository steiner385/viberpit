"""Task 1.8: idle reap with resume-on-reopen + activity tracking.

An assistant holds a live `claude` process for as long as its driver exists, and the
drawer has no "close" event — a user who opens it once and never comes back would pin a
process (and its account quota) forever. The reap closes the DRIVER while keeping the
ROW: `find_assistant` still returns it and `claude_session_id` still points at the CLI
transcript, so the next `POST /api/assistant/session` resumes the same thread (spec §4.7,
"Reopen after reap | same row | respawned with resume= | continues").

That is exactly why the reap does NOT go through `close_session`: that path pops the
record out of `_records` (orchestrator.py, `record = self._records.pop(...)`) and deletes
the session's cwd when it is a managed worktree/scratch dir — the assistant's cwd is the
identity's persistent scratch dir, and a popped record is an unresumable thread.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from server.db import dispose_db, init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.assistant.token import verify_token
from server.sessions.types import ASSISTANT_KIND
from server.tests.fakes.fake_factory import fake_sdk_factory

# The orchestrator harness (fake SDK, fake accounts, patched compose/trust/select) is
# the one from Task 1.5's tests — reused rather than re-derived so the two assistant
# suites cannot drift.
from server.tests.test_assistant_orchestrator import _make_orch, _stop_orch


def _token_of(api) -> str:
    """The `ccas1:` token lives only in the client's Authorization header."""
    return api._client.headers["Authorization"]


async def _new_assistant(orch, tmp_path, identity: str = "u@x"):
    return await orch.create_session(name="Assistant", cwd=str(tmp_path),
                                     identity=identity, kind=ASSISTANT_KIND,
                                     owner_identity=identity)


def test_reap_after_idle_keeps_row_and_resume_id(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")  # get_settings() is uncached
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'r.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            assert t0 is not None

            assert await orch._reap_idle_assistants_once(now=t0 + 50) == []
            assert a.id in orch._drivers

            assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]
            rec = orch.get_session(a.id)
            assert rec is not None                       # the row survives the reap
            assert rec.status == "ended" and rec.detached is True
            assert a.id not in orch._drivers
            # The thread is still findable and still carries its resume handle.
            assert (await orch.find_assistant("u@x")).id == a.id
            assert orch.get_session(a.id).claude_session_id is not None
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_persists_ended_to_the_row(tmp_path, monkeypatch):
    """`POST /api/assistant/session` branches on `status == "ended"` to pick the resume
    path, and rehydrate rebuilds records from the DB — an in-memory-only flip would come
    back as a live-looking session with no process after a daemon restart."""
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]

            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                row = await db.get(DbSession, a.id)
                assert row.status == "ended"
                assert row.kind == ASSISTANT_KIND
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_skips_live_turns_and_non_assistants(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            coder = await orch.create_session(name="Coder", cwd=str(tmp_path))
            orch.note_user_interaction(a.id)
            orch.note_user_interaction(coder.id)
            t0 = orch.get_session(a.id).last_activity_at

            # A coding session idle for a week is none of the reap's business: it is
            # the user's work, not a drawer nobody closed.
            assert await orch._reap_idle_assistants_once(now=t0 + 999_999) == [a.id]
            assert coder.id in orch._drivers
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_leaves_an_assistant_mid_turn_alone(tmp_path, monkeypatch):
    """Idle is measured from the last USER signal, so a long tool-running turn with no
    typing looks idle. Killing the driver there would drop the answer the user is
    waiting for."""
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            with patch.object(type(orch._drivers[a.id]), "_turn_live",
                              return_value=True):
                assert await orch._reap_idle_assistants_once(now=t0 + 999) == []
            assert a.id in orch._drivers
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reaped_assistant_resumes_with_its_overlay_and_a_fresh_token(tmp_path, monkeypatch):
    """The reap's whole point is that reopening resumes the thread — and a resumed
    assistant must come back as an ASSISTANT: system prompt, `ccswitch` MCP server, every
    builtin tool disabled. `ensure_driver` builds the driver from the record alone, so
    without re-registering the overlay the reopened drawer would be an unconstrained
    agent with Bash on the operator's machine (spec §4.2/§5), and its `ccas1:` token —
    minted per spawn — must be a new one."""
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'o.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            first_overlay = orch._assistant_overlays[a.id]
            first_api = orch._drivers[a.id]._assistant_api
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            await orch._reap_idle_assistants_once(now=t0 + 101)
            assert a.id not in orch._assistant_overlays   # the dead MCP server is released

            driver = await orch.ensure_driver(a.id, identity="u@x")
            assert driver is not None
            overlay = orch._assistant_overlays[a.id]
            assert overlay is not first_overlay
            assert overlay["tools"] == [] and "Bash" in overlay["disallowed_tools"]
            assert set(overlay["mcp_servers"]) == {"ccswitch"}
            # A NEW client, freshly minted: the reap closed the old one, and reusing a
            # closed httpx client would fail every tool call the resumed thread makes.
            # (The signature itself can be byte-identical — `TimestampSigner` has
            # one-second resolution — so identity of the client is what's checked.)
            api = driver._assistant_api
            assert api is not None and api is not first_api
            token = _token_of(api).removeprefix("Bearer ")
            claims = verify_token(token, max_age_s=3600)
            assert claims is not None and claims.session_id == a.id
            assert api._client.headers["X-CCSwitch-Assistant-Session"] == a.id
            # And the clock restarts, so the resumed thread is not reaped on the next
            # sweep for being 15 minutes "old".
            assert orch.get_session(a.id).last_activity_at is not None
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reopened_assistant_leaves_ended_and_can_be_reaped_again(tmp_path, monkeypatch):
    """Reopening must be repeatable, not one-shot. `_persist_status` refuses to leave a
    terminal `ended`, so a resumed assistant that kept that status would have its driver's
    status callbacks dropped for the life of the process AND be skipped by every later
    sweep — the second abandoned drawer would pin its `claude` process forever."""
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'c.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            orch.note_user_interaction(a.id)
            t0 = orch.get_session(a.id).last_activity_at
            assert await orch._reap_idle_assistants_once(now=t0 + 101) == [a.id]

            await orch.ensure_driver(a.id, identity="u@x")
            assert orch.get_session(a.id).status == "done"   # live again, not terminal
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                assert (await db.get(DbSession, a.id)).status == "done"

            t1 = orch.get_session(a.id).last_activity_at
            assert await orch._reap_idle_assistants_once(now=t1 + 101) == [a.id]
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_every_driver_rebuild_rearms_an_assistant(tmp_path):
    """Sibling of the resume gap, found by sweeping for the same shape: `_reassign`
    (usage-ceiling rotation / manual account switch — spec §4.7 lists it as a supported
    assistant lifecycle event) also rebuilds the driver for an existing record. Leaving
    the OLD overlay registered is worse than losing it: its `ccswitch` tools are bound to
    the loopback client `old_driver.aclose()` just closed, so the rotated assistant looks
    healthy and fails every tool call."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            first_api = orch._drivers[a.id]._assistant_api
            first_overlay = orch._assistant_overlays[a.id]

            await orch._reassign(orch.get_session(a.id), "alpha", reason="usage_limit")

            driver = orch._drivers[a.id]
            assert driver._assistant_api is not None
            assert driver._assistant_api is not first_api
            assert orch._assistant_overlays[a.id] is not first_overlay
            assert orch._assistant_overlays[a.id]["tools"] == []
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_does_not_touch_a_normal_session_resume(tmp_path):
    """A coding session resumed through the same `ensure_driver` gets NO overlay and no
    assistant client — the assistant branch must be kind-gated, not universal."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'n.db'}",
                                tmp_path=tmp_path)
        try:
            coder = await orch.create_session(name="Coder", cwd=str(tmp_path))
            drv = orch._drivers.pop(coder.id)
            await drv.aclose()
            resumed = await orch.ensure_driver(coder.id, identity="u@x")
            assert resumed is not None
            assert coder.id not in orch._assistant_overlays
            assert resumed._assistant_api is None
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


@pytest.mark.asyncio
async def test_reap_loop_is_started_and_stopped(tmp_path, monkeypatch):
    """The loop must live in `start()`/`stop()` like every other sweep
    (test_orchestrator_stop_completeness pins the general rule by task name)."""
    monkeypatch.setenv("CCSWITCH_DB_URL", f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    await init_db(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    try:
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        await orch.start()
        task = orch._assistant_reap_task
        assert task is not None and not task.done()
        await orch.stop()
        assert orch._assistant_reap_task is None and task.done()
    finally:
        await dispose_db()


@pytest.mark.asyncio
async def test_reap_loop_survives_a_failing_sweep(tmp_path):
    """One bad sweep must not silently end the loop for the process's lifetime."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    calls: list[int] = []

    async def boom(*, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("sweep exploded")

    orch._shutting_down = False
    orch._reap_idle_assistants_once = boom  # type: ignore[method-assign]
    task = asyncio.create_task(orch._assistant_reap_loop(interval=0.0))
    for _ in range(50):
        await asyncio.sleep(0)
        if len(calls) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 3


def test_activity_frame_bumps_last_activity(tmp_path):
    """`note_user_interaction` is the ONE activity signal — the WS `activity` frame (sent
    every 5 s while a session is on screen) and every prompt/answer already call it, so
    the reap rides existing plumbing instead of adding an attached-clients counter."""
    import inspect

    from server.bridge import ws

    src = inspect.getsource(ws)
    assert '"activity"' in src and "note_user_interaction" in src

    stamp = inspect.getsource(SessionOrchestrator.note_user_interaction)
    assert "last_activity_at" in stamp and "monotonic" in stamp

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'a.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            before = orch.get_session(a.id).last_activity_at
            assert before is not None            # stamped at spawn, not only on input
            time.sleep(0.01)
            orch.note_user_interaction(a.id)
            assert orch.get_session(a.id).last_activity_at > before
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_starts_the_clock_for_an_unstamped_assistant(tmp_path, monkeypatch):
    """A record rebuilt by rehydrate has no in-memory activity stamp (it is deliberately
    not persisted — a 15-minute window means nothing across a restart). The sweep starts
    its clock instead of reaping it instantly."""
    async def run():
        monkeypatch.setenv("CCSWITCH_ASSISTANT_IDLE_S", "100")
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'u.db'}",
                                tmp_path=tmp_path)
        try:
            a = await _new_assistant(orch, tmp_path)
            orch.get_session(a.id).last_activity_at = None
            assert await orch._reap_idle_assistants_once(now=1000.0) == []
            assert orch.get_session(a.id).last_activity_at == 1000.0
            assert await orch._reap_idle_assistants_once(now=1101.0) == [a.id]
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


def test_reap_module_uses_the_shared_kind_predicate():
    """No open-coded `kind == "assistant"` in the sweep (test_assistant_loop_guard pins
    the same rule repo-wide); the sweep is also a session loop, so it must be guarded."""
    import inspect

    src = inspect.getsource(SessionOrchestrator._reap_idle_assistants_once)
    assert "is_assistant(" in src

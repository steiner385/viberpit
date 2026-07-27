"""A completed backlog-runner session auto-archives itself.

Spawned sessions never reach a terminal DB state on their own, so a finished run
kept its live driver/worktree and (with target-state counting) held its slot
until the sweep freed it. When the agent signals completion — the derived status
becomes ``complete`` (see status.derive_status) — the orchestrator should archive
the session: stop the driver, release the worktree, and let the sweep reclaim the
slot. Only backlog-runner sessions, only on ``complete`` (not a plain ``done``
turn), and only once.
"""
from __future__ import annotations

import asyncio

from server.sessions.types import SessionRecord


def _rec(sid="s1", *, origin="backlog-runner"):
    r = SessionRecord(id=sid, name="run", account="a", claude_session_id="",
                      cols=80, rows=24, cwd="/tmp", repo="o/r", branch="feat/x")
    r.origin = origin
    return r


def _orch(records):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records
    orch._auto_archiving = set()
    orch._backlog_ctx = AutomationContext("backlog-runner")
    archived: list[str] = []

    async def _spy_archive(session_id, *, archived_by="user", context=None):
        archived.append(session_id)

    orch.archive_session = _spy_archive  # type: ignore[method-assign]
    orch.pr_for = lambda repo, branch: None  # type: ignore[method-assign]
    return orch, archived


def test_completed_backlog_session_is_archived():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        await asyncio.sleep(0)  # let the scheduled task run
        await asyncio.sleep(0)
        assert archived == ["s1"]

    asyncio.run(run())


def test_plain_done_turn_is_not_archived():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch._maybe_autoarchive_completed("s1", "done", orch._records["s1"])
        await asyncio.sleep(0)
        assert archived == []  # only an explicit completion signal archives

    asyncio.run(run())


def test_human_session_is_not_archived():
    async def run():
        orch, archived = _orch({"s1": _rec("s1", origin=None)})
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        await asyncio.sleep(0)
        assert archived == []  # human-created sessions are never auto-archived

    asyncio.run(run())


def test_manually_unarchived_backlog_session_is_not_re_archived():
    """A human unarchived this run (sticky opt-out) → completion must NOT re-archive it."""
    async def run():
        rec = _rec("s1")
        rec.auto_archive_opt_out_reason = "manual-unarchive"
        orch, archived = _orch({"s1": rec})
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert archived == []  # the sticky opt-out wins over the completion signal

    asyncio.run(run())


def test_double_complete_archives_once():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert archived == ["s1"]  # idempotent guard

    asyncio.run(run())


# --- Part 2: archive_context captures pr_state ---

def _orch_with_pr(records, pr_info=None):
    """Like _orch() but also stubs pr_for so _autopilot_pr_facts can return known data."""
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records
    orch._auto_archiving = set()
    orch._backlog_ctx = AutomationContext("backlog-runner")

    archived_calls: list[dict] = []

    async def _spy_archive(session_id, *, archived_by="user", context=None):
        archived_calls.append({"session_id": session_id, "archived_by": archived_by,
                                "context": context})

    orch.archive_session = _spy_archive  # type: ignore[method-assign]
    orch.pr_for = lambda repo, branch: pr_info  # type: ignore[method-assign]
    return orch, archived_calls


def test_completed_backlog_archive_context_includes_pr_state_no_pr():
    """When no PR exists, archive_context has pr_state with has_repo=True, prs=[]."""
    async def run():
        rec = _rec("s1")
        # rec.repo = "o/r", rec.branch = "feat/x"
        orch, calls = _orch_with_pr({"s1": rec}, pr_info=None)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        ctx = calls[0]["context"]
        assert "pr_state" in ctx
        assert ctx["pr_state"]["has_repo"] is True
        assert ctx["pr_state"]["prs"] == []

    asyncio.run(run())


def test_completed_backlog_archive_context_includes_pr_state_with_pr():
    """When a PR exists on the branch, archive_context pr_state reflects it."""
    import types
    async def run():
        rec = _rec("s1")
        # Build a minimal PullRequestInfo-like object that _autopilot_pr_facts can use
        pr_info = types.SimpleNamespace(
            number=42, state="open", is_draft=False, auto_merge_enabled=True,
            in_merge_queue=False, checks="passing",
            url="https://github.com/o/r/pull/42")
        orch, calls = _orch_with_pr({"s1": rec}, pr_info=pr_info)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(calls) == 1
        ctx = calls[0]["context"]
        assert "pr_state" in ctx
        prs = ctx["pr_state"]["prs"]
        assert len(prs) == 1
        assert prs[0]["number"] == 42

    asyncio.run(run())


def test_session_ended_treats_archived_as_terminal(tmp_path):
    """The sweep's session-ended probe must see an auto-archived session as ended
    (INACTIVE_SESSION_STATUSES), so its give-up / attempt bookkeeping fires."""
    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ended.db'}")
        import server.sessions.orchestrator as orch_mod
        orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        async with db_session() as db:
            db.add(DbSession(id="arch", name="x", account_name="a", cwd="/tmp",
                             status="archived", origin="backlog-runner"))
            db.add(DbSession(id="live", name="y", account_name="a", cwd="/tmp",
                             status="idle", origin="backlog-runner"))
            db.add(DbSession(id="end", name="z", account_name="a", cwd="/tmp",
                             status="ended", origin="backlog-runner"))

        assert await orch._backlog_session_ended("arch") is True
        assert await orch._backlog_session_ended("end") is True
        assert await orch._backlog_session_ended("live") is False

    asyncio.run(run())

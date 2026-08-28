"""A completed backlog-runner session auto-archives itself.

Spawned sessions never reach a terminal DB state on their own, so a finished run
kept its live driver/worktree and (with target-state counting) held its slot
until the sweep freed it. When the agent signals completion — the derived status
becomes ``complete`` (see status.derive_status) — the orchestrator should archive
the session: stop the driver, release the worktree, and let the sweep reclaim the
slot. Completion archiving is a universal rule (all origins — see
test_completion_autoarchive.py); this file focuses on the backlog-runner specifics:
only on ``complete`` (not a plain ``done`` turn), and only once.
"""
from __future__ import annotations

import asyncio

from server.sessions.types import SessionRecord


def _rec(sid="s1", *, origin="backlog-runner"):
    r = SessionRecord(id=sid, name="run", account="a", claude_session_id="",
                      cols=80, rows=24, cwd="/tmp", repo="o/r", branch="feat/x")
    r.origin = origin
    return r


async def _drain():
    # Let the scheduled _auto_archive_completed task run to completion. The path
    # awaits several times (the app-wide toggle read, the completion-bar gate, the
    # archive/nudge), so pump enough turns rather than a fixed one or two.
    for _ in range(6):
        await asyncio.sleep(0)


async def _autoarchive_enabled() -> bool:
    """Stub for the app-wide auto-archive toggle read. These are unit-level orchs
    with every external seam stubbed and no DB, so the toggle is pinned enabled
    here (its own behavior is covered in test_settings.py and the global-disable
    case in test_completion_autoarchive.py) rather than left to read ambient DB
    state — which otherwise leaks across files and flakes these tests."""
    return True


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
    orch._read_auto_archive_enabled = _autoarchive_enabled  # type: ignore[method-assign]
    return orch, archived


def test_completed_backlog_session_is_archived():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch.pr_for = lambda repo, branch: _armed_pr()  # met bar (armed PR) → archive
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        await _drain()
        assert archived == ["s1"]

    asyncio.run(run())


def test_plain_done_turn_is_not_archived():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch._maybe_autoarchive_completed("s1", "done", orch._records["s1"])
        await asyncio.sleep(0)
        assert archived == []  # only an explicit completion signal archives

    asyncio.run(run())


# Note: human-origin completion behaviour (archive when the bar is met, nudge when it
# isn't) is covered comprehensively in test_completion_autoarchive.py — completion
# archiving is now a universal rule, no longer scoped to backlog-runner.


def test_manually_unarchived_backlog_session_is_not_re_archived():
    """A human unarchived this run (sticky opt-out) → completion must NOT re-archive it."""
    async def run():
        rec = _rec("s1")
        rec.auto_archive_opt_out_reason = "manual-unarchive"
        orch, archived = _orch({"s1": rec})
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []  # the sticky opt-out wins over the completion signal

    asyncio.run(run())


def test_double_complete_archives_once():
    async def run():
        orch, archived = _orch({"s1": _rec("s1")})
        orch.pr_for = lambda repo, branch: _armed_pr()  # met bar → archive
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        orch._maybe_autoarchive_completed("s1", "complete", orch._records["s1"])
        await _drain()
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
    orch._read_auto_archive_enabled = _autoarchive_enabled  # type: ignore[method-assign]
    return orch, archived_calls


def test_completed_backlog_archive_context_includes_pr_state_no_pr():
    """When no PR exists, archive_context has pr_state with has_repo=True, prs=[].

    Uses a non-code (research) goal so the universal readiness gate is met with no PR —
    a code goal with no PR would nudge instead of archive (see test_completion_autoarchive)."""
    async def run():
        rec = _rec("s1")
        rec.active_goal = "investigate and summarize the failure"  # non-code → met, no PR
        # rec.repo = "o/r", rec.branch = "feat/x"
        orch, calls = _orch_with_pr({"s1": rec}, pr_info=None)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
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
        await _drain()
        assert len(calls) == 1
        ctx = calls[0]["context"]
        assert "pr_state" in ctx
        prs = ctx["pr_state"]["prs"]
        assert len(prs) == 1
        assert prs[0]["number"] == 42

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Completion gate: open-unarmed PR with --squash --auto in instructions must
# trigger a nudge instead of an immediate archive (issue #636).
# ---------------------------------------------------------------------------

_BACKLOG_PROMPT = (
    "Implement the following GitHub issue(s) in repository `o/r` "
    "and land them in a single pull request:\n\n"
    "- o/r#1  (close with `Fixes #1` in the PR body)\n\n"
    "Requirements — follow the repository's completion workflow exactly:\n"
    "5. Arm auto-merge: `gh pr merge <#> --squash --auto`.\n"
)


def _orch_with_send_prompt(records, pr_info=None):
    """Like _orch_with_pr but also captures send_prompt calls."""
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records
    orch._auto_archiving = set()
    orch._backlog_ctx = AutomationContext("backlog-runner")

    archived_calls: list[dict] = []
    nudged: list[str] = []
    resumed: list[str] = []

    async def _spy_archive(session_id, *, archived_by="user", context=None):
        archived_calls.append({"session_id": session_id, "archived_by": archived_by,
                                "context": context})

    async def _spy_send_prompt(session_id, prompt):
        nudged.append(prompt)
        return "sent"

    async def _spy_ensure_driver(session_id, identity="system"):
        # The nudge path resumes the session before sending (the driver was released
        # by the `complete` transition). Stubbed here so these unit-level stubs, which
        # have no _drivers registry, exercise the nudge rather than blowing up in
        # ensure_driver — a resumed session is exactly what production hands it.
        resumed.append(session_id)
        return object()

    orch.archive_session = _spy_archive  # type: ignore[method-assign]
    orch.ensure_driver = _spy_ensure_driver  # type: ignore[method-assign]
    orch.send_prompt = _spy_send_prompt  # type: ignore[method-assign]
    orch.pr_for = lambda repo, branch: pr_info  # type: ignore[method-assign]
    orch._read_auto_archive_enabled = _autoarchive_enabled  # type: ignore[method-assign]
    orch.resumed_for_nudge = resumed
    return orch, archived_calls, nudged


def _unarmed_pr():
    import types
    return types.SimpleNamespace(
        number=99, state="open", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/99")


def _armed_pr():
    import types
    return types.SimpleNamespace(
        number=99, state="open", is_draft=False, auto_merge_enabled=True,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/99")


def test_open_unarmed_pr_with_arm_step_in_instructions_defers_archive():
    """A backlog session that emits `complete` but has an open-unarmed PR and
    instructions with `--squash --auto` must be nudged, NOT archived (issue #636)."""
    async def run():
        rec = _rec("s1")
        rec.initial_prompt = _BACKLOG_PROMPT
        orch, archived, nudged = _orch_with_send_prompt({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "must NOT archive — PR is open-unarmed"
        assert nudged, "must send a nudge to arm auto-merge"
        assert "squash" in nudged[0].lower() or "auto" in nudged[0].lower() or "arm" in nudged[0].lower()

    asyncio.run(run())


def test_armed_pr_with_arm_step_in_instructions_archives_normally():
    """An armed PR satisfies the gate even with --squash --auto in instructions."""
    async def run():
        rec = _rec("s1")
        rec.initial_prompt = _BACKLOG_PROMPT
        orch, archived, nudged = _orch_with_send_prompt({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert len(archived) == 1 and archived[0]["session_id"] == "s1"
        assert nudged == []

    asyncio.run(run())


def test_no_pr_with_arm_step_in_instructions_defers_archive():
    """No PR in cache + --squash --auto in instructions → defer (not archive)."""
    async def run():
        rec = _rec("s1")
        rec.initial_prompt = _BACKLOG_PROMPT
        orch, archived, nudged = _orch_with_send_prompt({"s1": rec}, pr_info=None)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "no armed PR found → must not archive"
        assert nudged

    asyncio.run(run())


def test_open_unarmed_pr_without_arm_step_now_defers():
    """The readiness gate is now UNIVERSAL: even without `--squash --auto` in the
    instructions, a completed session with an open-unarmed PR is nudged to arm it, not
    archived. (Previously the gate only fired when the task explicitly demanded arming;
    that carve-out was the inconsistency this change removes.)"""
    async def run():
        rec = _rec("s1")
        # No initial_prompt — the gate still applies from the open-unarmed PR fact alone.
        orch, archived, nudged = _orch_with_send_prompt({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "open-unarmed PR is not the finished state"
        assert nudged, "must nudge toward arming the PR"

    asyncio.run(run())


def test_deferred_archive_clears_guard_for_retry():
    """After a deferred archive (nudge sent), the _auto_archiving guard is cleared
    so a subsequent `complete` emit can trigger a fresh archive attempt."""
    async def run():
        rec = _rec("s1")
        rec.initial_prompt = _BACKLOG_PROMPT
        orch, archived, nudged = _orch_with_send_prompt({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert "s1" not in orch._auto_archiving, "guard must be cleared after deferral"

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

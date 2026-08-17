"""Completion-marker auto-archive is a universal rule, not a backlog-runner privilege.

Any session that emits ``<!-- ccswitch:complete -->`` (derived status ``complete``)
is auto-archived once the completion bar (``met_bar``) confirms the goal is genuinely
met — regardless of origin and regardless of the autopilot mode. When the bar is NOT
met (e.g. an open-unarmed PR), the session is nudged toward the remaining work instead
of archiving; the nudge opens a fresh turn, so the session no longer rests in
``complete``. A per-session nudge cap bounds an ill-behaved agent that re-signals
completion without making progress.

This generalises the old backlog-runner-only path (see test_backlog_autoarchive.py).
"""
from __future__ import annotations

import asyncio
import types

from server.sessions.types import SessionRecord


def _rec(sid="s1", *, origin=None, repo="o/r", branch="feat/x", initial_prompt=None):
    r = SessionRecord(id=sid, name="run", account="a", claude_session_id="",
                      cols=80, rows=24, cwd="/tmp", repo=repo, branch=branch)
    r.origin = origin
    r.initial_prompt = initial_prompt
    return r


def _orch(records, pr_info=None):
    import server.sessions.orchestrator as orch_mod
    from server.automations.context import AutomationContext
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records
    orch._auto_archiving = set()
    orch._backlog_ctx = AutomationContext("backlog-runner")

    archived: list[dict] = []
    nudged: list[str] = []

    async def _spy_archive(session_id, *, archived_by="user", context=None):
        archived.append({"session_id": session_id, "archived_by": archived_by,
                         "context": context})

    async def _spy_send_prompt(session_id, prompt):
        nudged.append(prompt)
        return "sent"

    orch.archive_session = _spy_archive  # type: ignore[method-assign]
    orch.send_prompt = _spy_send_prompt  # type: ignore[method-assign]
    orch.pr_for = lambda repo, branch: pr_info  # type: ignore[method-assign]
    return orch, archived, nudged


def _armed_pr():
    return types.SimpleNamespace(
        number=42, state="open", is_draft=False, auto_merge_enabled=True,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/42")


def _unarmed_pr():
    return types.SimpleNamespace(
        number=99, state="open", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/99")


def _merged_pr():
    return types.SimpleNamespace(
        number=7, state="merged", is_draft=False, auto_merge_enabled=False,
        in_merge_queue=False, checks="passing", url="https://github.com/o/r/pull/7")


async def _drain():
    # Let the scheduled _auto_archive_completed task run to completion.
    for _ in range(4):
        await asyncio.sleep(0)


def test_human_complete_with_armed_pr_is_archived():
    """A human-origin session that self-declares complete with an armed PR archives —
    the old code returned early for any non-backlog origin, so it never did."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == []
    asyncio.run(run())


def test_human_complete_no_repo_is_archived():
    """A repo-less (non-code / research) session that completes archives immediately —
    met_bar returns met for a non-code goal with no PR expected."""
    async def run():
        rec = _rec("s1", origin=None, repo=None, branch=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=None)
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
        assert nudged == []
    asyncio.run(run())


def test_complete_with_merged_pr_non_backlog_origin_archived():
    """Any origin (here pr-doctor) with a merged PR archives on complete."""
    async def run():
        rec = _rec("s1", origin="pr-doctor")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_merged_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert [a["session_id"] for a in archived] == ["s1"]
    asyncio.run(run())


def test_human_complete_with_unarmed_pr_is_nudged_not_archived():
    """The readiness gate is universal: a completed session with an open-unarmed PR is
    nudged to arm it, NOT archived — even without an explicit --squash --auto step."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == [], "open-unarmed PR is not the finished state"
        assert nudged, "must nudge toward arming the PR"
        assert "99" in nudged[0]  # remedy references the PR number
    asyncio.run(run())


def test_completion_archive_provenance_is_completion_for_non_backlog():
    """Non-backlog completion archives are stamped archived_by='completion' so the retro
    archive audit + reversal-learning (AUTO_ARCHIVE_PROVENANCE) still cover them."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived[0]["archived_by"] == "completion"
    asyncio.run(run())


def test_backlog_completion_archive_keeps_backlog_provenance():
    """Backlog-runner completion archives keep their existing archived_by provenance."""
    async def run():
        rec = _rec("s1", origin="backlog-runner")
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived[0]["archived_by"] == "backlog-runner"
    asyncio.run(run())


def test_manual_unarchive_opt_out_blocks_any_origin():
    """The sticky manual-unarchive opt-out wins over the completion signal for every origin."""
    async def run():
        rec = _rec("s1", origin=None)
        rec.auto_archive_opt_out_reason = "manual-unarchive"
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_armed_pr())
        orch._maybe_autoarchive_completed("s1", "complete", rec)
        await _drain()
        assert archived == []
        assert nudged == []
    asyncio.run(run())


def test_repeated_not_ready_completes_stop_nudging_after_cap():
    """An agent that keeps re-signalling complete without arming its PR is nudged up to
    the cap, then left alone (no infinite nudge loop)."""
    async def run():
        rec = _rec("s1", origin=None)
        orch, archived, nudged = _orch({"s1": rec}, pr_info=_unarmed_pr())
        # Re-emit complete many times; each defer clears the guard so the next call runs.
        for _ in range(6):
            orch._maybe_autoarchive_completed("s1", "complete", rec)
            await _drain()
        assert archived == []
        from server.sessions.orchestrator import COMPLETE_NUDGE_CAP
        assert len(nudged) == COMPLETE_NUDGE_CAP, (
            f"nudges must be capped at {COMPLETE_NUDGE_CAP}, got {len(nudged)}")
    asyncio.run(run())

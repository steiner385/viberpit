"""Unit tests for ArchiveAuditLoop.tick() — all deps injected, no network/DB."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock


# ---------------------------------------------------------------------------
# Helpers mirroring test_archive_audit.py helpers for session-row + gh shapes
# ---------------------------------------------------------------------------

def _row(id, goal, repo="o/r", branch="feat/x", authored=("feat/x",), initial_prompt=None):
    return {"id": id, "repo": repo, "branch": branch,
            "authored_branches": list(authored), "goal": goal, "goal_source": "explicit",
            "initial_prompt": initial_prompt}


def _pr(number=1, state="OPEN", is_draft=False, auto_merge=False, merge_queue=False):
    return {"number": number, "state": state, "isDraft": is_draft,
            "autoMergeRequest": {"enabledAt": "t"} if auto_merge else None,
            "mergeQueueEntry": {"state": "QUEUED"} if merge_queue else None,
            "url": f"http://x/{number}"}


def _gh_returning(pr):
    """Sync gh_fn that returns [pr] for any (repo, branch) call."""
    def gh_fn(repo, branch):
        return [pr] if pr else []
    return gh_fn


# ---------------------------------------------------------------------------
# Fake ctx with async emit spy
# ---------------------------------------------------------------------------

class _FakeCtx:
    def __init__(self):
        self.emit = AsyncMock()


# ---------------------------------------------------------------------------
# Fixed epoch clock
# ---------------------------------------------------------------------------

_FIXED_EPOCH = 1700000000.0


async def _fake_record(results, mode):
    """Stand-in for persist_findings: mirrors its contract without a DB."""
    premature = [r["id"] for r in results if r["bucket"] == "premature"]
    return {"to_unarchive": premature if mode == "auto_reopen" else [],
            "opened": len(premature), "resolved": 0, "reopened": 0,
            "open_total": len(premature) if mode == "propose" else 0}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tick_disabled_does_nothing():
    """When mode is 'off', list_rows is never called and no unarchive/emit occurs."""
    async def run():
        ctx = _FakeCtx()
        list_rows = AsyncMock(return_value=[])
        unarchive_fn = AsyncMock()

        async def read_config():
            return ("off", {})

        from server.automations.archive_audit import ArchiveAuditLoop
        loop = ArchiveAuditLoop(
            ctx,
            read_config=read_config,
            list_rows=list_rows,
            gh_fn=_gh_returning(None),
            gh_num_fn=None,
            unarchive_fn=unarchive_fn,
            clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record,
        )
        await loop.tick()

        list_rows.assert_not_called()
        unarchive_fn.assert_not_called()
        ctx.emit.assert_not_called()

    asyncio.run(run())


def test_tick_enabled_premature_unarchives():
    """Enabled + a premature session (open unarmed PR): unarchive_fn called, emit fired."""
    async def run():
        ctx = _FakeCtx()
        # One row whose PR is open and unarmed -> premature
        rows = [_row("sess-premature", "fix ci")]
        list_rows = AsyncMock(return_value=rows)
        unarchive_fn = AsyncMock(return_value=None)

        async def read_config():
            return ("auto_reopen", {"window_days": 7})

        # gh_fn returns an open, unarmed, non-draft PR -> premature bucket
        gh_fn = _gh_returning(_pr(state="OPEN", auto_merge=False))

        from server.automations.archive_audit import ArchiveAuditLoop
        loop = ArchiveAuditLoop(
            ctx,
            read_config=read_config,
            list_rows=list_rows,
            gh_fn=gh_fn,
            gh_num_fn=None,
            unarchive_fn=unarchive_fn,
            clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record,
        )
        await loop.tick()

        # unarchive must have been called with the session id
        unarchive_fn.assert_awaited_once_with("sess-premature")

        # emit must have been called once with kind="audit"
        ctx.emit.assert_awaited_once()
        call_kwargs = ctx.emit.call_args
        assert call_kwargs.kwargs.get("kind") == "audit"
        detail = call_kwargs.kwargs.get("detail", {})
        assert detail.get("unarchived") == ["sess-premature"]
        assert detail.get("counts", {}).get("premature", 0) == 1

    asyncio.run(run())


def test_tick_enabled_legit_not_unarchived():
    """Enabled + a session with a merged PR (legit): unarchive_fn NOT called."""
    async def run():
        ctx = _FakeCtx()
        rows = [_row("sess-legit", "fix ci")]
        list_rows = AsyncMock(return_value=rows)
        unarchive_fn = AsyncMock(return_value=None)

        async def read_config():
            return ("auto_reopen", {"window_days": 7})

        # merged PR -> legit bucket
        gh_fn = _gh_returning(_pr(state="MERGED"))

        from server.automations.archive_audit import ArchiveAuditLoop
        loop = ArchiveAuditLoop(
            ctx,
            read_config=read_config,
            list_rows=list_rows,
            gh_fn=gh_fn,
            gh_num_fn=None,
            unarchive_fn=unarchive_fn,
            clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record,
        )
        await loop.tick()

        unarchive_fn.assert_not_called()
        # emit is still called (report), but unarchived list is empty
        ctx.emit.assert_awaited_once()
        detail = ctx.emit.call_args.kwargs.get("detail", {})
        assert detail.get("unarchived") == []
        assert detail.get("counts", {}).get("legit", 0) == 1

    asyncio.run(run())


def test_tick_enabled_merge_queued_not_unarchived():
    """Enabled + a session whose PR is in the merge queue (legit/armed): NOT unarchived.
    Regression tie-in to #523: queued PRs must be treated as armed, not premature."""
    async def run():
        ctx = _FakeCtx()
        rows = [_row("sess-queued", "fix ci")]
        list_rows = AsyncMock(return_value=rows)
        unarchive_fn = AsyncMock(return_value=None)

        async def read_config():
            return ("auto_reopen", {"window_days": 7})

        # open + mergeQueueEntry non-null -> in_merge_queue=True -> armed -> legit
        gh_fn = _gh_returning(_pr(state="OPEN", auto_merge=False, merge_queue=True))

        from server.automations.archive_audit import ArchiveAuditLoop
        loop = ArchiveAuditLoop(
            ctx,
            read_config=read_config,
            list_rows=list_rows,
            gh_fn=gh_fn,
            gh_num_fn=None,
            unarchive_fn=unarchive_fn,
            clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record,
        )
        await loop.tick()

        unarchive_fn.assert_not_called()
        detail = ctx.emit.call_args.kwargs.get("detail", {})
        assert detail.get("unarchived") == []
        # queued PR should be legit (met bar via in_merge_queue)
        assert detail.get("counts", {}).get("premature", 0) == 0

    asyncio.run(run())


def test_tick_enabled_no_rows_skips_emit():
    """When list_rows returns empty, neither unarchive nor emit are called."""
    async def run():
        ctx = _FakeCtx()
        list_rows = AsyncMock(return_value=[])
        unarchive_fn = AsyncMock()

        async def read_config():
            return ("auto_reopen", {"window_days": 7})

        from server.automations.archive_audit import ArchiveAuditLoop
        loop = ArchiveAuditLoop(
            ctx,
            read_config=read_config,
            list_rows=list_rows,
            gh_fn=_gh_returning(None),
            gh_num_fn=None,
            unarchive_fn=unarchive_fn,
            clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record,
        )
        await loop.tick()

        unarchive_fn.assert_not_called()
        ctx.emit.assert_not_called()

    asyncio.run(run())


def test_tick_publishes_status_counts_for_card():
    """After an enabled tick, ctx.archive_audit_counts is set so ArchiveAudit.status()
    reflects the run — guards against the status card being permanently stuck 'idle'."""
    async def run():
        from server.automations.archive_audit import ArchiveAudit, ArchiveAuditLoop
        ctx = _FakeCtx()
        list_rows = AsyncMock(return_value=[_row("sess-prem", "fix ci")])
        unarchive_fn = AsyncMock()

        async def read_config():
            return ("auto_reopen", {"window_days": 7})

        loop = ArchiveAuditLoop(
            ctx, read_config=read_config, list_rows=list_rows,
            gh_fn=_gh_returning(_pr(state="OPEN", auto_merge=False)),
            gh_num_fn=None, unarchive_fn=unarchive_fn, clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record)
        await loop.tick()

        unarchive_fn.assert_awaited_once_with("sess-prem")
        counts = getattr(ctx, "archive_audit_counts", None)
        assert counts is not None
        assert counts["total"] == 1 and counts["unarchived"] == 1
        assert counts["premature"] == 1
        assert ArchiveAudit().status(ctx).level == "active"

    asyncio.run(run())


def test_archive_audit_registered_in_registry():
    """The automation must be registered so the daemon builds/starts its loop."""
    from server.automations.registry import registry
    assert "archive-audit" in {a.id for a in registry.all()}


def test_tick_propose_files_a_finding_and_never_unarchives():
    """The default mode: a premature completion becomes a review finding, and NOTHING
    is reopened — the human decides (#1611 decision 6)."""
    async def run():
        from server.automations.archive_audit import ArchiveAudit, ArchiveAuditLoop
        ctx = _FakeCtx()
        unarchive_fn = AsyncMock()

        async def read_config():
            return ("propose", {"window_days": 7})

        loop = ArchiveAuditLoop(
            ctx, read_config=read_config,
            list_rows=AsyncMock(return_value=[_row("sess-prem", "fix ci")]),
            gh_fn=_gh_returning(_pr(state="OPEN", auto_merge=False)),
            gh_num_fn=None, unarchive_fn=unarchive_fn, clock=lambda: _FIXED_EPOCH,
            record_findings_fn=_fake_record)
        await loop.tick()

        unarchive_fn.assert_not_called()
        detail = ctx.emit.call_args.kwargs["detail"]
        assert detail["unarchived"] == [] and detail["findings"]["opened"] == 1
        st = ArchiveAudit().status(ctx)
        # An outstanding finding is a HUMAN wait, not a failure and not "idle".
        assert st.waiting_reason == "human" and st.level == "warn"
        assert st.counts["open_findings"] == 1

    asyncio.run(run())


def test_default_mode_is_propose_and_legacy_act_is_not_consent_to_auto_reopen():
    """An enabled row with no mode, or one still carrying the retired pre-#1611 "act"
    (auto-unarchive), resolves to propose — never to auto_reopen."""
    from server.automations.archive_audit import ArchiveAudit
    from server.automations.base import retired_modes_of
    assert ArchiveAudit.modes == ("off", "propose", "auto_reopen")
    assert retired_modes_of(ArchiveAudit()) == {"act": "propose"}
    assert "act" not in ArchiveAudit.modes

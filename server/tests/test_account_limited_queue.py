"""A create (manual or automation) that finds every account at its ceiling is QUEUED
with queue_reason="account_limited" (user decision 2026-10-08), and the drain starts
it only once select_account finds headroom again."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime
from unittest.mock import patch

import pytest

from server.sessions import session_queue as sq
from server.tests.test_session_cap_enqueue import _make_orch, _stop_orch
from viberpit.selector import AllAccountsBusy

_RESET = datetime(2030, 1, 1, 10, 0, 0)


def _busy(*a, **k):
    raise AllAccountsBusy(_RESET, "at_ceiling")


def test_manual_create_at_ceiling_queues_account_limited(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
            assert rec.id not in orch._drivers
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_no_usable_accounts_still_refuses(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            def reauth(*a, **k):
                raise AllAccountsBusy(None, "no_usable_accounts")
            with patch("server.sessions.orchestrator.select_account", side_effect=reauth):
                with pytest.raises(AllAccountsBusy):
                    await orch.create_session("m", initial_prompt="hi",
                                              cwd=str(tmp_path / "reporoot"))
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_assistant_at_ceiling_still_refuses(tmp_path):
    """The assistant is exempt from every cap and never queues."""
    from server.sessions.types import ASSISTANT_KIND

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'s.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                with pytest.raises(AllAccountsBusy):
                    await orch.create_session("asst", initial_prompt="hi",
                                              cwd=str(tmp_path / "reporoot"),
                                              kind=ASSISTANT_KIND, owner_identity="me")
            assert await sq.positions() == {}
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_automation_launch_at_ceiling_also_queues(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"),
                                                origin="automation:test",
                                                origin_ref="acme/widgets#1")
            assert rec.status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry.params.get("queue_reason") == "account_limited"
            assert entry.params.get("origin") == "automation:test"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_drain_leaves_account_limited_queued_until_headroom(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
                assert await orch._maybe_start_queued() == 0
            assert orch.get_session(rec.id).status == "queued"
            # Headroom returns: the real (fixture-seeded) selector now picks "alpha".
            assert await orch._maybe_start_queued() == 1
            assert orch.get_session(rec.id).status != "queued"
            assert await sq.entry_for(rec.id) is None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_drain_probe_is_read_only(tmp_path):
    """The drain's availability probe must never burn a reservation window."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'r.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            with patch("server.sessions.orchestrator.select_account",
                       side_effect=_busy) as sel:
                await orch.create_session("m", initial_prompt="hi",
                                          cwd=str(tmp_path / "reporoot"))
                sel.reset_mock()
                assert await orch._maybe_start_queued() == 0
            assert sel.call_count == 1
            assert sel.call_args.kwargs["reserve"] is False
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def _git_repo(path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(cmd, cwd=path, check=True)
    return str(path)


def test_queued_row_leaks_no_worktree(tmp_path):
    """The refused spawn already checked out a worktree; the queued row must not
    keep it (a leaked worktree pins its child branch and wedges the later start)."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'w.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            repo = _git_repo(tmp_path / "proj")
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session("m", initial_prompt="hi", cwd=repo)
            assert rec.status == "queued"
            assert rec.id not in orch._worktrees
            listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                                    check=True, capture_output=True, text=True).stdout
            assert listed.count("worktree ") == 1, listed
            assert orch.get_session(rec.id).status == "queued"
            # And it still starts cleanly once quota frees.
            assert await orch._maybe_start_queued() == 1
            assert rec.id in orch._worktrees
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_ownership_token_create_at_ceiling_queues_and_keeps_its_claim(tmp_path):
    """A claimed automation launch (ownership_token) at the ceiling rides the same
    path as a ``wait_for_slot`` → ``runner_capacity`` queue: launch() returns the
    queued record and create_session binds the reservation to it — so the claim is
    'active' on THIS session (not 'uncertain', not leaked), and still is after the
    drain starts it."""
    from server.automations.ownership import CanonicalTarget, acquire, owned

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'o.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            async with acquire([CanonicalTarget("I_acct", "acme/widgets#9")],
                               owner="automation:test", source="automatic",
                               reason="dispatch",
                               provenance={"origin_ref": "acme/widgets#9"}) as tx:
                token = tx.token
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                rec = await orch.create_session(
                    "m", initial_prompt="hi", cwd=str(tmp_path / "reporoot"),
                    origin="automation:test", origin_ref="acme/widgets#9",
                    ownership_token=token)
            assert rec.status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
            async with owned(token) as tx:
                assert tx.operation.phase == "active"
                assert tx.operation.reserved_session_id == rec.id
                assert tx.operation.session_id == rec.id

            assert await orch._maybe_start_queued() == 1
            assert orch.get_session(rec.id).status != "queued"
            async with owned(token) as tx:
                assert tx.operation.phase == "active"
                assert tx.operation.session_id == rec.id
                assert tx.operation.reserved_session_id == rec.id
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


async def _enqueue(orch, cwd: str, *, queue_reason: str):
    import uuid
    sid = str(uuid.uuid4())
    return await orch._enqueue_session(
        sid, name="q", account=None, initial_prompt="hi", cols=80, rows=24,
        cwd=cwd, branch=None, repo=None, identity="system",
        permission_mode="interactive", attachment_ids=None, name_is_auto=True,
        origin=None, origin_ref=None, model=None, queue_reason=queue_reason)


def test_spawn_at_ceiling_after_probe_passes_stays_queued(tmp_path):
    """Probe race: the read-only probe saw headroom, but the spawn's own reserving
    select_account hits the ceiling. The session stays queued (not 'ended'), its
    entry intact and still account_limited, with no leaked worktree."""
    from unittest.mock import AsyncMock

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'p.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            repo = _git_repo(tmp_path / "proj")
            rec = await _enqueue(orch, repo, queue_reason="account_limited")
            orch._account_headroom_or_raise = AsyncMock(return_value=None)  # type: ignore[method-assign]
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                assert await orch._maybe_start_queued() == 0
            assert orch.get_session(rec.id).status == "queued"
            assert rec.id not in orch._drivers and rec.id not in orch._worktrees
            listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                                    check=True, capture_output=True, text=True).stdout
            assert listed.count("worktree ") == 1, listed
            entry = await sq.entry_for(rec.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
            from server.db import db_session
            from server.db.models import Session as DbSession
            async with db_session() as db:
                assert (await db.get(DbSession, rec.id)).status == "queued"
            # Quota frees: it starts.
            assert await orch._maybe_start_queued() == 1
            assert orch.get_session(rec.id).status != "queued"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_global_cap_entry_hitting_ceiling_is_relabelled_account_limited(tmp_path):
    """A never-probed entry (global_cap) whose slot frees while every account is at
    its ceiling waits as account_limited instead of being ended."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'g.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            rec = await _enqueue(orch, str(tmp_path / "reporoot"), queue_reason="global_cap")
            assert "queue_reason" not in (await sq.entry_for(rec.id)).params
            with patch("server.sessions.orchestrator.select_account", side_effect=_busy):
                assert await orch._maybe_start_queued() == 0
            assert orch.get_session(rec.id).status == "queued"
            entry = await sq.entry_for(rec.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
            assert entry.params.get("name") == "q"  # the rest of the blob is untouched
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_spawn_needing_reauth_is_still_ended(tmp_path):
    """no_usable_accounts never clears on its own: today's 'ended' behaviour stays."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'n.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            rec = await _enqueue(orch, str(tmp_path / "reporoot"), queue_reason="global_cap")

            def reauth(*a, **k):
                raise AllAccountsBusy(None, "no_usable_accounts")
            with patch("server.sessions.orchestrator.select_account", side_effect=reauth):
                await orch._maybe_start_queued()
            assert orch.get_session(rec.id).status == "ended"
            assert await sq.entry_for(rec.id) is None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())

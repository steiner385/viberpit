"""Start now (2026-10-08): a queued session starts immediately over the global cap,
a host cap and a product cap — but never over an account ceiling."""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from server.sessions import session_queue as sq
from server.tests.test_product_capacity_queue import _make_capacity_orch, _seed_product_at_cap
from server.tests.test_session_cap_enqueue import _make_orch, _stop_orch
from viberpit.selector import AllAccountsBusy


def test_start_now_overrides_global_cap(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'a.db'}",
                                tmp_path=tmp_path, max_active_sessions=1)
        try:
            first = await orch.create_session("one", cwd=str(tmp_path / "reporoot"))
            queued = await orch.create_session("two", cwd=str(tmp_path / "reporoot"))
            assert first.status != "queued" and queued.status == "queued"
            assert await orch.start_queued_now(queued.id) == "started"
            assert orch.get_session(queued.id).status != "queued"
            assert await sq.entry_for(queued.id) is None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_overrides_product_cap(tmp_path):
    async def run():
        orch = await _make_capacity_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'b.db'}",
                                         tmp_path=tmp_path)
        try:
            await _seed_product_at_cap("acme/widgets")
            rec = await orch.create_session("m", repo="acme/widgets", initial_prompt="hi",
                                            cwd=str(tmp_path / "reporoot"))
            assert rec.status == "queued"
            assert await orch.start_queued_now(rec.id) == "started"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_refuses_account_limited_and_keeps_it_queued(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            def busy(*a, **k):
                raise AllAccountsBusy(datetime(2030, 1, 1), "at_ceiling")
            with patch("server.sessions.orchestrator.select_account", side_effect=busy):
                rec = await orch.create_session("m", initial_prompt="hi",
                                                cwd=str(tmp_path / "reporoot"))
                with pytest.raises(AllAccountsBusy):
                    await orch.start_queued_now(rec.id)
            assert orch.get_session(rec.id).status == "queued"
            assert await sq.entry_for(rec.id) is not None
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_spawn_hitting_ceiling_raises_and_stays_queued(tmp_path):
    """The read-only probe passes but the forced spawn's own select_account hits the
    ceiling: the drain re-queues the entry, so start_queued_now must raise
    AllAccountsBusy (never report 'not_queued') and the session stays queued."""
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'e.db'}",
                                tmp_path=tmp_path, max_active_sessions=1)
        try:
            await orch.create_session("one", cwd=str(tmp_path / "reporoot"))
            queued = await orch.create_session("two", initial_prompt="hi",
                                               cwd=str(tmp_path / "reporoot"))
            assert queued.status == "queued"
            orch._account_headroom_or_raise = AsyncMock(return_value=None)  # type: ignore[method-assign]

            def busy(*a, **k):
                raise AllAccountsBusy(datetime(2030, 1, 1), "at_ceiling")
            with patch("server.sessions.orchestrator.select_account", side_effect=busy):
                with pytest.raises(AllAccountsBusy) as excinfo:
                    await orch.start_queued_now(queued.id)
            assert excinfo.value.kind == "at_ceiling"
            assert orch.get_session(queued.id).status == "queued"
            entry = await sq.entry_for(queued.id)
            assert entry is not None and entry.params.get("queue_reason") == "account_limited"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_now_unknown_or_unqueued(tmp_path):
    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path/'d.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            live = await orch.create_session("m", cwd=str(tmp_path / "reporoot"))
            assert await orch.start_queued_now(live.id) == "not_queued"
            assert await orch.start_queued_now("nope") == "not_queued"
        finally:
            await _stop_orch(orch)
    asyncio.run(run())


def test_start_queued_now_is_per_session_routed():
    from server.sessions.fleet_orchestrator import _PER_SESSION
    assert "start_queued_now" in _PER_SESSION

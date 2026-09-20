"""SessionOrchestrator._automation_create_session: the dispatch logic every
fleet-global automation loop (Backlog Runner, PR Doctor, ...) now binds instead of
create_session directly (spec: automation dispatch balance, 2026-09-20).

Exercised against a bare stand-in for `self` (only `.create_session` is touched),
not a fully-started orchestrator — the method is a thin routing wrapper, and a full
`SessionOrchestrator.start()` harness (see test_orchestrator_model_gate.py) would
test everything BUT the branch this file cares about.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from server.sessions.internal_dispatch import EdgeUnreachable
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.sessions.engine import AgentEngine


def _rec(id_: str) -> SessionRecord:
    return SessionRecord(id=id_, name="x", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="e", cols=80, rows=24, cwd="/tmp")


class _FakeSelf:
    """Duck-types the one attribute `_automation_create_session` reads from self."""

    def __init__(self):
        self.create_session = AsyncMock(return_value=_rec("local"))


@pytest.mark.asyncio
async def test_routing_disabled_calls_local_create_directly():
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "false"}):
        me = _FakeSelf()
        rec = await SessionOrchestrator._automation_create_session(me, name="x", origin="backlog-runner")
        assert rec.id == "local"
        me.create_session.assert_awaited_once_with(name="x", origin="backlog-runner")


@pytest.mark.asyncio
async def test_routing_enabled_success_never_touches_local_create():
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}), \
         patch("server.sessions.internal_dispatch.create_via_edge",
               new=AsyncMock(return_value=_rec("via-edge"))) as via_edge:
        me = _FakeSelf()
        rec = await SessionOrchestrator._automation_create_session(me, name="x")
        assert rec.id == "via-edge"
        me.create_session.assert_not_awaited()
        via_edge.assert_awaited_once()


@pytest.mark.asyncio
async def test_edge_unreachable_falls_back_to_local_create():
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}), \
         patch("server.sessions.internal_dispatch.create_via_edge",
               new=AsyncMock(side_effect=EdgeUnreachable("connection refused"))):
        me = _FakeSelf()
        rec = await SessionOrchestrator._automation_create_session(me, name="x", origin="pr-doctor")
        assert rec.id == "local"
        me.create_session.assert_awaited_once_with(name="x", origin="pr-doctor")


@pytest.mark.asyncio
async def test_domain_refusal_propagates_without_falling_back():
    from server.sessions.selection import AllAccountsBusy
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}), \
         patch("server.sessions.internal_dispatch.create_via_edge",
               new=AsyncMock(side_effect=AllAccountsBusy(None, kind="at_ceiling"))):
        me = _FakeSelf()
        with pytest.raises(AllAccountsBusy):
            await SessionOrchestrator._automation_create_session(me, name="x")
        me.create_session.assert_not_awaited()

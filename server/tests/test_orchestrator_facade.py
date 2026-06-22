from unittest import mock
import pytest
from server.sessions.facade import InProcessOrchestratorAdapter


def _orch():
    orch = mock.MagicMock()
    orch.create_session = mock.AsyncMock(return_value="created")
    orch.get_session = mock.MagicMock(return_value="rec")          # sync on the real orch
    orch.resolve_permission = mock.MagicMock(return_value=True)    # sync
    orch.poller = mock.MagicMock()
    orch.poller.get = mock.AsyncMock(return_value={"usage": 1})
    orch._registry = mock.MagicMock()
    orch._registry.pending = mock.MagicMock(return_value=["p1"])
    return orch


@pytest.mark.asyncio
async def test_async_method_is_awaited_and_forwarded():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.create_session(name="S", account="a") == "created"
    orch.create_session.assert_awaited_once_with(name="S", account="a")


@pytest.mark.asyncio
async def test_sync_method_is_wrapped_awaitable():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.get_session("s1") == "rec"
    orch.get_session.assert_called_once_with("s1")
    assert await f.resolve_permission("s1", "p1", "allow", "me") is True


@pytest.mark.asyncio
async def test_usage_wraps_poller_get():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.usage("acct") == {"usage": 1}
    orch.poller.get.assert_awaited_once_with("acct")


@pytest.mark.asyncio
async def test_pending_permissions_wraps_registry():
    orch = _orch()
    f = InProcessOrchestratorAdapter(orch)
    assert await f.pending_permissions("s1") == ["p1"]
    orch._registry.pending.assert_called_once_with("s1")

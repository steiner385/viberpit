"""Driver.interrupt() on a disconnected SDK client after daemon restart.

When the daemon restarts mid-turn, the session's driver holds a disconnected SDK client.
The stall watchdog triggers reassign, which calls driver.interrupt(). Before the fix, the
guard `if self._client:` checked existence but not connection state, so _client.interrupt()
raised CLIConnectionError and aborted recovery. After the fix, we catch CLIConnectionError
and proceed — the client is already dead, so interrupt is unnecessary.
"""
import asyncio
from unittest.mock import AsyncMock, Mock
import pytest
from claude_agent_sdk._errors import CLIConnectionError
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry


@pytest.mark.asyncio
async def test_interrupt_on_disconnected_client_proceeds():
    """interrupt() on a disconnected client catches CLIConnectionError and proceeds."""
    
    # Build a minimal driver with a fake SDK client that raises on interrupt
    out_q = asyncio.Queue()
    
    disconnected_client = Mock()
    disconnected_client.interrupt = AsyncMock(side_effect=CLIConnectionError("Not connected. Call connect() first."))
    
    def fake_sdk_factory(*args, **kwargs):
        return disconnected_client
    
    driver = SessionDriver(
        session_id="test-123",
        account="acc1",
        cwd="/tmp",
        config_dir="/tmp/claude",
        mode="default",
        sdk_factory=fake_sdk_factory,
        registry=PermissionRegistry(),
        identity="user@example.com",
    )
    
    # Wire the disconnected client in directly (simulates post-restart state)
    driver._client = disconnected_client
    driver._out = out_q
    driver._turn_active = True
    
    # Before the fix: this raises CLIConnectionError and aborts
    # After the fix: catches the error and proceeds
    await driver.interrupt()
    
    # Verify it called the disconnected client's interrupt (and caught the error)
    disconnected_client.interrupt.assert_awaited_once()
    
    # Verify turn_active was cleared (proves we didn't abort early)
    assert driver._turn_active is False


@pytest.mark.asyncio
async def test_aclose_on_disconnected_client_logs_and_proceeds():
    """aclose() on a disconnected client logs the interrupt failure and proceeds to drain."""
    
    out_q = asyncio.Queue()
    
    disconnected_client = Mock()
    disconnected_client.interrupt = AsyncMock(side_effect=CLIConnectionError("Not connected. Call connect() first."))
    disconnected_client.__aexit__ = AsyncMock()
    
    def fake_sdk_factory(*args, **kwargs):
        return disconnected_client
    
    async def fake_pump():
        # Immediate finish so aclose doesn't hang
        pass
    
    driver = SessionDriver(
        session_id="test-456",
        account="acc2",
        cwd="/tmp",
        config_dir="/tmp/claude2",
        mode="default",
        sdk_factory=fake_sdk_factory,
        registry=PermissionRegistry(),
        identity="user2@example.com",
    )
    
    driver._client = disconnected_client
    driver._turn_active = True
    driver._pump = asyncio.create_task(fake_pump())
    driver._close_grace = 0.1  # Short timeout for test speed
    
    # aclose() already has try/except that logs and proceeds (pre-existing behavior)
    await driver.aclose()
    
    # Verify interrupt was attempted (and the exception was caught/logged)
    disconnected_client.interrupt.assert_awaited_once()
    
    # Verify aclose proceeded to __aexit__ (the actual cleanup call)
    disconnected_client.__aexit__.assert_awaited_once()

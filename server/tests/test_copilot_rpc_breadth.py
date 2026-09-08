"""Breadth tests for the Copilot 3-touch: RPC registration + RemoteOrchestrator proxies.

Verifies that the four copilot methods are:
  1. Registered as RPC handlers in register_orchestrator_rpc.
  2. Present on RemoteOrchestrator and forward to the correct call("name", account=...).
"""
from unittest import mock

from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator

_COPILOT_METHODS = [
    "copilot_add_start",
    "copilot_add_status",
    "copilot_add_cancel",
    # Copilot quota is read through the process-backed refresher (shared with
    # Codex), not a Copilot-specific RPC.
    "process_quota_refresh",
]


# ---------------------------------------------------------------------------
# RPC registration
# ---------------------------------------------------------------------------

def _build_app_and_orch():
    app = create_daemon_app()
    orch = SessionOrchestrator(
        sdk_factory=mock.MagicMock(),
        poll_interval=9999.0,
        usage_fn=lambda n: None,
    )
    register_orchestrator_rpc(app, orch)
    return app, orch


def test_all_four_methods_are_registered_as_rpc():
    """register_orchestrator_rpc must register all four copilot handlers.

    register_rpc stores handlers in app.state.rpc (a dict), not as FastAPI
    routes directly. The single /rpc/{method} route dispatches on that dict.
    """
    app, _ = _build_app_and_orch()
    registered = set(app.state.rpc.keys())
    for name in _COPILOT_METHODS:
        assert name in registered, (
            f"RPC handler '{name}' not registered — missing register_rpc(app, '{name}', ...) "
            f"in server/ipc/orchestrator_rpc.py"
        )


# ---------------------------------------------------------------------------
# RemoteOrchestrator proxies
# ---------------------------------------------------------------------------

def test_remote_orchestrator_has_all_four_proxy_methods():
    """RemoteOrchestrator must expose all four copilot proxy methods."""
    for name in _COPILOT_METHODS:
        assert callable(getattr(RemoteOrchestrator, name, None)), (
            f"RemoteOrchestrator.{name} missing — add a proxy in server/sessions/remote.py"
        )


def _make_fake_client():
    """Return an async-capable fake client that records call() invocations."""
    client = mock.MagicMock()
    client.call = mock.AsyncMock(return_value={"ok": True})
    return client


async def _call_method(ro, name, account="acct-copilot"):
    return await getattr(ro, name)(account=account)


import pytest


@pytest.mark.asyncio
async def test_copilot_add_start_forwards_to_rpc():
    client = _make_fake_client()
    ro = RemoteOrchestrator(client)
    await ro.copilot_add_start(account="acct1")
    client.call.assert_called_once_with("copilot_add_start", account="acct1")


@pytest.mark.asyncio
async def test_copilot_add_status_forwards_to_rpc():
    client = _make_fake_client()
    ro = RemoteOrchestrator(client)
    await ro.copilot_add_status(account="acct2")
    client.call.assert_called_once_with("copilot_add_status", account="acct2")


@pytest.mark.asyncio
async def test_copilot_add_cancel_forwards_to_rpc():
    client = _make_fake_client()
    ro = RemoteOrchestrator(client)
    await ro.copilot_add_cancel(account="acct3")
    client.call.assert_called_once_with("copilot_add_cancel", account="acct3")


@pytest.mark.asyncio
async def test_process_quota_refresh_forwards_to_rpc():
    client = _make_fake_client()
    ro = RemoteOrchestrator(client)
    await ro.process_quota_refresh(account="acct4", force=True)
    client.call.assert_called_once_with(
        "process_quota_refresh", account="acct4", force=True, persist=True)

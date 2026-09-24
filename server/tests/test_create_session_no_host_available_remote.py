"""NoHostAvailable/NoEngineHostAvailable must survive the daemon RPC boundary as a
typed NoHostAvailable (#2209).

Before this fix, ``reconstruct_create_session_error`` only recognized
``AllAccountsBusy``, ``CloneError`` and ``GhError`` — a fleet-wide capacity refusal
crossed the edge boundary as a bare, un-reconstructed ``DaemonError``, so no caller
could pattern-match on it even if it wanted to (which is exactly what let it escape
``AutomationContext.spawn_session`` uncaught and reach ``pm_triage.tick()``'s
catch-all as a raw exception logged at WARNING).
"""
from __future__ import annotations

import pytest

from server.ipc.client import DaemonClient, DaemonError
from server.sessions.fleet_router import NoHostAvailable
from server.sessions.remote import RemoteOrchestrator


@pytest.mark.asyncio
async def test_remote_create_session_reraises_no_host_available():
    class _SaturatedClient:
        async def call(self, method, **params):
            raise DaemonError(
                "NoHostAvailable",
                "no enabled, healthy host with capacity; refusing to run in-process "
                "to avoid a split-brain over the shared database")

    ro = RemoteOrchestrator(_SaturatedClient())
    with pytest.raises(NoHostAvailable) as ei:
        await ro.create_session(name="x")
    assert "no enabled, healthy host with capacity" in str(ei.value)


@pytest.mark.asyncio
async def test_remote_create_session_reraises_no_engine_host_available_as_base_type():
    """The engine-specific subclass folds to the base type across the RPC boundary
    (see reconstruct_create_session_error) — every existing `except NoHostAvailable`
    still catches it, and the message already names the engine."""
    class _NoCodexClient:
        async def call(self, method, **params):
            raise DaemonError(
                "NoEngineHostAvailable",
                "no healthy host can run engine codex (rocky: codex not installed)")

    ro = RemoteOrchestrator(_NoCodexClient())
    with pytest.raises(NoHostAvailable) as ei:
        await ro.create_session(name="x")
    assert "codex" in str(ei.value)


@pytest.mark.asyncio
async def test_no_host_available_over_real_uds():
    """End-to-end: daemon raise → error envelope → DaemonError → reconstruction,
    over the real IPC transport."""
    from unittest import mock

    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.ipc_harness import serve_daemon_uds

    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)

    async def _boom(**kwargs):
        raise NoHostAvailable(
            "no enabled, healthy host with capacity; refusing to run in-process "
            "to avoid a split-brain over the shared database")

    orch.create_session = _boom  # type: ignore[method-assign]
    register_orchestrator_rpc(app, orch)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        with pytest.raises(NoHostAvailable):
            await ro.create_session(name="x")

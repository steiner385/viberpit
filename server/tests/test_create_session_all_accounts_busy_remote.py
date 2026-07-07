"""AllAccountsBusy must survive the daemon RPC boundary as its typed self.

In remote mode the daemon flattens exceptions to a type+message envelope, so a
typed ``AllAccountsBusy`` (which the edge's create handler catches to render a
clear "all accounts at limit, resets <date>" message) collapsed to a generic
DaemonError → opaque 500 → "Something went wrong." The remote client must
reconstruct AllAccountsBusy from the envelope — including ``earliest_reset`` and
``kind`` — exactly as ``run_command`` reconstructs ``KeyError``.
"""
from datetime import datetime, timezone

import pytest

from ccswitch.selector import AllAccountsBusy
from server.ipc.client import DaemonClient, DaemonError
from server.sessions.remote import RemoteOrchestrator


def test_all_accounts_busy_error_data_roundtrips():
    reset = datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)
    data = AllAccountsBusy(reset, kind="at_ceiling").error_data
    assert data == {"earliest_reset": reset.isoformat(), "kind": "at_ceiling"}
    assert AllAccountsBusy(None, kind="no_usable_accounts").error_data == {
        "earliest_reset": None, "kind": "no_usable_accounts"}


@pytest.mark.asyncio
async def test_remote_create_session_reraises_all_accounts_busy_with_reset():
    reset = datetime(2026, 7, 8, 21, 0, tzinfo=timezone.utc)

    class _BusyClient:
        async def call(self, method, **params):
            raise DaemonError("AllAccountsBusy", "all accounts at/above usage ceiling",
                              {"earliest_reset": reset.isoformat(), "kind": "at_ceiling"})

    ro = RemoteOrchestrator(_BusyClient())
    with pytest.raises(AllAccountsBusy) as ei:
        await ro.create_session(name="x")
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset == reset


@pytest.mark.asyncio
async def test_remote_create_session_reraises_no_usable_accounts():
    class _BusyClient:
        async def call(self, method, **params):
            raise DaemonError("AllAccountsBusy", "all accounts at/above usage ceiling",
                              {"earliest_reset": None, "kind": "no_usable_accounts"})

    ro = RemoteOrchestrator(_BusyClient())
    with pytest.raises(AllAccountsBusy) as ei:
        await ro.create_session(name="x")
    assert ei.value.kind == "no_usable_accounts"
    assert ei.value.earliest_reset is None


@pytest.mark.asyncio
async def test_remote_create_session_passes_through_other_daemon_errors():
    class _BoomClient:
        async def call(self, method, **params):
            raise DaemonError("ValueError", "cwd does not exist: /nope")

    ro = RemoteOrchestrator(_BoomClient())
    with pytest.raises(DaemonError):
        await ro.create_session(name="x")


@pytest.mark.asyncio
async def test_all_accounts_busy_over_real_uds():
    """End-to-end: daemon raise → error envelope (with data) → DaemonError →
    reconstruction, over the real IPC transport."""
    from unittest import mock
    from server.ipc.daemon import create_daemon_app
    from server.ipc.orchestrator_rpc import register_orchestrator_rpc
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.ipc_harness import serve_daemon_uds

    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    reset = datetime(2026, 7, 10, 17, 0, tzinfo=timezone.utc)

    async def _boom(**kwargs):
        raise AllAccountsBusy(reset, kind="at_ceiling")

    orch.create_session = _boom  # type: ignore[method-assign]
    register_orchestrator_rpc(app, orch)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        with pytest.raises(AllAccountsBusy) as ei:
            await ro.create_session(name="x")
    assert ei.value.kind == "at_ceiling"
    assert ei.value.earliest_reset == reset

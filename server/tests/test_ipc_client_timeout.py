"""A slow/wedged daemon RPC must surface as a typed DaemonUnavailable, never a
raw httpx.ReadTimeout bubbling to an unhandled 500 (incident 2026-07-02)."""
import asyncio

import pytest

from server.ipc.client import DaemonClient, DaemonUnavailable
from server.ipc.daemon import create_daemon_app, register_rpc
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_call_timeout_raises_daemon_unavailable():
    app = create_daemon_app()

    async def slow(params):
        await asyncio.sleep(5)
        return {}

    register_rpc(app, "slow", slow)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with pytest.raises(DaemonUnavailable):
                await client.call("slow", timeout=0.2)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_call_with_explicit_timeout_still_succeeds_fast():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            assert await client.call("ping", timeout=5.0) == {"pong": True}
        finally:
            await client.aclose()

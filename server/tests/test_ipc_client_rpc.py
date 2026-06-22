import pytest
from server.ipc.client import DaemonClient, DaemonUnavailable, DaemonError
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_call_ping_over_uds():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            assert await client.call("ping") == {"pong": True}
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_call_unavailable_when_no_daemon():
    client = DaemonClient("/nonexistent/ccsd.sock")
    with pytest.raises(DaemonUnavailable):
        await client.call("ping")
    await client.aclose()


@pytest.mark.asyncio
async def test_call_surfaces_handler_error():
    app = create_daemon_app()
    from server.ipc.daemon import register_rpc

    async def boom(params):
        raise ValueError("nope")

    register_rpc(app, "boom", boom)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            with pytest.raises(DaemonError) as ei:
                await client.call("boom")
            assert ei.value.err_type == "ValueError"
        finally:
            await client.aclose()

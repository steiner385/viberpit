import asyncio
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_stream_resumes_after_ws_drop():
    app = create_daemon_app()
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")
            await client.call("echo", stream="s1", text="before")
            assert (await asyncio.wait_for(stream.__anext__(), 2))["text"] == "before"

            # Force every server-side /events connection closed.
            for ws in list(app.state.subscribers.get("s1", ())):
                await ws.close()

            # The client reader should reconnect + re-subscribe; clear the ready gate
            # so we can await re-subscription, then a new echo must arrive.
            client._ready["s1"].clear()
            await client.events_ready("s1")
            await client.call("echo", stream="s1", text="after")
            assert (await asyncio.wait_for(stream.__anext__(), 3))["text"] == "after"
        finally:
            await client.aclose()

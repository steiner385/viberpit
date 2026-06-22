import asyncio
import pytest
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.tests.ipc_harness import serve_daemon_uds


@pytest.mark.asyncio
async def test_subscribe_receives_demo_ticks():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            stream = client.subscribe("s1")
            await client.events_ready("s1")          # subscription registered on the daemon
            await client.call("start_demo", stream="s1", n=3)
            got = [await asyncio.wait_for(stream.__anext__(), 2) for _ in range(3)]
        finally:
            await client.aclose()
    assert [e["i"] for e in got] == [0, 1, 2]


@pytest.mark.asyncio
async def test_two_streams_are_demuxed():
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            a = client.subscribe("a")
            b = client.subscribe("b")
            await client.events_ready("a")
            await client.events_ready("b")
            await client.call("echo", stream="b", text="forb")
            ev = await asyncio.wait_for(b.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "forb"}


@pytest.mark.asyncio
async def test_subscribe_after_connection_established():
    """A subscribe() issued AFTER the reader is already connected still registers
    (the dynamic-attach path A2 relies on), not only the streams present at connect."""
    async with serve_daemon_uds(create_daemon_app()) as sock:
        client = DaemonClient(sock)
        try:
            first = client.subscribe("first")
            await client.events_ready("first")        # reader is now connected
            later = client.subscribe("later")         # subscribe after connect
            await client.events_ready("later")
            await client.call("echo", stream="later", text="dynamic")
            ev = await asyncio.wait_for(later.__anext__(), 2)
        finally:
            await client.aclose()
    assert ev == {"type": "echo", "text": "dynamic"}


@pytest.mark.asyncio
async def test_deliver_drops_oldest_on_overflow():
    client = DaemonClient("/unused.sock")
    client._queues["s"] = asyncio.Queue(maxsize=2)
    client._deliver("s", {"i": 1})
    client._deliver("s", {"i": 2})
    client._deliver("s", {"i": 3})           # overflow → drop oldest ({"i":1})
    drained = [client._queues["s"].get_nowait() for _ in range(2)]
    assert drained == [{"i": 2}, {"i": 3}]
    await client.aclose()

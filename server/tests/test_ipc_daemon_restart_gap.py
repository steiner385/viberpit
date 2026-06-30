# server/tests/test_ipc_daemon_restart_gap.py
import asyncio
import contextlib
import os
import tempfile

import pytest
import uvicorn

from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app


async def _serve(app, sock):
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    return server, task


async def _stop(server, task):
    server.should_exit = True
    await task


@pytest.mark.asyncio
async def test_restart_window_events_survive_daemon_restart():
    tmp = tempfile.mkdtemp(prefix="ccsd-gap-")
    sock = os.path.join(tmp, "d.sock")
    received: list[dict] = []
    client = DaemonClient(sock)

    drain_task = None
    srv_b = None
    task_b = None

    app_a = create_daemon_app()
    srv_a, task_a = await _serve(app_a, sock)
    try:
        stream = client.subscribe("s1")
        await client.events_ready("s1")

        async def drain():
            async for ev in stream:
                if ev.get("type") != "_ipc_reconnect":   # ignore Task-3 sentinel if present
                    received.append(ev)
        drain_task = asyncio.create_task(drain())

        for i in range(6):                      # daemon A: seq 0..5
            await app_a.state.publish("s1", {"type": "textDelta", "i": i})
        await asyncio.sleep(0.3)
        assert sorted(e["i"] for e in received) == [0, 1, 2, 3, 4, 5]

        # ---- simulate a DAEMON RESTART on the same socket (seq resets to 0) ----
        await _stop(srv_a, task_a)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        app_b = create_daemon_app()
        for i in (100, 101, 102):               # restart-window events buffered at seq 0,1,2
            await app_b.state.publish("s1", {"type": "textDelta", "i": i})
        srv_b, task_b = await _serve(app_b, sock)
        await asyncio.sleep(2.5)                 # client reconnects + resubscribes

        got = {e["i"] for e in received}
        assert {100, 101, 102} <= got, f"restart-window events lost: {sorted(got)}"

    finally:
        if drain_task is not None:
            drain_task.cancel()
        if srv_b is not None:
            with contextlib.suppress(Exception):
                await _stop(srv_b, task_b)
        await client.aclose()
        with contextlib.suppress(Exception):
            os.unlink(sock)
            os.rmdir(tmp)


@pytest.mark.asyncio
async def test_reconcile_epoch_resets_watermarks_on_change():
    client = DaemonClient("/nonexistent.sock")  # no I/O; we drive _reconcile_epoch directly
    client._last_seq["s1"] = 42
    client._baseline["s1"] = 42

    async def fake_info_same():
        return {"epoch": "E1"}

    client.info = fake_info_same          # type: ignore[assignment]
    await client._reconcile_epoch()       # first sight of E1 → record, no reset
    assert client._last_seq.get("s1") == 42
    assert client._epoch == "E1"

    async def fake_info_new():
        return {"epoch": "E2"}

    client.info = fake_info_new           # type: ignore[assignment]
    await client._reconcile_epoch()       # epoch changed → drop stale watermarks
    assert "s1" not in client._last_seq
    assert client._baseline["s1"] is None
    assert client._epoch == "E2"
    await client.aclose()

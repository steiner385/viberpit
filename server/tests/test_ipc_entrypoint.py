import asyncio
import os
import sys
import tempfile
import pytest
from server.ipc.client import DaemonClient


@pytest.mark.asyncio
async def test_entrypoint_serves_ping_over_uds():
    tmpdir = tempfile.mkdtemp(prefix="ccsd-ep-")
    sock = os.path.join(tmpdir, "d.sock")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "server.ipc", sock,
        env={**os.environ, "PYTHONPATH": "src:server"},
    )
    try:
        for _ in range(100):                      # wait for the socket to appear
            if os.path.exists(sock):
                break
            await asyncio.sleep(0.05)
        client = DaemonClient(sock)
        try:
            assert await client.call("ping") == {"pong": True}
        finally:
            await client.aclose()
    finally:
        proc.terminate()
        await proc.wait()
        for p in (sock, tmpdir):
            try:
                os.remove(p) if os.path.isfile(p) else os.rmdir(p)
            except OSError:
                pass

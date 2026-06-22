"""Run a daemon app on a temp Unix socket via in-process uvicorn, for tests."""
import asyncio
import contextlib
import os
import tempfile

import uvicorn


@contextlib.asynccontextmanager
async def serve_daemon_uds(app):
    tmpdir = tempfile.mkdtemp(prefix="ccsd-")
    sock = os.path.join(tmpdir, "d.sock")
    server = uvicorn.Server(uvicorn.Config(app, uds=sock, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield sock
    finally:
        server.should_exit = True
        await task
        with contextlib.suppress(FileNotFoundError):
            os.unlink(sock)
        with contextlib.suppress(OSError):
            os.rmdir(tmpdir)

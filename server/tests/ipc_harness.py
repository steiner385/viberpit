"""Run a daemon app on a temp Unix socket or mTLS TCP port via in-process uvicorn, for tests."""
import asyncio
import contextlib
import os
import socket as _socket
import tempfile

import uvicorn

from server.ipc.paths import Endpoint
from server.ipc.serve import uvicorn_run_kwargs
from server.ipc.tls import DaemonTLSFiles


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


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.asynccontextmanager
async def serve_daemon_tls(app, certs):
    """Serve `app` over mTLS on an ephemeral 127.0.0.1 port; yield its Endpoint."""
    endpoint = Endpoint("https", host="127.0.0.1", port=_free_port())
    tls = DaemonTLSFiles(cert=certs.server_cert, key=certs.server_key, ca=certs.ca)
    kwargs = uvicorn_run_kwargs(endpoint, daemon_tls=tls)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off", **kwargs))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield endpoint
    finally:
        server.should_exit = True
        await task

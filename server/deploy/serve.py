"""Production entrypoint plumbing for the deploy controller: build the app, and serve it
on a Unix socket."""
from __future__ import annotations
import logging
import os

import uvicorn

from server.deploy.app import build_controller_app
from server.deploy.__main__ import _daemon_enabled

log = logging.getLogger("server.deploy.serve")
DEFAULT_DEPLOY_SOCKET = "/run/ccswitch/deploy.sock"


def deploy_socket() -> str:
    """Return the UDS socket path for the deploy controller, from CCSWITCH_DEPLOY_SOCKET env
    or the default /run/ccswitch/deploy.sock."""
    return os.environ.get("CCSWITCH_DEPLOY_SOCKET", DEFAULT_DEPLOY_SOCKET)


def serve(socket_path: str | None = None) -> None:
    """Build the deploy controller app and serve it on a Unix socket."""
    sock = socket_path or deploy_socket()
    repo = os.environ.get("CCSWITCH_DEPLOY_REPO", os.getcwd())
    host = os.environ.get("CCSWITCH_HOST", "127.0.0.1")
    port = os.environ.get("CCSWITCH_PORT", "8787")
    venv_pip = os.environ.get("CCSWITCH_DEPLOY_VENV_PIP", os.path.join(repo, ".venv", "bin", "pip"))
    daemon_enabled = _daemon_enabled()
    app, _, _ = build_controller_app(repo=repo, health_url=f"http://{host}:{port}/healthz",
                                     venv_pip=venv_pip, daemon_enabled=daemon_enabled)
    log.info("deploy controller serving on %s", sock)
    uvicorn.run(app, uds=sock, log_level="info")

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
# Same directory as the socket: the unit's RuntimeDirectory=ccswitch makes it writable
# and RuntimeDirectoryPreserve=yes keeps it across the controller's own restart — the
# exact window the persisted status must survive (#865). Wiped on reboot, which is
# fine: after a reboot "no deploy in progress" is the truth.
DEFAULT_DEPLOY_STATUS_PATH = "/run/ccswitch/deploy-status.json"


def deploy_socket() -> str:
    """Return the UDS socket path for the deploy controller, from CCSWITCH_DEPLOY_SOCKET env
    or the default /run/ccswitch/deploy.sock."""
    return os.environ.get("CCSWITCH_DEPLOY_SOCKET", DEFAULT_DEPLOY_SOCKET)


def deploy_status_path() -> str:
    """Where the controller persists its DeployStatus across its own self-restart, from
    CCSWITCH_DEPLOY_STATUS_PATH env or the default under /run/ccswitch."""
    return os.environ.get("CCSWITCH_DEPLOY_STATUS_PATH", DEFAULT_DEPLOY_STATUS_PATH)


def serve(socket_path: str | None = None) -> None:
    """Build the deploy controller app and serve it on a Unix socket."""
    # The deploy gate's gh reads (ci_verdict / publish_deployment) run in THIS process,
    # so register the App seams here too (issue #912/#913). Without this, _app_token()
    # returns None and the reads fall back to the pinned ccswitch gh identity — already
    # safe (ambient GITHUB_TOKEN is stripped), but the App token is the intended auth.
    from server.github import app_auth as _app_auth
    _app_auth.register_git_providers()
    sock = socket_path or deploy_socket()
    repo = os.environ.get("CCSWITCH_DEPLOY_REPO", os.getcwd())
    host = os.environ.get("CCSWITCH_HOST", "127.0.0.1")
    port = os.environ.get("CCSWITCH_PORT", "8787")
    venv_python = os.environ.get("CCSWITCH_DEPLOY_VENV_PYTHON", os.path.join(repo, ".venv", "bin", "python"))
    daemon_enabled = _daemon_enabled()
    app, _, _ = build_controller_app(repo=repo, health_url=f"http://{host}:{port}/healthz",
                                     venv_python=venv_python, daemon_enabled=daemon_enabled,
                                     status_path=deploy_status_path(),
                                     init_db_url=os.environ.get("CCSWITCH_DB_URL", ""))
    log.info("deploy controller serving on %s", sock)
    uvicorn.run(app, uds=sock, log_level="info")

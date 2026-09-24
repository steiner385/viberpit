"""Production entrypoint plumbing for the deploy controller: build the app, and serve it
on a Unix socket."""
from __future__ import annotations
import logging
import os

import uvicorn

from server.deploy.app import build_controller_app
from server.deploy.client import default_socket_path
from server.deploy.__main__ import _daemon_enabled
from server.hostpaths import DEPLOY_STATUS_PATH as DEFAULT_DEPLOY_STATUS_PATH
from server.http_keepalive import SERVER_KEEPALIVE_TIMEOUT_S

log = logging.getLogger("server.deploy.serve")


def deploy_socket() -> str:
    """The controller's UDS socket path — the same resolution the edge's client uses."""
    return default_socket_path()


def deploy_status_path() -> str:
    """Where the controller persists its DeployStatus across its own self-restart, from
    VIBERPIT_DEPLOY_STATUS_PATH env or the default under hostpaths.RUN_DIR."""
    return os.environ.get("VIBERPIT_DEPLOY_STATUS_PATH", DEFAULT_DEPLOY_STATUS_PATH)


def serve(socket_path: str | None = None) -> None:
    """Build the deploy controller app and serve it on a Unix socket."""
    # The deploy gate's gh reads (ci_verdict / publish_deployment) run in THIS process,
    # so register the App seams here too (issue #912/#913). Without this, _app_token()
    # returns None and the reads fall back to the pinned ViberPit gh identity — already
    # safe (ambient GITHUB_TOKEN is stripped), but the App token is the intended auth.
    from server.github import app_auth as _app_auth
    _app_auth.register_git_providers()
    sock = socket_path or deploy_socket()
    repo = os.environ.get("VIBERPIT_DEPLOY_REPO", os.getcwd())
    host = os.environ.get("VIBERPIT_HOST", "127.0.0.1")
    port = os.environ.get("VIBERPIT_PORT", "8787")
    venv_python = os.environ.get("VIBERPIT_DEPLOY_VENV_PYTHON", os.path.join(repo, ".venv", "bin", "python"))
    daemon_enabled = _daemon_enabled()
    app, _, _ = build_controller_app(repo=repo, health_url=f"http://{host}:{port}/healthz",
                                     venv_python=venv_python, daemon_enabled=daemon_enabled,
                                     status_path=deploy_status_path(),
                                     init_db_url=os.environ.get("VIBERPIT_DB_URL", ""))
    log.info("deploy controller serving on %s", sock)
    # timeout_keep_alive must outlive the edge's client-pool expiry — server/http_keepalive.py.
    uvicorn.run(app, uds=sock, log_level="info", timeout_keep_alive=SERVER_KEEPALIVE_TIMEOUT_S)

"""Upgrade one remote session-runner from the Fleet UI, on demand.

The Fleet "Update" button and the deploy fan-out share ONE implementation —
``server.deploy.runners.RunnerDeploy`` — so a manual upgrade and a deploy can never
disagree about how a runner is brought to a commit. This module only resolves the
target (the fleet's deployed sha, see ``server.deploy.state``) and keeps the in-process
progress dict the ``/api/fleet/hosts`` list polls.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from server.deploy import git as deploy_git
from server.deploy.client import DeployControllerClient, default_socket_path
from server.deploy.runners import RunnerDeploy, RunnerTarget
from server.version import repo_root

log = logging.getLogger(__name__)

__all__ = ["start_upgrade", "run_upgrade", "upgrade_wire", "is_running", "default_target_sha",
           "restart_expected"]

_STATE: dict[str, dict[str, Any]] = {}


def _reset_state_for_tests() -> None:
    _STATE.clear()
    _controller_cache.update(at=0.0, hosts={})


def is_running(name: str) -> bool:
    st = _STATE.get(name)
    return bool(st and st.get("state") == "running")


# --------------------------------------------------------------------------- #
# "Is this host down ON PURPOSE right now?" — the one predicate the fleet's failure
# paths (fleet_orchestrator, host_probe) consult before logging a worker as
# unreachable. A runner restarts on every deploy and is unreachable for 30–90 s each
# time; logging every failed poll in that window at WARNING turned 19 restarts in
# 48 h into ~100 warnings and an Error Triage issue about a daemon that was doing
# exactly what it was told (#1614). Two things restart a host deliberately, and both
# must be visible here: the Fleet "Update" button (this module's own `_STATE`) and the
# deploy controller's fan-out — a SEPARATE process, read through its status socket.
# --------------------------------------------------------------------------- #

# One status read per burst of failures, not one per failure: a restart window fails
# two RPCs every 5 s plus the probe, and the controller is busy driving the deploy.
_CONTROLLER_STATUS_TTL_S = 5.0
_controller_cache: dict[str, Any] = {"at": 0.0, "hosts": {}}
_controller_client: DeployControllerClient | None = None


async def _default_controller_status() -> dict:
    global _controller_client
    if _controller_client is None:
        _controller_client = DeployControllerClient(default_socket_path())
    return await _controller_client.get_status()


async def _controller_hosts(fetch: Callable[[], Awaitable[dict]]) -> dict[str, dict]:
    now = time.monotonic()
    if now - _controller_cache["at"] < _CONTROLLER_STATUS_TTL_S:
        return _controller_cache["hosts"]
    try:
        # The controller's /status is {"repo", "pending", "deploy": DeployStatus}; the
        # per-host fan-out lives under "deploy". Reading a top-level "hosts" key made every
        # controller-driven restart look like "no deploy" and re-filed #1614 as #2181.
        hosts = ((await fetch()).get("deploy") or {}).get("hosts") or {}
    except Exception:  # noqa: BLE001 — a logging decision must never raise
        # No controller, or one that could not answer: no evidence of a deploy. Cached
        # like a real answer so an absent controller is not re-dialled per failure.
        hosts = {}
    _controller_cache.update(at=now, hosts=hosts)
    return hosts


async def restart_expected(name: str, *,
                           controller_status: Callable[[], Awaitable[dict]] | None = None,
                           ) -> bool:
    """True while ``name`` is being brought to a commit by the Fleet UI or the deploy
    controller — i.e. its daemon being unreachable is expected, not an incident."""
    if is_running(name):
        return True
    hosts = await _controller_hosts(controller_status or _default_controller_status)
    return (hosts.get(name) or {}).get("state") == "running"


def upgrade_wire(name: str) -> dict[str, Any]:
    st = _STATE.get(name)
    if not st:
        return {"state": "idle", "step": None, "error": None, "startedAt": None, "target": None}
    return {"state": st["state"], "step": st["step"], "error": st["error"],
            "startedAt": st["started_at"], "target": st["target"]}


def _origin_master_full(run=subprocess.run) -> str:
    """origin/master's tip as a FULL sha — same reason as the deploy_state branch below."""
    deploy_git.fetch(repo_root(), run=run)
    return deploy_git.remote_sha(repo_root(), "origin/master", run=run)


async def default_target_sha(resolve_origin: Callable[[], str] = _origin_master_full) -> str:
    """The fleet's deployed sha (deploy_state), else origin/master's tip for a fleet that
    has never recorded a deploy. Never a commit AHEAD of what the edge runs.

    Returns the FULL sha, not ``deployed_short``: a short sha is only minimal-unique on
    the edge that abbreviated it, and lands in ``git merge --ff-only <target>`` on a
    DIFFERENT host — one that may need more hex digits to disambiguate the same commit
    (steps.short_sha_match's own docstring: dobby abbreviated to 8 chars, rocky to 7).
    RunnerDeploy._verify is prefix-based, so a full target still verifies fine against a
    short ``/info`` sha. BOTH branches return a full sha — the no-deploy_state fallback
    resolves ``origin/master`` with ``rev-parse`` (not ``rev-parse --short``) for exactly
    the same reason.
    """
    from server.deploy.state import read_deploy_state
    state = await read_deploy_state()
    if state is not None:
        return state.deployed_sha
    return await asyncio.to_thread(resolve_origin)


async def run_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str,
                      run=subprocess.run, fetch_info=None,
                      resolve_target: Callable[[], Awaitable[str]] | None = None,
                      **runner_kwargs: Any) -> dict[str, Any]:
    """Run the whole upgrade for one host; never raises (state records failure)."""
    st = _STATE.setdefault(name, {})
    st.update(state="running", step="fetch", error=None, target=None,
              started_at=datetime.now(timezone.utc).isoformat())
    try:
        target = await (resolve_target() if resolve_target else default_target_sha())
        st["target"] = target
        rt = RunnerTarget(name=name, endpoint=endpoint, enabled=True, healthy=True,
                          ssh_host=ssh_host, repo_path=repo_path)

        def on_progress(_n: str, p) -> None:
            st.update(state=p.state if p.state in ("running", "done", "failed") else "failed",
                      step=p.step, error=p.error)

        rd = RunnerDeploy(rt, local_name="", run=run, fetch_info=fetch_info,
                          on_progress=on_progress, **runner_kwargs)
        # A manual upgrade always restarts (the operator asked for the new code to RUN) and
        # always syncs deps (it cannot know what changed since the runner's last update).
        p = await rd.deploy(target_sha=target, daemon_changed=True, deps_changed=True,
                            local_daemon_enabled=True)
        if p.state == "done":
            st.update(state="done", step=None)
            log.info("fleet.upgrade_done host=%s sha=%s", name, p.sha)
        else:
            st.update(state="failed", step=p.step, error=p.error)
            log.warning("fleet.upgrade_failed host=%s step=%s: %s", name, p.step, p.error)
        return st
    except Exception as exc:  # noqa: BLE001
        st.update(state="failed", error=str(exc)[:500])
        log.warning("fleet.upgrade_failed host=%s: %s", name, exc)
        return st


def start_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str, **kw: Any) -> bool:
    if is_running(name):
        return False
    _STATE.setdefault(name, {}).update(
        state="running", step="fetch", error=None, target=None,
        started_at=datetime.now(timezone.utc).isoformat())
    task = asyncio.get_running_loop().create_task(
        run_upgrade(name, ssh_host=ssh_host, repo_path=repo_path, endpoint=endpoint, **kw))
    _STATE[name]["_task"] = task
    return True

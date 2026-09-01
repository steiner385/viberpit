"""Upgrade a remote session-runner's daemon over ssh, triggered from the app.

Deploys only update the edge host; a remote runner's checkout goes stale until an
operator ssh-es in by hand (rocky sat on an Aug-30 sha for two days). This module
runs the same update sequence the deploy controller uses for its single targeted
remote daemon (``server.deploy.steps.restart_units``), but per registry host and
on demand: fetch → ff-only merge → pip (editable, ``python -m pip``) → sync →
``sudo -n systemctl restart``, then verify the daemon's ``/info`` reports the
target sha.

Everything that lands in ssh argv is validated upstream (the /api/fleet PATCH
rejects values not matching ``server.deploy.steps.SSH_HOST_RE`` / ``SSH_REPO_RE``),
and commands are built as argv lists — no shell interpolation on the local side.

State is tracked in-process, per host: an upgrade survives only as long as the
edge process, which is fine — it runs for ~a minute and the UI polls it live via
``GET /api/fleet/hosts``.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from server.deploy import git as deploy_git
from server.deploy.steps import short_sha_match
from server.sessions.host_probe import Fetch, default_fetch

log = logging.getLogger(__name__)

Run = Callable[..., subprocess.CompletedProcess]

__all__ = ["start_upgrade", "run_upgrade", "upgrade_wire", "is_running"]

# The edge's own checkout — where origin/master's target sha is resolved from.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])

# BatchMode: never hang on an interactive prompt from a background task.
# ConnectTimeout: an unreachable host must fail the step, not wedge the upgrade.
_SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10")

# Total budget for the daemon to come back reporting the target sha. systemctl
# restart returns when the process STARTS, not when uvicorn is serving; rocky
# needs ~10-30s more to bind its port (see steps.HEALTH_ATTEMPTS_WITH_DAEMON).
VERIFY_TIMEOUT_S = 90.0
VERIFY_INTERVAL_S = 3.0

# Per-host upgrade state, merged into the /api/fleet/hosts wire format so the UI
# can poll progress. In-process on purpose (see module docstring).
_STATE: dict[str, dict[str, Any]] = {}


def _reset_state_for_tests() -> None:
    _STATE.clear()


def is_running(name: str) -> bool:
    st = _STATE.get(name)
    return bool(st and st.get("state") == "running")


def upgrade_wire(name: str) -> dict[str, Any]:
    """The host's upgrade state in wire (camelCase) shape; idle when never run."""
    st = _STATE.get(name)
    if not st:
        return {"state": "idle", "step": None, "error": None,
                "startedAt": None, "target": None}
    return {"state": st["state"], "step": st["step"], "error": st["error"],
            "startedAt": st["started_at"], "target": st["target"]}


def default_target_sha(run: Run = subprocess.run) -> str:
    """Short sha of origin/master, freshly fetched, from the edge's own checkout."""
    deploy_git.fetch(_REPO_ROOT, run=run)
    return deploy_git.short_sha(_REPO_ROOT, "origin/master", run=run)


def _remote_commands(repo_path: str) -> list[tuple[str, list[str]]]:
    """The ordered (step, remote argv) sequence run over ssh.

    pip is ALWAYS invoked as ``<venv>/bin/python -m pip`` — never the ``bin/pip``
    launcher, whose shebang bakes in the venv's creation-time absolute path and
    ENOENTs after the venv moves (PR #860 / steps.sync_deps_if_changed).

    ``sync`` flushes the rewritten checkout to disk BEFORE the restart: rocky's
    2026-08-29 hard reset mid-window replayed a 7,864-line module back as zero
    bytes and crash-looped the daemon (see steps.restart_units).

    The pip requirement is single-quoted because ssh joins argv into one remote
    shell command line: ``[server]`` is a glob pattern there, and an unquoted
    path that happens to match something would be rewritten. The path itself is
    SSH_REPO_RE-validated (no quotes/metacharacters), so the quoting is inert.
    """
    venv_python = f"{repo_path}/.venv/bin/python"
    return [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("merge", ["git", "-C", repo_path, "merge", "--ff-only", "origin/master"]),
        ("pip", [venv_python, "-m", "pip", "install", "-e",
                 f"'{repo_path}[server]'", "--quiet"]),
        ("sync", ["sync"]),
        ("restart", ["sudo", "-n", "systemctl", "restart", "ccswitch-daemon.service"]),
    ]


async def run_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str,
                      run: Run = subprocess.run,
                      fetch_info: Fetch | None = None,
                      resolve_target: Callable[[], str] | None = None,
                      verify_timeout_s: float = VERIFY_TIMEOUT_S,
                      verify_interval_s: float = VERIFY_INTERVAL_S) -> dict[str, Any]:
    """Run the whole upgrade for one host; never raises (state records failure).

    Blocking work (ssh subprocesses, the local git resolve) runs via
    ``asyncio.to_thread`` so the request loop is never blocked.
    """
    fetch_info = fetch_info or default_fetch
    resolve_target = resolve_target or default_target_sha
    st = _STATE.setdefault(name, {})
    st.update(state="running", step="fetch", error=None, target=None,
              started_at=datetime.now(timezone.utc).isoformat())
    loop = asyncio.get_running_loop()
    try:
        target = await asyncio.to_thread(resolve_target)
        st["target"] = target
        for step, remote in _remote_commands(repo_path):
            st["step"] = step
            argv = ["ssh", *_SSH_OPTS, ssh_host, *remote]
            r = await asyncio.to_thread(run, argv, capture_output=True, text=True)
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "").strip()[:500]
                raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")
        # Verify: the restarted daemon must come back REPORTING the target sha —
        # a restart that relaunches stale code is the failure this feature exists
        # to end, so "restarted" alone is not success.
        st["step"] = "verify"
        deadline = loop.time() + verify_timeout_s
        while True:
            try:
                info = await fetch_info(endpoint)
                got = info.get("sha")
                # Prefix match, not ==: hosts abbreviate the same commit to
                # different lengths (steps.short_sha_match, #788).
                if got and short_sha_match(str(got), target):
                    st.update(state="done", step=None)
                    log.info("fleet.upgrade_done host=%s sha=%s", name, got)
                    return st
            except Exception:
                pass  # daemon still restarting — keep polling until the deadline
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"daemon did not report sha {target} within "
                    f"{int(verify_timeout_s)}s of restart")
            await asyncio.sleep(verify_interval_s)
    except Exception as exc:
        st.update(state="failed", error=str(exc)[:500])
        log.warning("fleet.upgrade_failed host=%s step=%s: %s", name, st.get("step"), exc)
        return st


def start_upgrade(name: str, *, ssh_host: str, repo_path: str, endpoint: str,
                  **kw: Any) -> bool:
    """Start a background upgrade for *name*. False if one is already running.

    Marks the host ``running`` synchronously BEFORE scheduling the task, so two
    concurrent POSTs cannot both start an upgrade.
    """
    if is_running(name):
        return False
    _STATE.setdefault(name, {}).update(
        state="running", step="fetch", error=None, target=None,
        started_at=datetime.now(timezone.utc).isoformat())
    task = asyncio.get_running_loop().create_task(
        run_upgrade(name, ssh_host=ssh_host, repo_path=repo_path,
                    endpoint=endpoint, **kw))
    # Keep a reference so the task is not garbage-collected mid-flight; the
    # per-host state dict outlives it either way.
    _STATE[name]["_task"] = task
    return True

"""Per-runner deploy lifecycle (spec §2): snapshot → update checkout → (drain, restart)
→ verify → restore, for every host in the fleet registry, local or ssh-reachable.

Transport rules (constitution IV — everything in ssh argv is validated first):
  * remote checkouts move by ``merge --ff-only <sha>`` only; ``reset --hard`` exists solely
    for the rollback rewind;
  * pip is ``<repo>/.venv/bin/python -m pip`` (never ``bin/pip``, #860) and only when the
    lockfile changed;
  * ``sync`` after every checkout write, before the restart (2026-08-29 zero-byte module).

A runner is restarted ONLY on a daemon-changing deploy: ``steps._EDGE_ONLY`` guarantees
an edge-only diff touched no daemon-imported Python, so the running process already
equals the new checkout's daemon code. The checkout is updated on EVERY deploy so
``/info.checkoutSha`` matches the deployed commit and the skew gate (spec §4) stays green.

``deploy_runners`` runs at most ``concurrency`` runners at once (decision 2a: 2) and
isolates failures — one runner's failure never stops another (decision 1a).
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from server.deploy import drain as _drain
from server.deploy.steps import (
    LOCAL_DAEMON_RESTART, SSH_HOST_RE, SSH_REPO_RE, _SHA_RE, short_sha_match,
)

log = logging.getLogger("server.deploy.runners")

Run = Callable[..., subprocess.CompletedProcess]
Fetch = Callable[[str], Awaitable[dict]]
Progress = Callable[[str, "HostProgress"], None]

SSH_OPTS = ("-oBatchMode=yes", "-oConnectTimeout=10")
RUNNER_UNIT = "ccswitch-daemon.service"
VERIFY_TIMEOUT_S = 90.0
VERIFY_INTERVAL_S = 3.0
DEFAULT_CONCURRENCY = 2

SKIPPED_STATES = frozenset({"offline", "disabled"})
FAILED_STATES = frozenset({"failed", "unconfigured"})


@dataclass(frozen=True)
class RunnerTarget:
    name: str
    endpoint: str
    enabled: bool
    healthy: bool | None
    ssh_host: str | None
    repo_path: str | None
    # The probe's last_error, verbatim. Load-bearing: `healthy is False` alone cannot tell
    # "unreachable" from "reached fine, but FENCED by the skew/protocol gate" — see
    # is_deployable.
    last_error: str | None = None


@dataclass
class HostProgress:
    state: str = "pending"        # pending | running | done | failed | offline | disabled | unconfigured
    step: str | None = None
    error: str | None = None
    sha: str | None = None
    moved: bool = False           # checkout advanced — a rollback must rewind it
    restore: dict | None = None

    def to_wire(self) -> dict:
        return {"state": self.state, "step": self.step, "error": self.error, "sha": self.sha,
                "restore": self.restore}


def _venv_python(repo_path: str) -> str:
    return f"{repo_path}/.venv/bin/python"


def _validate_repo(repo_path: str) -> None:
    if not SSH_REPO_RE.match(repo_path):
        raise ValueError(f"repo_path {repo_path!r} is not a safe absolute path")


def _validate(repo_path: str, sha: str) -> None:
    _validate_repo(repo_path)
    if not _SHA_RE.match(sha):
        raise ValueError(f"sha {sha!r} is not a hex git sha")


def remote_update_commands(repo_path: str, target_sha: str, *,
                           deps_changed: bool) -> list[tuple[str, list[str]]]:
    """Ordered (step, remote argv) to bring a runner's checkout to *target_sha*."""
    _validate(repo_path, target_sha)
    cmds: list[tuple[str, list[str]]] = [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("merge", ["git", "-C", repo_path, "merge", "--ff-only", target_sha]),
    ]
    if deps_changed:
        # Single-quoted: ssh joins argv into one remote shell line and `[server]` is a
        # glob there. The path is SSH_REPO_RE-validated, so the quotes are inert.
        cmds.append(("pip", [_venv_python(repo_path), "-m", "pip", "install", "-e",
                             f"'{repo_path}[server]'", "--quiet"]))
    cmds.append(("sync", ["sync"]))
    return cmds


def remote_rewind_commands(repo_path: str, sha: str) -> list[tuple[str, list[str]]]:
    """Rollback only: move a runner BACK to *sha* (ff-only cannot go backwards).

    A rollback sha lands in ssh argv exactly like a forward one, so it gets the same
    repo-path + hex-sha validation before any command runs.
    """
    _validate(repo_path, sha)
    return [
        ("fetch", ["git", "-C", repo_path, "fetch", "--quiet", "origin"]),
        ("reset", ["git", "-C", repo_path, "reset", "--hard", sha]),
        ("sync", ["sync"]),
    ]


REMOTE_RESTART = ["sudo", "-n", "systemctl", "restart", RUNNER_UNIT]
# The local daemon-restart argv lives in steps.LOCAL_DAEMON_RESTART (steps.py's own
# restart_local_daemon uses it too) and is referenced directly at the call sites below, so
# the two callers can never drift apart.


def is_deployable(t: RunnerTarget) -> bool:
    """Is this host something a deploy should try to update?

    NOT the same question as "is it dispatchable". The probe sets healthy=False for two
    quite different situations:

    * the host could not be REACHED (``ConnectError: …``) — nothing to deploy to; and
    * the host answered perfectly well but is FENCED by the version-skew or protocol gate.

    The fenced case is precisely the host a deploy must fix. Treating `healthy is not True`
    as "offline" meant a runner that failed one deploy was silently skipped by every later
    one — and the deploy still reported green, so the host stayed fenced forever.
    """
    # Imported lazily: host_probe pulls in the ORM/session layer, which this module (and
    # its tests) otherwise never need.
    from server.sessions.host_probe import FENCED_REACHABLE_PREFIXES
    if not t.enabled:
        return False
    if t.healthy is True:
        return True
    return (t.healthy is False and t.last_error is not None
            and t.last_error.startswith(FENCED_REACHABLE_PREFIXES))


def is_local(target: RunnerTarget, local_name: str) -> bool:
    """The daemon on THIS host: its checkout is the controller's own repo (already updated
    by the `update` phase) and its unit restarts without ssh."""
    return target.ssh_host is None and target.name == local_name


def _default_client_factory(endpoint: str) -> Any:
    from server.ipc.client import DaemonClient
    from server.ipc.paths import parse_endpoint
    return DaemonClient(parse_endpoint(endpoint))


def _default_fetch_info() -> Fetch:
    from server.sessions.host_probe import default_fetch
    return default_fetch


class RunnerDeploy:
    def __init__(self, target: RunnerTarget, *, local_name: str, run: Run = subprocess.run,
                 client_factory: Callable[[str], Any] | None = None,
                 fetch_info: Fetch | None = None, drain_mod=_drain,
                 verify_timeout_s: float = VERIFY_TIMEOUT_S,
                 verify_interval_s: float = VERIFY_INTERVAL_S,
                 on_progress: Progress | None = None):
        self.target = target
        self.local_name = local_name
        self._run = run
        self._client_factory = client_factory or _default_client_factory
        self._fetch_info: Fetch = fetch_info or _default_fetch_info()
        self._drain = drain_mod
        self._verify_timeout_s = verify_timeout_s
        self._verify_interval_s = verify_interval_s
        self._on_progress = on_progress
        self.progress = HostProgress()

    # ---- bookkeeping -------------------------------------------------------------
    def _mark(self, state: str | None = None, step: str | None = None, **kw: Any) -> None:
        if state is not None:
            self.progress.state = state
        self.progress.step = step
        for k, v in kw.items():
            setattr(self.progress, k, v)
        if self._on_progress is not None:
            try:
                self._on_progress(self.target.name, self.progress)
            except Exception:
                log.debug("runner %s: on_progress callback failed", self.target.name, exc_info=True)

    # ---- transport ---------------------------------------------------------------
    async def _ssh(self, step: str, remote: list[str]) -> None:
        ssh_host = self.target.ssh_host
        if not ssh_host or not SSH_HOST_RE.match(ssh_host):
            raise RuntimeError(f"ssh_host {ssh_host!r} is not a valid [user@]host")
        argv = ["ssh", *SSH_OPTS, ssh_host, *remote]
        r = await asyncio.to_thread(self._run, argv, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    async def _local(self, step: str, argv: list[str]) -> None:
        r = await asyncio.to_thread(self._run, argv, capture_output=True, text=True)
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "").strip()[:500]
            raise RuntimeError(f"{step} failed (rc={r.returncode}): {detail}")

    async def _close_quietly(self, client: Any) -> None:
        """Release a per-call daemon client — one helper, every call site.

        Each client owns two ``httpx.AsyncClient``s, their transports and (on the TLS
        branch) certifi-loaded SSL contexts; left unclosed they hold their sockets until
        GC, the leak ``DaemonClient.aclose`` exists to prevent.

        Tolerant by design, and load-bearingly so: this runs inside ``deploy``'s try, so a
        raising ``aclose`` would mark the runner FAILED — and since the fan-out gates the
        deploy, fail the whole deploy over teardown. The factory is also an injection
        point whose test doubles are plain objects with only ``call``, so a missing
        ``aclose`` must be a no-op. Best-effort, logged at debug.
        """
        closer = getattr(client, "aclose", None)
        if closer is None:
            return
        try:
            await closer()
        except Exception:  # noqa: BLE001 — teardown must never fail a deploy
            log.debug("runner %s: client teardown failed", self.target.name, exc_info=True)

    async def _rpc(self, method: str) -> dict:
        client = self._client_factory(self.target.endpoint)
        try:
            return await client.call(method)
        finally:
            await self._close_quietly(client)

    async def _drain_then(self) -> None:
        """Drain in-flight turns (bounded by the deploy caps) and drop the edge's /events
        socket so the old daemon can exit on SIGTERM promptly (#740). Best-effort."""
        self._mark(step="drain")
        client = self._client_factory(self.target.endpoint)
        try:
            result = await self._drain.drain_and_wait(client)
        finally:
            await self._close_quietly(client)
        if result != "ready":
            log.warning("runner %s: drain %s — restarting anyway", self.target.name, result)
        try:
            await self._rpc("close_events_connections")
        except Exception:
            log.debug("runner %s: close_events_connections failed (best-effort)",
                      self.target.name, exc_info=True)

    async def _verify(self, target_sha: str, *, require_process: bool) -> str:
        """Poll /info until the checkout (and, after a restart, the process) is on target."""
        self._mark(step="verify")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._verify_timeout_s
        while True:
            try:
                info = await self._fetch_info(self.target.endpoint)
                checkout = info.get("checkoutSha") or info.get("sha")
                process = info.get("sha")
                ok = bool(checkout) and short_sha_match(str(checkout), target_sha)
                if ok and require_process:
                    ok = bool(process) and short_sha_match(str(process), target_sha)
                if ok:
                    return str(process) if require_process else str(checkout)
            except Exception:
                pass  # daemon still restarting — keep polling until the deadline
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"verify failed: daemon did not report {target_sha} within "
                    f"{int(self._verify_timeout_s)}s")
            await asyncio.sleep(self._verify_interval_s)

    # ---- the lifecycle -----------------------------------------------------------
    async def deploy(self, *, target_sha: str, daemon_changed: bool, deps_changed: bool,
                     local_daemon_enabled: bool) -> HostProgress:
        t = self.target
        if not t.enabled:
            self._mark("disabled")
            return self.progress
        if not is_deployable(t):
            self._mark("offline")
            return self.progress
        local = is_local(t, self.local_name)
        if local and not local_daemon_enabled:
            self._mark("disabled")
            return self.progress
        repo_path: str | None = None
        if not local:
            if not t.ssh_host or not t.repo_path:
                self._mark("unconfigured",
                           error=f"host {t.name!r} has no ssh_host/repo_path — cannot update it")
                return self.progress
            if not SSH_HOST_RE.match(t.ssh_host):
                self._mark("failed", error=f"ssh_host {t.ssh_host!r} is not a valid [user@]host")
                return self.progress
            repo_path = t.repo_path
        try:
            self._mark("running", step="snapshot")
            try:
                await self._rpc("snapshot_deploy")
            except Exception:
                log.warning("runner %s: session snapshot failed (continuing)", t.name, exc_info=True)

            if not local:
                assert repo_path is not None
                for step, remote in remote_update_commands(repo_path, target_sha,
                                                           deps_changed=deps_changed):
                    self._mark(step=step)
                    await self._ssh(step, remote)
                    if step == "merge":
                        self.progress.moved = True

            if daemon_changed:
                await self._drain_then()
                self._mark(step="restart")
                if local:
                    await self._local("restart", LOCAL_DAEMON_RESTART)
                else:
                    await self._ssh("restart", REMOTE_RESTART)

            sha = await self._verify(target_sha, require_process=daemon_changed)
            self.progress.sha = sha

            if daemon_changed:
                self._mark(step="restore")
                try:
                    self.progress.restore = await self._rpc("restore_deploy")
                except Exception:
                    log.warning("runner %s: restore_deploy failed", t.name, exc_info=True)
                    self.progress.restore = {"restored": [], "failed": [], "error": "restore call failed"}
            self._mark("done")
            log.info("deploy.runner.done host=%s sha=%s restarted=%s", t.name, sha, daemon_changed)
        except Exception as exc:  # noqa: BLE001 — a runner failure is a REPORTED state
            self._mark("failed", step=self.progress.step, error=str(exc)[:500])
            log.warning("deploy.runner.failed host=%s step=%s: %s", t.name, self.progress.step, exc)
        return self.progress

    async def rewind(self, sha: str, *, daemon_changed: bool) -> None:
        """Rollback: put a moved remote runner back on *sha*. Never raises.

        The CHECKOUT always moves back — the deploy advanced it on every runner, whatever
        the diff touched. The DAEMON is only drained and restarted when this deploy
        restarted it: an edge-only deploy left the process on its prior commit (which is
        *sha*), so restarting here would kill live turns to reach a state already true.
        The drain is why the restart is conditional and not merely wasteful — a bare
        restart takes in-flight turns down with it.
        """
        t = self.target
        if is_local(t, self.local_name) or not t.ssh_host or not t.repo_path:
            return
        try:
            for step, remote in remote_rewind_commands(t.repo_path, sha):
                self._mark("running", step=f"rewind:{step}")
                await self._ssh(step, remote)
            if daemon_changed:
                await self._drain_then()
                self._mark(step="rewind:restart")
                await self._ssh("restart", REMOTE_RESTART)
            await self._verify(sha, require_process=daemon_changed)
            self._mark("done", sha=sha)
        except Exception as exc:  # noqa: BLE001
            self._mark("failed", error=f"rewind failed: {exc}"[:500])
            log.error("deploy.runner.rewind_failed host=%s: %s", t.name, exc)


async def deploy_runners(targets: list[RunnerTarget], *, target_sha: str, daemon_changed: bool,
                         deps_changed: bool, local_name: str, local_daemon_enabled: bool,
                         concurrency: int = DEFAULT_CONCURRENCY,
                         on_progress: Progress | None = None,
                         **runner_kwargs: Any) -> dict[str, HostProgress]:
    """Deploy every target, at most *concurrency* at a time. Returns name → progress."""
    sem = asyncio.Semaphore(max(1, concurrency))
    deploys = {t.name: RunnerDeploy(t, local_name=local_name, on_progress=on_progress,
                                    **runner_kwargs) for t in targets}

    async def one(rd: RunnerDeploy) -> None:
        async with sem:
            await rd.deploy(target_sha=target_sha, daemon_changed=daemon_changed,
                            deps_changed=deps_changed, local_daemon_enabled=local_daemon_enabled)

    await asyncio.gather(*(one(rd) for rd in deploys.values()), return_exceptions=True)
    return {name: rd.progress for name, rd in deploys.items()}


async def rewind_runners(targets: list[RunnerTarget], results: dict[str, HostProgress], *,
                         sha: str, local_name: str, daemon_changed: bool,
                         **runner_kwargs: Any) -> None:
    """Rollback helper: rewind every runner whose checkout this deploy moved.

    *daemon_changed* is the deploy's own classification, threaded through so the rewind
    restarts exactly the runners the deploy restarted — no more.
    """
    moved = [t for t in targets if results.get(t.name) is not None and results[t.name].moved]
    await asyncio.gather(
        *(RunnerDeploy(t, local_name=local_name, **runner_kwargs).rewind(
            sha, daemon_changed=daemon_changed) for t in moved),
        return_exceptions=True)


async def load_runner_targets() -> list[RunnerTarget]:
    """Every fleet host row (enabled or not — the lifecycle reports `disabled` itself)."""
    from sqlalchemy import select as sa_select
    from server.db import db_session
    from server.db.models import Host
    async with db_session() as db:
        rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
    return [RunnerTarget(name=r.name, endpoint=r.endpoint, enabled=bool(r.enabled),
                         healthy=r.healthy, ssh_host=r.ssh_host, repo_path=r.repo_path,
                         last_error=r.last_error)
            for r in rows]

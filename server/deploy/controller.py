from __future__ import annotations
import asyncio
import logging
import subprocess
from typing import Any, Callable

from server.deploy import git as _git
from server.deploy import steps as _steps
from server.deploy import drain as _drain
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState
from server.obs.context import log_event, new_request_id

log = logging.getLogger("server.deploy.controller")


def default_daemon_factory(daemon_enabled: bool) -> "Callable[[], Any] | None":
    """Return a factory that constructs a DaemonClient, or None if daemon is disabled."""
    if not daemon_enabled:
        return None
    from server.ipc.client import DaemonClient
    from server.ipc.paths import sessiond_socket
    return lambda: DaemonClient(sessiond_socket())


async def _aclose_quietly(client: Any) -> None:
    """Release a per-deploy ``DaemonClient``.

    ``default_daemon_factory`` builds a NEW client per call site, and each one owns two
    ``httpx.AsyncClient``s, two transports and — on the TLS branch — two certifi-loaded SSL
    contexts. Left unclosed they survive until GC, holding their sockets: exactly the
    "short-lived caller leaks the reserved health socket" shape ``DaemonClient.aclose``
    exists to prevent.

    Tolerant by design. The factory is an injection point and test doubles are plain
    objects with only ``call``; a deploy must never fail because teardown found no
    ``aclose``. Best-effort, logged at debug.
    """
    closer = getattr(client, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 — teardown must never fail a deploy
        log.debug("deploy: daemon client teardown failed", exc_info=True)


def _default_changed_files(repo: str, a: str, b: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "--name-only", a, b],
                           check=True, capture_output=True, text=True)
        return [p for p in r.stdout.splitlines() if p.strip()]
    except subprocess.CalledProcessError:
        return None   # uncomputable → classify_change fails safe to daemon_changed


class DeployController:
    def __init__(self, repo: str, *, health_url: str, venv_pip: str,
                 daemon_client_factory: Callable[[], Any] | None = None,
                 daemon_enabled: bool = False,
                 steps_mod=_steps, git_mod=_git, drain_mod=_drain,
                 changed_files_fn: Callable[[str, str, str], list[str] | None] = _default_changed_files):
        self.repo = repo
        self.health_url = health_url
        self.venv_pip = venv_pip
        self._client_factory = daemon_client_factory
        self.daemon_enabled = daemon_enabled
        self._steps = steps_mod
        self._git = git_mod
        self._drain = drain_mod
        self._changed_files = changed_files_fn
        self._lock_acquired = False
        self.status = DeployStatus(target_sha="")

    def _set(self, phase: Phase, state: PhaseState) -> None:
        self.status.phases[phase.value] = state.value

    async def deploy_once(self) -> DeployStatus:
        if self._lock_acquired:
            raise RuntimeError("deploy already in progress")
        self._lock_acquired = True
        self.status = DeployStatus(target_sha="", running=True)
        self._deploy_id = new_request_id()
        local = ""
        daemon_changed = False
        try:
            # --- preflight ---
            # Every synchronous step below runs via asyncio.to_thread so the controller's
            # event loop stays free to serve /status and /healthz. Inline, a long step
            # (npm build, wait_healthy's 30s poll) freezes the loop; the edge's 10s client
            # then times out and synthesises a spurious "controller unavailable" mid-deploy.
            self._set(Phase.PREFLIGHT, PhaseState.RUNNING)
            await asyncio.to_thread(self._git.fetch, self.repo)
            local = await asyncio.to_thread(self._git.current_sha, self.repo)
            remote = await asyncio.to_thread(self._git.remote_sha, self.repo)
            self.status.target_sha = remote
            if local == remote:
                # No new commits, but the built web/dist can still be stale relative to the
                # checkout: a manual restart can advance HEAD outside this controller and never
                # rebuild the frontend, leaving served assets baked with an older BUILD_SHA than
                # the running backend -> a permanent "new version available" banner that survives
                # reload. Rebuild on every deploy so served assets stay in lockstep with the
                # running sha. Best-effort: a build failure here must not fail a no-op deploy.
                try:
                    await asyncio.to_thread(self._steps.build_frontend, self.repo)
                except DeployError as e:
                    self.status.warnings.append(f"frontend rebuild failed: {e}")
                for p in Phase:
                    self._set(p, PhaseState.DONE)
                return self.status
            verdict = await asyncio.to_thread(self._steps.ci_verdict, remote)
            if verdict != "green":
                raise DeployError("preflight", f"CI verdict={verdict}")
            self._set(Phase.PREFLIGHT, PhaseState.DONE)

            # --- save (snapshot live sessions; best-effort) ---
            self._set(Phase.SAVE, PhaseState.RUNNING)
            if self._client_factory is not None:
                client = self._client_factory()
                try:
                    await client.call("snapshot_deploy")
                except Exception:
                    self.status.warnings.append("session snapshot failed (continuing)")
                finally:
                    await _aclose_quietly(client)
            self._set(Phase.SAVE, PhaseState.DONE)

            # --- update ---
            self._set(Phase.UPDATE, PhaseState.RUNNING)
            await asyncio.to_thread(self._steps.guard_working_tree, self.repo)
            await asyncio.to_thread(self._steps.fast_forward, self.repo)
            await asyncio.to_thread(self._steps.sync_deps_if_changed, self.repo, local, remote,
                                    venv_pip=self.venv_pip)
            await asyncio.to_thread(self._steps.build_frontend, self.repo)
            self._set(Phase.UPDATE, PhaseState.DONE)

            # --- report ---
            self._set(Phase.REPORT, PhaseState.RUNNING)
            changed = await asyncio.to_thread(self._changed_files, self.repo, local, remote)
            daemon_changed = self._steps.classify_change(changed) == "daemon_changed"
            # The controller cannot restart itself inline — it is the process running
            # this deploy — so note it now and schedule a deferred restart at the end.
            controller_changed = self._steps.deploy_code_changed(changed)
            self._set(Phase.REPORT, PhaseState.DONE)

            # --- restart ---
            self._set(Phase.RESTART, PhaseState.RUNNING)
            if daemon_changed and self._client_factory is not None:
                client = self._client_factory()
                log_event(log, logging.INFO, "deploy.drain.start", deploy_id=self._deploy_id)
                try:
                    result = await self._drain.drain_and_wait(client)
                finally:
                    await _aclose_quietly(client)
                if result != "ready":
                    self.status.warnings.append(f"drain {result} — restarting anyway")
            if daemon_changed and self.daemon_enabled:
                log_event(log, logging.INFO, "deploy.daemon.stop", deploy_id=self._deploy_id)
            # Resolve the target sha BEFORE restarting: a remote daemon has its own
            # checkout and restart_units fast-forwards it to exactly this commit, so
            # both hosts land on the same code instead of the edge racing ahead.
            short = await asyncio.to_thread(self._git.short_sha, self.repo, remote)
            try:
                await asyncio.to_thread(self._steps.restart_units,
                                        daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled,
                                        target_sha=short)
            except Exception as e:
                raise DeployError("restart", str(e)) from e
            if daemon_changed and self.daemon_enabled:
                log_event(log, logging.INFO, "deploy.daemon.start", deploy_id=self._deploy_id)
            # Give the gate a longer window when a daemon was restarted: the
            # remote restart returns before uvicorn is serving, so the edge can
            # legitimately report daemon:"unavailable" for tens of seconds.
            _attempts = (self._steps.HEALTH_ATTEMPTS_WITH_DAEMON if daemon_changed
                         else self._steps.HEALTH_ATTEMPTS_EDGE_ONLY)
            if not await asyncio.to_thread(self._steps.wait_healthy, short,
                                           url=self.health_url, attempts=_attempts):
                raise DeployError("restart", "health gate failed")

            # /healthz proves the process is up and the schema matches; it never
            # touches the ledger or the session store. A corrupt ledger left
            # /api/accounts 500ing while /healthz stayed green (2026-08-22), so
            # probe the functional endpoints too before calling this a success.
            base = self.health_url.rsplit("/healthz", 1)[0] or self.health_url
            await asyncio.to_thread(self._steps.require_endpoints_healthy, base)
            log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id=self._deploy_id)

            # This deploy changed the controller's own code. Hand a restart to
            # systemd so it lands AFTER this deploy reports, rather than killing it.
            # Without this the controller keeps executing the code it started with:
            # on 2026-08-21 that left a stale remote daemon because #719's fix was on
            # disk but not in the running process.
            if controller_changed:
                scheduled = await asyncio.to_thread(
                    self._steps.schedule_controller_restart)
                if scheduled:
                    log_event(log, logging.INFO, "deploy.controller.refresh_scheduled",
                              deploy_id=self._deploy_id)
                else:
                    self.status.warnings.append(
                        "deploy code changed but the controller restart could not be "
                        "scheduled — it is still running the previous code")
            self._set(Phase.RESTART, PhaseState.DONE)

            # --- resume (eager re-launch; only when the daemon actually restarted) ---
            if daemon_changed and self._client_factory is not None:
                self._set(Phase.RESUME, PhaseState.RUNNING)
                client = self._client_factory()
                try:
                    self.status.restore = await client.call("restore_deploy")
                    failed = self.status.restore.get("failed", [])
                    recovered = len(self.status.restore.get("restored", []))
                    lost = len(failed)
                    log_event(log, logging.INFO, "deploy.sessions.reattach",
                              deploy_id=self._deploy_id, recovered=recovered, lost=lost)
                    if failed:
                        self.status.warnings.append(f"resume failed for {len(failed)} session(s): {', '.join(failed)}")
                except Exception:
                    self.status.warnings.append("session restore call failed")
                finally:
                    await _aclose_quietly(client)
                self._set(Phase.RESUME, PhaseState.DONE)
            else:
                # Edge-only deploy: the daemon never restarted, so no sessions were dropped
                # and there is nothing to restore. The phase still COMPLETED (trivially) —
                # record it DONE. If left unset, the frontend's allPhasesDone() gate reads the
                # missing phase as 'pending' and reports a spurious deploy *failure* even though
                # nothing failed and deploy.error is None.
                self._set(Phase.RESUME, PhaseState.DONE)

            # --- publish (best-effort) ---
            publish_err = await asyncio.to_thread(self._steps.publish_deployment, remote)
            if publish_err:
                self.status.warnings.append(f"could not publish GitHub deployment: {publish_err}")
            return self.status
        except DeployError as e:
            self._set(Phase[e.phase.upper()], PhaseState.FAILED)
            self.status.error = str(e)
            # Roll back only if we already fast-forwarded (preflight failures changed nothing).
            if e.phase != "preflight" and local:
                await asyncio.to_thread(self._steps.rollback_to, self.repo, local,
                                        daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled)
            return self.status
        finally:
            self.status.running = False
            self._lock_acquired = False

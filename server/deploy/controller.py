from __future__ import annotations
import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Callable

from server.deploy import git as _git
from server.deploy import steps as _steps
from server.deploy import drain as _drain
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState
from server.obs.context import log_event, new_request_id

log = logging.getLogger("server.deploy.controller")


def default_daemon_factory(daemon_enabled: bool) -> "Callable[[], Any] | None":
    """Return a factory that constructs a DaemonClient, or None if daemon is disabled.

    Resolves the daemon the same way the edge does (``sessiond_endpoint``: the
    ``CCSWITCH_SESSIOND_ENDPOINT`` URL, else the legacy socket path). It used to take the
    legacy UDS path unconditionally, so on a host whose daemon listens only on https
    (dobby, 2026-08-30: no ``sessiond.sock`` exists) every pre-deploy RPC failed —
    "session snapshot failed (continuing)" on each upgrade, and on a daemon-changing
    deploy the drain could not run either: the daemon was restarted "anyway", mid-turn.
    """
    if not daemon_enabled:
        return None
    from server.ipc.client import DaemonClient
    from server.ipc.paths import sessiond_endpoint
    return lambda: DaemonClient(sessiond_endpoint())


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
    def __init__(self, repo: str, *, health_url: str, venv_python: str,
                 daemon_client_factory: Callable[[], Any] | None = None,
                 daemon_enabled: bool = False,
                 steps_mod=_steps, git_mod=_git, drain_mod=_drain,
                 changed_files_fn: Callable[[str, str, str], list[str] | None] = _default_changed_files,
                 status_path: str | None = None):
        self.repo = repo
        self.health_url = health_url
        self.venv_python = venv_python
        self._client_factory = daemon_client_factory
        self.daemon_enabled = daemon_enabled
        self._steps = steps_mod
        self._git = git_mod
        self._drain = drain_mod
        self._changed_files = changed_files_fn
        self._lock_acquired = False
        self._status_path = status_path
        # A deploy that changes deploy code schedules a restart of THIS process, so a
        # fresh controller may be born seconds after a deploy finished. Starting from an
        # empty DeployStatus made /status report that completed upgrade as "did not
        # complete" (#865) — load the persisted terminal state instead.
        self.status = self._load_status() or DeployStatus(target_sha="")

    def _load_status(self) -> DeployStatus | None:
        if not self._status_path:
            return None
        try:
            with open(self._status_path, encoding="utf-8") as f:
                st = DeployStatus.from_dict(json.load(f))
        except (OSError, ValueError):
            return None
        if st.running:
            # The previous process died mid-deploy. This process is not running any
            # deploy, so serving running=True would freeze the UI spinner and 409-block
            # /start forever. Report the interruption as a terminal failure instead.
            st.running = False
            for phase, state in st.phases.items():
                if state == PhaseState.RUNNING.value:
                    st.phases[phase] = PhaseState.FAILED.value
            if st.error is None:
                st.error = "deploy interrupted: the controller process was restarted mid-deploy"
        return st

    def _persist_status(self) -> None:
        """Best-effort atomic write of the current status — bookkeeping must never fail
        (or slow) a deploy, so any OSError is swallowed after a debug log."""
        if not self._status_path:
            return
        try:
            tmp = f"{self._status_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.status.to_dict(), f)
            os.replace(tmp, self._status_path)
        except OSError:
            log.debug("deploy: could not persist status to %s", self._status_path,
                      exc_info=True)

    def _set(self, phase: Phase, state: PhaseState) -> None:
        self.status.phases[phase.value] = state.value
        self._persist_status()

    async def deploy_once(self) -> DeployStatus:
        if self._lock_acquired:
            raise RuntimeError("deploy already in progress")
        self._lock_acquired = True
        self.status = DeployStatus(target_sha="", running=True)
        self._deploy_id = new_request_id()
        local = ""
        daemon_changed = False
        controller_changed = False
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
                                    venv_python=self.venv_python)
            await asyncio.to_thread(self._steps.build_frontend, self.repo)
            self._set(Phase.UPDATE, PhaseState.DONE)

            # --- report ---
            self._set(Phase.REPORT, PhaseState.RUNNING)
            changed = await asyncio.to_thread(self._changed_files, self.repo, local, remote)
            daemon_changed = self._steps.classify_change(changed) == "daemon_changed"
            # The controller cannot restart itself inline — it is the process running
            # this deploy — so note it now; the deferred restart is scheduled in the
            # finally, once the status is terminal.
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
            # Drop the edge's /events WebSocket before stopping the daemon. The long-lived
            # connection holds uvicorn's graceful shutdown open for ~45s; closing it here
            # lets the old daemon exit on SIGTERM instead of waiting for the edge to
            # reconnect. Best-effort: a failure here must not abort the deploy (issue #740).
            if daemon_changed and self._client_factory is not None:
                _ec_client = self._client_factory()
                try:
                    await _ec_client.call("close_events_connections")
                except Exception:
                    log.debug("deploy: close_events_connections RPC failed (best-effort)",
                              exc_info=True)
                finally:
                    await _aclose_quietly(_ec_client)
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
            # Give the gate the longer window whenever a REMOTE daemon is in play — not just
            # when it was restarted. A remote restart returns before uvicorn is serving, so the
            # edge reports daemon:"unavailable" for tens of seconds; but even an EDGE-ONLY
            # deploy must re-attach the restarted edge to the remote daemon over a settling
            # tailnet, during which /healthz blocks and reports the daemon unavailable just the
            # same (the 2026-08-26 #780 rollback of a frontend-only deploy). Size the window by
            # remote-daemon PRESENCE, not code-change; require_daemon_sha below still keys off
            # daemon_changed, since an edge-only deploy leaves the daemon on its prior commit.
            _needs_daemon_window = daemon_changed or self._steps.remote_daemon_configured()
            _attempts = (self._steps.HEALTH_ATTEMPTS_WITH_DAEMON if _needs_daemon_window
                         else self._steps.HEALTH_ATTEMPTS_EDGE_ONLY)
            # Only assert the daemon's sha when THIS deploy advanced it. An edge-only
            # deploy (daemon_changed=False) never touches the remote daemon, so it
            # stays on its prior commit by design; requiring the target sha there
            # rolls back every edge-only deploy once the daemon drifts behind the
            # edge (the 2026-08-26 #776 rollback). Availability is still required.
            if not await asyncio.to_thread(self._steps.wait_healthy, short,
                                           url=self.health_url, attempts=_attempts,
                                           require_daemon_sha=daemon_changed):
                raise DeployError("restart", "health gate failed")

            # /healthz proves the process is up and the schema matches; it never
            # touches the ledger or the session store. A corrupt ledger left
            # /api/accounts 500ing while /healthz stayed green (2026-08-22), so
            # probe the functional endpoints too before calling this a success.
            base = self.health_url.rsplit("/healthz", 1)[0] or self.health_url
            await asyncio.to_thread(self._steps.require_endpoints_healthy, base)
            log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id=self._deploy_id)
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
            # Signal failure immediately so the UI can leave the spinner state; rollback
            # can take ~60s (it restarts units), and keeping running=True that long keeps
            # the overlay frozen on "Restarting service" with no error shown (issue #741).
            self.status.running = False
            # Roll back only if we already fast-forwarded (preflight failures changed nothing).
            if e.phase != "preflight" and local:
                await asyncio.to_thread(self._steps.rollback_to, self.repo, local,
                                        daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled)
            return self.status
        except Exception as e:  # noqa: BLE001 — a deploy must always end in a reported state
            # Anything that is not a DeployError (an OSError from a missing launcher, a
            # git wrapper raising, ...) used to escape here unhandled: the task died with
            # "Task exception was never retrieved", `error` stayed None, the running
            # phase stayed "running", and — because the fast-forward had already moved
            # the checkout — no rollback ran, leaving the checkout ahead of the running
            # process with the UI reporting nothing to deploy (2026-08-30 23:19 EDT).
            failed = next((p for p, st in self.status.phases.items()
                           if st == PhaseState.RUNNING.value), None)
            log.error("deploy: unexpected %s in phase %s", type(e).__name__, failed,
                      exc_info=True)
            if failed is not None:
                self._set(Phase(failed), PhaseState.FAILED)
            self.status.error = f"{failed or 'deploy'}: unexpected {type(e).__name__}: {e}"
            self.status.running = False
            if failed not in (None, Phase.PREFLIGHT.value) and local:
                await asyncio.to_thread(self._steps.rollback_to, self.repo, local,
                                        daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled)
            return self.status
        finally:
            self.status.running = False
            # This deploy changed the controller's own code: hand a restart to systemd.
            # Without it the controller keeps executing the code it started with (on
            # 2026-08-21 that left a stale remote daemon: #719's fix was on disk but not
            # in the running process). Scheduled HERE — after the status is terminal —
            # because the +10s timer used to be armed in the RESTART phase, where
            # resume/publish routinely outlast it: the process was replaced mid-deploy
            # and a completed upgrade was reported as "did not complete" (#865). A
            # failed deploy schedules nothing: the rollback already restored the code
            # this process is running.
            if controller_changed and self.status.error is None:
                scheduled = await asyncio.to_thread(self._steps.schedule_controller_restart)
                if scheduled:
                    log_event(log, logging.INFO, "deploy.controller.refresh_scheduled",
                              deploy_id=self._deploy_id)
                else:
                    self.status.warnings.append(
                        "deploy code changed but the controller restart could not be "
                        "scheduled — it is still running the previous code")
            self._persist_status()
            self._lock_acquired = False

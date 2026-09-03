from __future__ import annotations
import asyncio
import json
import logging
import os
import subprocess
from typing import Callable

from server.deploy import git as _git
from server.deploy import runners as _runners
from server.deploy import state as _state
from server.deploy import steps as _steps
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState
from server.hostname import self_host_name
from server.obs.context import log_event, new_request_id

log = logging.getLogger("server.deploy.controller")


def _default_changed_files(repo: str, a: str, b: str) -> list[str] | None:
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "--name-only", a, b],
                           check=True, capture_output=True, text=True)
        return [p for p in r.stdout.splitlines() if p.strip()]
    except subprocess.CalledProcessError:
        return None   # uncomputable → classify_change fails safe to daemon_changed


class DeployController:
    def __init__(self, repo: str, *, health_url: str, venv_python: str,
                 daemon_enabled: bool = False,
                 steps_mod=_steps, git_mod=_git, runners_mod=_runners, state_mod=_state,
                 local_name: str | None = None,
                 changed_files_fn: Callable[[str, str, str], list[str] | None] = _default_changed_files,
                 status_path: str | None = None):
        self.repo = repo
        self.health_url = health_url
        self.venv_python = venv_python
        self.daemon_enabled = daemon_enabled
        self._steps = steps_mod
        self._git = git_mod
        self._runners = runners_mod
        self._state = state_mod
        # Which fleet host IS this process — the runner whose checkout is `repo` itself
        # and whose unit restarts without ssh.
        self._local_name = local_name or self_host_name()
        self._changed_files = changed_files_fn
        self._lock_acquired = False
        self._status_path = status_path
        # Set by the runners phase; read by _rollback to rewind the checkouts it moved.
        self._runner_targets: list | None = None
        self._runner_results: dict | None = None
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

    def _on_host_progress(self, name: str, progress) -> None:
        """Stream one runner's progress onto /status as it happens — the fan-out runs
        several hosts at once and can take minutes, so waiting for the whole phase to
        finish would leave the UI blind to which host is where."""
        self.status.hosts[name] = progress.to_wire()
        self._persist_status()

    async def _rollback(self, local: str, *, daemon_changed: bool) -> None:
        """Undo this deploy: the edge + local daemon via steps, then every REMOTE runner
        whose checkout the runners phase already advanced."""
        await asyncio.to_thread(self._steps.rollback_to, self.repo, local,
                                daemon_changed=daemon_changed,
                                daemon_enabled=self.daemon_enabled)
        results = self._runner_results
        targets = self._runner_targets
        if results and targets:
            await self._runners.rewind_runners(targets, results, sha=local,
                                               local_name=self._local_name,
                                               daemon_changed=daemon_changed)

    async def deploy_once(self, target_sha: str | None = None) -> DeployStatus:
        """Run one deploy. Without *target_sha* the deploy targets origin/master's tip,
        exactly as always. With *target_sha* — the green-frontier path, used when the tip
        is red but an older pending commit is green — preflight additionally validates
        that the sha lies inside ``live..origin/master`` and is itself CI-green, and then
        the WHOLE deploy (fast-forward, dep-sync, classification, restarts, publish)
        targets that sha instead of the tip."""
        if self._lock_acquired:
            raise RuntimeError("deploy already in progress")
        self._lock_acquired = True
        self.status = DeployStatus(target_sha="", running=True)
        self._deploy_id = new_request_id()
        self._runner_targets = None
        self._runner_results = None
        local = ""
        daemon_changed = False
        controller_changed = False
        # The new code is live on THIS host: set once the edge has passed the health gate
        # and the functional probes, i.e. once a rollback is no longer on the table.
        edge_landed = False
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
            if target_sha is not None:
                # The sha lands in git argv (merge --ff-only <sha>): refuse anything that
                # is not plain hex before running ANY git command with it.
                if not _steps._SHA_RE.match(target_sha):
                    raise DeployError("preflight",
                                      f"targetSha {target_sha!r} is not a hex git sha")
                # The target must sit inside live..origin/master: an ancestor of the tip
                # (it has been merged) that the current checkout can fast-forward to.
                if not await asyncio.to_thread(self._git.is_ancestor, self.repo,
                                               target_sha, remote):
                    raise DeployError("preflight",
                                      f"targetSha {target_sha} is not an ancestor of "
                                      "origin/master")
                if not await asyncio.to_thread(self._git.is_ancestor, self.repo,
                                               local, target_sha):
                    raise DeployError("preflight",
                                      f"targetSha {target_sha} is not reachable from the "
                                      f"deployed commit {local} (cannot fast-forward)")
                remote = target_sha
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

            # --- save ---
            # Session snapshots are per-runner now: each daemon in the fleet snapshots its
            # OWN sessions at the top of its lifecycle, inside the runners phase. There is
            # no fleet-wide snapshot left for the controller to take, but the phase is kept
            # so the wire shape (and the UI's phase list) does not change.
            self._set(Phase.SAVE, PhaseState.DONE)

            # --- update ---
            self._set(Phase.UPDATE, PhaseState.RUNNING)
            await asyncio.to_thread(self._steps.guard_working_tree, self.repo)
            if target_sha is None:
                await asyncio.to_thread(self._steps.fast_forward, self.repo)
            else:
                # remote == target_sha here: fast-forward to exactly that commit,
                # stopping short of the (red) origin/master tip.
                await asyncio.to_thread(self._steps.fast_forward, self.repo, remote)
            await asyncio.to_thread(self._steps.sync_deps_if_changed, self.repo, local, remote,
                                    venv_python=self.venv_python)
            await asyncio.to_thread(self._steps.build_frontend, self.repo)
            self._set(Phase.UPDATE, PhaseState.DONE)

            # --- report ---
            self._set(Phase.REPORT, PhaseState.RUNNING)
            changed = await asyncio.to_thread(self._changed_files, self.repo, local, remote)
            daemon_changed = self._steps.classify_change(changed) == "daemon_changed"
            # Whether each runner must reinstall its venv. Fails safe on an uncomputable
            # diff (changed is None), exactly as classify_change does.
            deps_changed = (changed is None) or any(
                f in ("pyproject.toml", "uv.lock") for f in changed)
            # The controller cannot restart itself inline — it is the process running
            # this deploy — so note it now; the deferred restart is scheduled in the
            # finally, once the status is terminal.
            controller_changed = self._steps.deploy_code_changed(changed)
            self._set(Phase.REPORT, PhaseState.DONE)

            # --- runners (every fleet daemon, local + remote; spec §2) ---
            # Each runner snapshots, updates its own checkout, drains+restarts (only when
            # the diff touched daemon code), verifies /info and restores its sessions.
            # It runs BEFORE the edge restarts: daemon-before-edge is load-bearing (#190).
            self._set(Phase.RUNNERS, PhaseState.RUNNING)
            # The edge's health gate and the deploy_state row speak the SHORT sha; the
            # runners merge/reset their checkouts to the FULL one (RunnerDeploy verifies
            # /info by prefix, so a short /info sha still matches).
            short = await asyncio.to_thread(self._git.short_sha, self.repo, remote)
            targets = await self._runners.load_runner_targets()
            if not targets:
                # An empty fan-out is indistinguishable from a successful one in the phase
                # state, and the usual cause is an unset CCSWITCH_DB_URL in a hand-run
                # `python -m server.deploy --once` (docs/runbooks/rocky-session-host.md §5):
                # the registry lookup lands on an empty default DB and every runner is
                # silently left on the old code while the deploy reports green.
                self.status.warnings.append(
                    "runners: no enabled fleet hosts found — nothing was fanned out "
                    "(is CCSWITCH_DB_URL pointing at the fleet database?)")
            results = await self._runners.deploy_runners(
                targets, target_sha=remote, daemon_changed=daemon_changed,
                deps_changed=deps_changed, local_name=self._local_name,
                local_daemon_enabled=self.daemon_enabled,
                on_progress=self._on_host_progress)
            self._runner_targets, self._runner_results = targets, results
            for name, p in results.items():
                self.status.hosts[name] = p.to_wire()
            runner_failures = [f"{n} failed at {p.step or '?'}: {p.error or p.state}"
                               for n, p in results.items() if p.state in self._runners.FAILED_STATES]
            self._set(Phase.RUNNERS, PhaseState.FAILED if runner_failures else PhaseState.DONE)

            # --- restart (the edge) ---
            self._set(Phase.RESTART, PhaseState.RUNNING)
            try:
                await asyncio.to_thread(self._steps.restart_edge)
            except Exception as e:
                raise DeployError("restart", str(e)) from e
            # Give the gate the longer window whenever a REMOTE runner is in play — not just
            # when it was restarted. A remote restart returns before uvicorn is serving, so the
            # edge reports daemon:"unavailable" for tens of seconds; but even an EDGE-ONLY
            # deploy must re-attach the restarted edge to the remote daemon over a settling
            # tailnet, during which /healthz blocks and reports the daemon unavailable just the
            # same (the 2026-08-26 #780 rollback of a frontend-only deploy). Size the window by
            # remote-runner PRESENCE, not code-change; require_daemon_sha below still keys off
            # daemon_changed, since an edge-only deploy leaves the daemon on its prior commit.
            _needs_daemon_window = daemon_changed or self._steps.remote_runners_configured(targets)
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
            # Past this line the new code IS what this process will run after a restart:
            # nothing below rolls the checkout back. The controller's self-refresh keys off
            # this, not off status.error — a failed RUNNER sets error while the edge stands.
            edge_landed = True
            log_event(log, logging.INFO, "deploy.edge.cutover", deploy_id=self._deploy_id)
            # The fleet's authoritative version moved: record it now (spec §1) so the skew
            # gate measures every runner — including one that failed above — against it.
            try:
                await self._state.record_deploy(sha=remote, short=short,
                                                daemon_changed=daemon_changed)
            except Exception:
                log.warning("deploy: could not record deploy_state", exc_info=True)
                self.status.warnings.append("deployed version not recorded (deploy_state)")
            self._set(Phase.RESTART, PhaseState.DONE)

            # --- resume: per-runner restores already ran inside the runners phase ---
            # An edge-only deploy restarted no daemon, so every runner's restore is empty —
            # the phase still COMPLETED and must be recorded DONE, or the frontend's
            # allPhasesDone() gate reads the missing phase as 'pending' and reports a
            # spurious deploy failure with deploy.error still None.
            self._set(Phase.RESUME, PhaseState.RUNNING)
            restored: list[str] = []
            failed_sessions: list[str] = []
            for name, p in results.items():
                r = p.restore or {}
                restored += list(r.get("restored", []))
                failed_sessions += list(r.get("failed", []))
            self.status.restore = {"restored": restored, "failed": failed_sessions}
            # The full outcome, not just counts: this status object is wiped seconds later
            # by the controller's own scheduled self-restart (issue #864).
            log_event(log, logging.INFO, "deploy.sessions.reattach",
                      deploy_id=self._deploy_id, recovered=len(restored),
                      lost=len(failed_sessions), restored=restored, failed=failed_sessions)
            if failed_sessions:
                self.status.warnings.append(
                    f"resume failed for {len(failed_sessions)} session(s): "
                    f"{', '.join(failed_sessions)}")
            self._set(Phase.RESUME, PhaseState.DONE)

            # --- publish (best-effort) ---
            publish_err = await asyncio.to_thread(self._steps.publish_deployment, remote)
            if publish_err:
                self.status.warnings.append(f"could not publish GitHub deployment: {publish_err}")
            if runner_failures:
                # Decision 1a: the edge landed and stays; report the deploy as failed with
                # the offending hosts named. Never a rollback — one lagging runner must not
                # drag the whole fleet back.
                self.status.error = "runners: " + "; ".join(runner_failures)
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
                await self._rollback(local, daemon_changed=daemon_changed)
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
                await self._rollback(local, daemon_changed=daemon_changed)
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
            # rolled-back (or preflight-failed) deploy schedules nothing: the rollback
            # already restored the code this process is running.
            #
            # The gate is `edge_landed`, NOT `error is None`. Once the fan-out could fail
            # the deploy on a lagging RUNNER while the edge kept the new code, keying off
            # `error` left the controller executing stale deploy code with the new code on
            # disk — precisely the #719/#865 hazard this restart exists to prevent.
            if controller_changed and edge_landed:
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

from __future__ import annotations
import logging
import subprocess
from typing import Any, Callable

from server.deploy import git as _git
from server.deploy import steps as _steps
from server.deploy import drain as _drain
from server.deploy.types import DeployError, DeployStatus, Phase, PhaseState

log = logging.getLogger("server.deploy.controller")


def default_daemon_factory(daemon_enabled: bool) -> "Callable[[], Any] | None":
    """Return a factory that constructs a DaemonClient, or None if daemon is disabled."""
    if not daemon_enabled:
        return None
    from server.ipc.client import DaemonClient
    from server.ipc.paths import sessiond_socket
    return lambda: DaemonClient(sessiond_socket())


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
        local = ""
        daemon_changed = False
        try:
            # --- preflight ---
            self._set(Phase.PREFLIGHT, PhaseState.RUNNING)
            self._git.fetch(self.repo)
            local = self._git.current_sha(self.repo)
            remote = self._git.remote_sha(self.repo)
            self.status.target_sha = remote
            if local == remote:
                for p in Phase:
                    self._set(p, PhaseState.DONE)
                return self.status
            verdict = self._steps.ci_verdict(remote)
            if verdict != "green":
                raise DeployError("preflight", f"CI verdict={verdict}")
            self._set(Phase.PREFLIGHT, PhaseState.DONE)

            # --- save (snapshot live sessions; best-effort) ---
            self._set(Phase.SAVE, PhaseState.RUNNING)
            if self._client_factory is not None:
                try:
                    await self._client_factory().call("snapshot_deploy")
                except Exception:
                    self.status.warnings.append("session snapshot failed (continuing)")
            self._set(Phase.SAVE, PhaseState.DONE)

            # --- update ---
            self._set(Phase.UPDATE, PhaseState.RUNNING)
            self._steps.guard_working_tree(self.repo)
            self._steps.fast_forward(self.repo)
            self._steps.sync_deps_if_changed(self.repo, local, remote, venv_pip=self.venv_pip)
            self._steps.build_frontend(self.repo)
            self._set(Phase.UPDATE, PhaseState.DONE)

            # --- report ---
            self._set(Phase.REPORT, PhaseState.RUNNING)
            changed = self._changed_files(self.repo, local, remote)
            daemon_changed = self._steps.classify_change(changed) == "daemon_changed"
            self._set(Phase.REPORT, PhaseState.DONE)

            # --- restart ---
            self._set(Phase.RESTART, PhaseState.RUNNING)
            if daemon_changed and self._client_factory is not None:
                client = self._client_factory()
                result = await self._drain.drain_and_wait(client)
                if result != "ready":
                    self.status.warnings.append(f"drain {result} — restarting anyway")
            try:
                self._steps.restart_units(daemon_changed=daemon_changed,
                                          daemon_enabled=self.daemon_enabled)
            except Exception as e:
                raise DeployError("restart", str(e)) from e
            short = self._git.short_sha(self.repo, remote)
            if not self._steps.wait_healthy(short, url=self.health_url):
                raise DeployError("restart", "health gate failed")
            self._set(Phase.RESTART, PhaseState.DONE)

            # --- resume (eager re-launch; only when the daemon actually restarted) ---
            if daemon_changed and self._client_factory is not None:
                self._set(Phase.RESUME, PhaseState.RUNNING)
                try:
                    self.status.restore = await self._client_factory().call("restore_deploy")
                    failed = self.status.restore.get("failed", [])
                    if failed:
                        self.status.warnings.append(f"resume failed for {len(failed)} session(s): {', '.join(failed)}")
                except Exception:
                    self.status.warnings.append("session restore call failed")
                self._set(Phase.RESUME, PhaseState.DONE)

            # --- publish (best-effort) ---
            if not self._steps.publish_deployment(remote):
                self.status.warnings.append("could not publish GitHub deployment")
            return self.status
        except DeployError as e:
            self._set(Phase[e.phase.upper()], PhaseState.FAILED)
            self.status.error = str(e)
            # Roll back only if we already fast-forwarded (preflight failures changed nothing).
            if e.phase != "preflight" and local:
                self._steps.rollback_to(self.repo, local, daemon_changed=daemon_changed,
                                        daemon_enabled=self.daemon_enabled)
            return self.status
        finally:
            self.status.running = False
            self._lock_acquired = False

"""Autopilot deploy-resume recovery: sessions that fail to re-launch after a
daemon-affecting deploy are retried by the daemon (not the user), and the
'failed to resume' banner clears the moment they come back — via auto-retry or
a manual open. See orchestrator._recover_resume_failures_once / resume_recovery_status."""
import inspect
from types import SimpleNamespace

import pytest

from server.sessions import deploy_snapshot as ds
from server.sessions.orchestrator import (
    RESUME_RECOVERY_MAX_ATTEMPTS,
    SessionOrchestrator,
)


class MiniOrch:
    """Binds the real recovery methods with a minimal, network-free surface.

    ``ensure_driver`` succeeds for ids in ``resumable``; ids in ``raising`` raise
    (a start failure) and every other id returns None (unknown) — both count as a
    failed resume, matching the real method's two failure shapes."""

    restore_deploy = SessionOrchestrator.restore_deploy
    _recover_resume_failures_once = SessionOrchestrator._recover_resume_failures_once
    _prune_recovered = SessionOrchestrator._prune_recovered
    resume_recovery_status = SessionOrchestrator.resume_recovery_status
    _owns_record = SessionOrchestrator._owns_record
    _owns_record_by_stamp = SessionOrchestrator._owns_record_by_stamp

    def __init__(self, *, drivers=None, records=None, resumable=None, raising=None,
                 enabled_hosts=None):
        self._drivers = drivers or {}
        self._records = records or {}
        self._resumable = set(resumable or ())
        self._raising = set(raising or ())
        self._resume_recovery: dict[str, int] = {}
        self._enabled_hosts = enabled_hosts
        self.ensured: list[str] = []

    async def _enabled_host_names(self):
        return self._enabled_hosts

    async def ensure_driver(self, session_id, identity="system"):
        self.ensured.append(session_id)
        if session_id in self._raising:
            raise RuntimeError("boom")
        if session_id in self._resumable:
            driver = object()
            self._drivers[session_id] = driver   # real ensure_driver registers it
            return driver
        return None


@pytest.mark.asyncio
async def test_restore_enrolls_only_failed_sessions(tmp_path):
    p = str(tmp_path / "live.json")
    ds.write_snapshot([{"id": "s1", "status": "working"}, {"id": "s2", "status": "idle"}], path=p)
    o = MiniOrch(resumable={"s1"})
    res = await o.restore_deploy(path=p)
    assert res == {"restored": ["s1"], "failed": ["s2"], "replayed": [], "skipped": []}
    # s2 (failed) is enrolled for recovery at attempt 0; s1 (restored) is not.
    assert o._resume_recovery == {"s2": 0}


@pytest.mark.asyncio
async def test_recovery_pass_drops_enrollment_now_owned_elsewhere(monkeypatch):
    """A row re-stamped to (or grabbed from) another enabled host after the snapshot
    is that host's to resume: retrying ensure_driver here would only re-take its
    lease out from under the owner (#1824). Drop the enrollment so the banner clears
    instead of counting attempts toward gave-up."""
    import server.sessions.orchestrator as mod
    monkeypatch.setattr(mod, "daemon_role", lambda: "primary")
    monkeypatch.setattr(mod, "self_host_name", lambda: "dobby")
    o = MiniOrch(records={"foreign": SimpleNamespace(id="foreign", host="rocky"),
                          "mine": SimpleNamespace(id="mine", host="dobby")},
                 resumable={"mine", "foreign"},
                 enabled_hosts=frozenset({"dobby", "rocky"}))
    o._resume_recovery = {"foreign": 1, "mine": 0}
    await o._recover_resume_failures_once()
    assert o.ensured == ["mine"]
    assert o._resume_recovery == {}
    assert o.resume_recovery_status() == {"failed": [], "gaveUp": []}


@pytest.mark.asyncio
async def test_recovery_pass_relaunches_transient_failure():
    o = MiniOrch(resumable={"s2"})
    o._resume_recovery = {"s2": 0}
    await o._recover_resume_failures_once()
    # Came back → dropped from the set; the banner will clear.
    assert o._resume_recovery == {}
    assert o.resume_recovery_status() == {"failed": [], "gaveUp": []}


@pytest.mark.asyncio
async def test_recovery_pass_gives_up_after_max_attempts():
    o = MiniOrch(raising={"s2"})           # never resumes
    o._resume_recovery = {"s2": 0}
    for _ in range(RESUME_RECOVERY_MAX_ATTEMPTS):
        await o._recover_resume_failures_once()
    assert o._resume_recovery["s2"] == RESUME_RECOVERY_MAX_ATTEMPTS
    attempts_made = len(o.ensured)
    # Capped: a further pass must NOT retry a gave-up session.
    await o._recover_resume_failures_once()
    assert len(o.ensured) == attempts_made
    # Still surfaced to the human, flagged as gave-up (not silently dropped).
    assert o.resume_recovery_status() == {"failed": [], "gaveUp": ["s2"]}


@pytest.mark.asyncio
async def test_manual_open_prunes_from_recovery_set():
    # A human opened s2 (a live driver now exists) before the pass ran.
    o = MiniOrch(drivers={"s2": object()})
    o._resume_recovery = {"s2": 1}
    o.resume_recovery_status()             # status read prunes now-live ids
    assert o._resume_recovery == {}
    # And the pass never wastes an ensure_driver on it.
    await o._recover_resume_failures_once()
    assert o.ensured == []


def test_resume_recovery_status_partitions_by_attempts():
    o = MiniOrch()
    o._resume_recovery = {"a": 0, "b": RESUME_RECOVERY_MAX_ATTEMPTS, "c": 1}
    st = o.resume_recovery_status()
    assert set(st["failed"]) == {"a", "c"}
    assert st["gaveUp"] == ["b"]


def test_recovery_runs_in_tick_loop_regardless_of_mode():
    # The recovery pass must live in the always-on tick LOOP, not inside the
    # mode-gated AutopilotLoop.tick() — otherwise it wouldn't run when autopilot
    # is off, and 'all failed sessions, always' would be violated.
    src = inspect.getsource(SessionOrchestrator._autopilot_tick_loop)
    assert "_recover_resume_failures_once" in src


def test_resume_recovery_status_registered_as_rpc():
    from server.ipc import orchestrator_rpc
    src = inspect.getsource(orchestrator_rpc.register_orchestrator_rpc)
    assert "resume_recovery_status" in src


def test_remote_orchestrator_proxies_resume_recovery_status():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "resume_recovery_status")

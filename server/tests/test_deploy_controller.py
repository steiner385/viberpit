import pytest
import threading
from types import SimpleNamespace
from server.deploy.controller import DeployController
from server.deploy.types import Phase, PhaseState


class FakeGit:
    def __init__(self, local, remote, changed):
        self._local, self._remote, self._changed = local, remote, changed
        self.reset_to = None
    def fetch(self, repo, ref="master", run=None): pass
    def current_sha(self, repo, run=None): return self._local
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def short_sha(self, repo, sha, run=None): return sha[:7]


def make_steps(*, ci="green", healthy=True, changed=None):
    calls = SimpleNamespace(restarted=[], rolled_back=False, published=False)
    mod = SimpleNamespace()
    mod.ci_verdict = lambda sha, gh_api=None: ci
    mod.guard_working_tree = lambda repo, run=None: False
    mod.fast_forward = lambda repo, ref="master", run=None: None
    mod.sync_deps_if_changed = lambda repo, a, b, *, venv_pip, run=None: []
    mod.build_frontend = lambda repo, run=None: None
    mod.classify_change = lambda files: "daemon_changed" if files and any("orchestrator" in f for f in files) else "edge_only"
    mod._changed_files = changed or []
    def restart(*, daemon_changed, daemon_enabled, target_sha=None, run=None):
        u = (["ccswitch-daemon.service"] if daemon_changed and daemon_enabled else []) + ["ccswitch.service"]
        calls.restarted = u; return u
    mod.restart_units = restart
    # The controller cannot restart itself inline (it runs the deploy), so it asks
    # steps to schedule a deferred refresh when deploy code changed.
    mod.deploy_code_changed = lambda changed: False
    def _sched(**k):
        calls.controller_refresh_scheduled = True
        return True
    mod.schedule_controller_restart = _sched
    mod.wait_healthy = lambda sha, **k: healthy
    # The controller selects a poll window by daemon_changed (a remote daemon
    # restart outlasts the edge-only window).
    mod.HEALTH_ATTEMPTS_EDGE_ONLY = 15
    mod.HEALTH_ATTEMPTS_WITH_DAEMON = 75
    # Functional probes run after the health gate; default to healthy in these tests.
    mod.require_endpoints_healthy = lambda base, paths=None, **k: None
    def rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None):
        calls.rolled_back = True
    mod.rollback_to = rollback
    def publish(sha, gh_api_json=None):
        calls.published = True; return None
    mod.publish_deployment = publish
    return mod, calls


@pytest.mark.asyncio
async def test_up_to_date_is_noop():
    g = FakeGit("same", "same", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.running is False and st.error is None


@pytest.mark.asyncio
async def test_up_to_date_still_rebuilds_frontend():
    """No new commits still rebuilds web/dist: a manual restart can advance the checkout
    outside this controller and leave the built frontend baked with an older BUILD_SHA than
    the running backend, showing a permanent 'new version available' banner. Rebuilding on
    every deploy keeps served assets in lockstep with the running sha."""
    g = FakeGit("same", "same", [])
    s, _ = make_steps()
    built: list[str] = []
    s.build_frontend = lambda repo, run=None: built.append(repo)
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.running is False and st.error is None
    assert built == ["/r"]


@pytest.mark.asyncio
async def test_up_to_date_rebuild_failure_is_warning_not_error():
    """A frontend rebuild failure on a no-op deploy must not fail the deploy; it degrades to
    a warning so the controller still reports success and stale dist is merely not healed."""
    g = FakeGit("same", "same", [])
    s, _ = make_steps()
    def boom(repo, run=None):
        from server.deploy.types import DeployError
        raise DeployError("update", "frontend build failed: npm exploded")
    s.build_frontend = boom
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.error is None
    assert any("frontend" in w for w in st.warnings)


@pytest.mark.asyncio
async def test_red_ci_refuses_before_any_change():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(ci="red")
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip", git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_health_failure_triggers_rollback():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=False)
    # force daemon_changed path so a restart happens
    s.classify_change = lambda files: "daemon_changed"
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert calls.rolled_back is True
    assert st.phases["restart"] == PhaseState.FAILED.value


@pytest.mark.asyncio
async def test_edge_only_skips_daemon_restart_and_publishes():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]   # no daemon restart
    assert calls.published is True and st.error is None


@pytest.mark.asyncio
async def test_concurrent_deploy_rejected():
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s,
                         changed_files_fn=lambda repo, a, b: ["web/x.tsx"])
    c._lock_acquired = True   # simulate an in-flight deploy
    with pytest.raises(RuntimeError, match="in progress"):
        await c.deploy_once()


@pytest.mark.asyncio
async def test_daemon_deploy_snapshots_and_restores():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"

    rpc_calls = []
    class FakeDaemon:
        async def call(self, method, **params):
            rpc_calls.append(method)
            if method == "begin_drain": return {}
            if method == "drain_status": return {"state": "ready"}
            if method == "snapshot_deploy": return {"live": [{"id": "s1", "status": "working"}]}
            if method == "restore_deploy": return {"restored": ["s1"], "failed": ["s2"]}
            if method == "close_events_connections": return {"closed": 0}
            raise AssertionError(method)

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert "snapshot_deploy" in rpc_calls and "restore_deploy" in rpc_calls
    assert st.restore == {"restored": ["s1"], "failed": ["s2"]}
    assert any("s2" in w for w in st.warnings)        # failed restore flagged, deploy stands
    assert st.phases["save"] == "done" and st.phases["resume"] == "done"
    assert st.error is None


@pytest.mark.asyncio
async def test_every_per_deploy_daemon_client_is_closed():
    """``default_daemon_factory`` builds a NEW DaemonClient per call site, and each one
    owns two ``httpx.AsyncClient``s, two transports and two SSL contexts. Left unclosed
    they hold their sockets until GC — the same short-lived-caller leak that
    ``DaemonClient.aclose`` exists to prevent, once per deploy per call site.
    """
    g = FakeGit("a", "b", [])
    s, _calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"

    built, closed = [], []

    class FakeDaemon:
        def __init__(self):
            built.append(self)
        async def call(self, method, **params):
            if method == "drain_status": return {"state": "ready"}
            if method == "snapshot_deploy": return {"live": []}
            if method == "restore_deploy": return {"restored": [], "failed": []}
            return {}
        async def aclose(self):
            closed.append(self)

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()

    assert st.error is None
    assert len(built) >= 3, "expected a client for snapshot, drain and restore"
    assert closed == built, (
        f"{len(built) - len(closed)} of {len(built)} per-deploy daemon clients leaked")


@pytest.mark.asyncio
async def test_a_daemon_client_without_aclose_does_not_fail_the_deploy():
    """The factory is an injection point and test doubles are plain objects with only
    ``call``. Teardown is best-effort: a missing (or raising) ``aclose`` must never turn a
    successful deploy into a failed one."""
    g = FakeGit("a", "b", [])
    s, _calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"

    class RudeDaemon:
        async def call(self, method, **params):
            if method == "drain_status": return {"state": "ready"}
            if method == "snapshot_deploy": return {"live": []}
            if method == "restore_deploy": return {"restored": [], "failed": []}
            return {}
        async def aclose(self):
            raise RuntimeError("transport already gone")

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: RudeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is None
    assert all(p == PhaseState.DONE.value for p in st.phases.values())


@pytest.mark.asyncio
async def test_edge_only_skips_restore_but_snapshots():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    rpc_calls = []
    class FakeDaemon:
        async def call(self, method, **params):
            rpc_calls.append(method); return {}
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert "snapshot_deploy" in rpc_calls
    assert "restore_deploy" not in rpc_calls
    assert st.error is None
    # An edge-only deploy skips the restore RPC, but the RESUME phase still COMPLETED
    # (there was nothing to restore). It must be recorded DONE — otherwise the frontend's
    # allPhasesDone() gate sees a 'pending' resume and reports a spurious deploy failure.
    assert st.phases["resume"] == PhaseState.DONE.value
    assert all(st.phases[p.value] == PhaseState.DONE.value for p in Phase)

@pytest.mark.asyncio
async def test_blocking_steps_run_off_the_event_loop():
    """Every synchronous deploy step (git/build/dep-sync/restart/health-wait/publish) must
    run via a worker thread, not inline on the controller's asyncio loop — otherwise a long
    step (npm build ~15-30s, wait_healthy ~30s) freezes the loop and starves /status, so the
    edge synthesises a spurious 'controller unavailable' mid-deploy."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def rec(name, fn):
        def wrapped(*a, **k):
            seen[name] = threading.get_ident()
            return fn(*a, **k)
        return wrapped

    g = FakeGit("a", "b", [])
    for m in ("fetch", "current_sha", "remote_sha", "short_sha"):
        setattr(g, m, rec(f"git.{m}", getattr(g, m)))
    s, _ = make_steps(healthy=True)
    for m in ("ci_verdict", "guard_working_tree", "fast_forward", "sync_deps_if_changed",
              "build_frontend", "restart_units", "wait_healthy", "publish_deployment"):
        setattr(s, m, rec(m, getattr(s, m)))

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    # The blocking steps actually executed...
    assert "build_frontend" in seen and "wait_healthy" in seen and "git.fetch" in seen
    # ...and none of them ran on the event-loop thread.
    on_loop = {name for name, tid in seen.items() if tid == loop_thread}
    assert not on_loop, f"blocking steps ran on the event loop: {sorted(on_loop)}"


@pytest.mark.asyncio
async def test_rollback_runs_off_the_event_loop():
    """The rollback path (git reset + rebuild + restart) is just as blocking as the forward
    path and must also be offloaded so /status survives a failed deploy's rollback."""
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=False)   # health gate fails -> rollback
    s.classify_change = lambda files: "daemon_changed"

    def rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None):
        seen["rollback_to"] = threading.get_ident()
    s.rollback_to = rollback

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    await c.deploy_once()
    assert seen.get("rollback_to") not in (None, loop_thread)


@pytest.mark.asyncio
async def test_restart_units_exception_triggers_rollback():
    """If restart_units raises (e.g. CalledProcessError), rollback is called and
    status.phases['restart'] == 'failed' with status.error set."""
    import subprocess
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"
    # Make restart_units raise CalledProcessError (not a DeployError)
    def bad_restart(*, daemon_changed, daemon_enabled, run=None):
        raise subprocess.CalledProcessError(1, ["systemctl"])
    s.restart_units = bad_restart
    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert calls.rolled_back is True
    assert st.phases["restart"] == PhaseState.FAILED.value
    assert st.error is not None


@pytest.mark.asyncio
async def test_running_cleared_before_rollback_executes():
    """running must flip to False before rollback starts (issue #741).

    The rollback restarts units which takes ~60s; keeping running=True that long freezes
    the overlay on "Restarting service" with no error shown — the exact symptom reported.
    """
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=False)
    s.classify_change = lambda files: "daemon_changed"
    running_at_rollback: list[bool] = []
    c_ref: list = []

    orig_rollback = s.rollback_to
    def spy_rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None):
        running_at_rollback.append(c_ref[0].status.running)
        return orig_rollback(repo, sha, daemon_changed=daemon_changed,
                             daemon_enabled=daemon_enabled, run=run)
    s.rollback_to = spy_rollback

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    c_ref.append(c)
    await c.deploy_once()
    assert running_at_rollback == [False], (
        "running must be False before rollback so the overlay can leave the spinner state")


@pytest.mark.asyncio
async def test_close_events_connections_called_before_daemon_restart():
    """close_events_connections RPC must be called before restart_units (issue #740).

    Closing the edge's /events WebSocket BEFORE the daemon gets SIGTERM lets uvicorn
    shut down immediately rather than waiting ~45s for the connection to drain.
    """
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"
    order: list[str] = []

    orig_restart = s.restart_units
    def restart_spy(*, daemon_changed, daemon_enabled, target_sha=None, run=None):
        order.append("restart_units")
        return orig_restart(daemon_changed=daemon_changed, daemon_enabled=daemon_enabled,
                            target_sha=target_sha, run=run)
    s.restart_units = restart_spy

    class FakeDaemon:
        async def call(self, method, **params):
            order.append(f"rpc:{method}")
            if method == "begin_drain": return {}
            if method == "drain_status": return {"state": "ready"}
            if method == "snapshot_deploy": return {"live": []}
            if method == "restore_deploy": return {"restored": [], "failed": []}
            if method == "close_events_connections": return {"closed": 1}
            raise AssertionError(method)
        async def aclose(self): pass

    c = DeployController("/r", health_url="http://h", venv_pip="/v/pip",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is None
    assert "rpc:close_events_connections" in order
    close_idx = order.index("rpc:close_events_connections")
    restart_idx = order.index("restart_units")
    assert close_idx < restart_idx, "close_events_connections must precede restart_units"

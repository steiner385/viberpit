import pytest
import threading
from types import SimpleNamespace
from server.deploy.controller import DeployController, default_daemon_factory
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
    mod.sync_deps_if_changed = lambda repo, a, b, *, venv_python, run=None: []
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
    def wait_healthy(sha, **k):
        calls.health_kwargs = k
        return healthy
    mod.wait_healthy = wait_healthy
    # The controller selects a poll window by whether a remote daemon is PRESENT
    # (daemon_changed OR remote_daemon_configured): the edge must re-attach to a
    # remote daemon over the tailnet after its own restart, outlasting the
    # edge-only window even when the daemon's code did not change.
    mod.HEALTH_ATTEMPTS_EDGE_ONLY = 15
    mod.HEALTH_ATTEMPTS_WITH_DAEMON = 75
    mod.remote_daemon_configured = lambda: False
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
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.error is None
    assert any("frontend" in w for w in st.warnings)


@pytest.mark.asyncio
async def test_red_ci_refuses_before_any_change():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(ci="red")
    c = DeployController("/r", health_url="http://h", venv_python="/v/python", git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_health_failure_triggers_rollback():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=False)
    # force daemon_changed path so a restart happens
    s.classify_change = lambda files: "daemon_changed"
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert calls.rolled_back is True
    assert st.phases["restart"] == PhaseState.FAILED.value


@pytest.mark.asyncio
async def test_unexpected_exception_after_fast_forward_is_reported_and_rolled_back():
    """A non-DeployError escaping a step (here: the OSError of a missing pip launcher,
    2026-08-30 23:19 EDT) must end the deploy in a REPORTED state — phase FAILED, error
    text, running=False — and roll the checkout back, because the fast-forward already
    moved it. Before: the task died with "Task exception was never retrieved", error
    stayed None, `update` stayed "running", and the checkout sat ahead of the running
    process with the pending banner saying there was nothing to deploy."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps()

    def missing_launcher(repo, a, b, *, venv_python, run=None):
        raise FileNotFoundError(2, "No such file or directory", "/r/.venv/bin/pip")
    s.sync_deps_if_changed = missing_launcher
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.phases["update"] == PhaseState.FAILED.value
    assert st.error and "FileNotFoundError" in st.error and "update" in st.error
    assert st.running is False
    assert calls.rolled_back is True, "the fast-forward had happened: must rewind"
    assert c._lock_acquired is False


@pytest.mark.asyncio
async def test_unexpected_exception_in_preflight_changes_nothing():
    """Before the fast-forward there is nothing to roll back — mirror the DeployError rule."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps()
    def boom(sha, gh_api=None):
        raise RuntimeError("gh exploded")
    s.ci_verdict = boom
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s)
    st = await c.deploy_once()
    assert st.phases["preflight"] == PhaseState.FAILED.value
    assert st.error and "RuntimeError" in st.error
    assert calls.rolled_back is False


@pytest.mark.asyncio
async def test_edge_only_skips_daemon_restart_and_publishes():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]   # no daemon restart
    assert calls.published is True and st.error is None


@pytest.mark.asyncio
async def test_edge_only_with_remote_daemon_uses_long_health_window():
    """An edge-only deploy must still use the LONG health window when a remote daemon is
    configured: after the edge restarts it must re-attach to the daemon over a settling
    tailnet, and /healthz blocks on that re-attach reporting daemon:"unavailable". Sizing the
    window by daemon_changed alone rolled back a healthy frontend-only deploy (#780). The
    daemon-sha assertion still keys off daemon_changed — an edge-only deploy leaves the daemon
    on its prior commit by design, so require_daemon_sha stays False."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.remote_daemon_configured = lambda: True
    c = DeployController("/r", health_url="http://h/healthz", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]                       # daemon NOT restarted
    assert calls.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_WITH_DAEMON
    assert calls.health_kwargs["require_daemon_sha"] is False
    assert st.error is None


@pytest.mark.asyncio
async def test_edge_only_single_host_keeps_short_health_window():
    """No remote daemon means nothing slow to re-attach to; keep the fast edge-only window."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.remote_daemon_configured = lambda: False
    c = DeployController("/r", health_url="http://h/healthz", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_EDGE_ONLY
    assert st.error is None


@pytest.mark.asyncio
async def test_concurrent_deploy_rejected():
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: RudeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is None
    assert all(p == PhaseState.DONE.value for p in st.phases.values())


def _spy_daemon_client(monkeypatch):
    """Capture the endpoint DaemonClient is built with, without opening any transport."""
    from server.ipc import client as client_mod
    seen = []
    class SpyClient:
        def __init__(self, endpoint, *, client_tls=None):
            seen.append(endpoint)
    monkeypatch.setattr(client_mod, "DaemonClient", SpyClient)
    return seen


def test_default_daemon_factory_uses_the_configured_https_endpoint(monkeypatch):
    """The controller must resolve the daemon exactly as the edge does. Hard-wiring the
    legacy UDS path meant an https-only daemon (dobby, 2026-08-30) got every pre-deploy
    RPC — snapshot, drain, close_events_connections — failing, and a daemon-changing
    deploy restarted the daemon without draining in-flight turns."""
    monkeypatch.setenv("CCSWITCH_SESSIOND_ENDPOINT", "https://runner.example:9443")
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    seen = _spy_daemon_client(monkeypatch)
    factory = default_daemon_factory(True)
    assert factory is not None
    factory()
    assert len(seen) == 1
    ep = seen[0]
    assert not ep.is_unix and ep.host == "runner.example" and ep.port == 9443


def test_default_daemon_factory_keeps_the_legacy_socket_when_no_endpoint_is_set(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SESSIOND_ENDPOINT", raising=False)
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/run/x/sessiond.sock")
    seen = _spy_daemon_client(monkeypatch)
    default_daemon_factory(True)()
    assert seen[0].is_unix and seen[0].path == "/run/x/sessiond.sock"


def test_default_daemon_factory_is_none_when_daemon_disabled():
    assert default_daemon_factory(False) is None


@pytest.mark.asyncio
async def test_edge_only_skips_restore_but_snapshots():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    rpc_calls = []
    class FakeDaemon:
        async def call(self, method, **params):
            rpc_calls.append(method); return {}
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
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

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         daemon_client_factory=lambda: FakeDaemon(),
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is None
    assert "rpc:close_events_connections" in order
    close_idx = order.index("rpc:close_events_connections")
    restart_idx = order.index("restart_units")
    assert close_idx < restart_idx, "close_events_connections must precede restart_units"


# --- controller self-refresh timing (issue #865) -------------------------------------
# The +10s systemd-run used to be scheduled DURING the RESTART phase; resume + publish
# routinely take longer, so the controller process was replaced mid-deploy, wiping the
# in-memory DeployStatus and reporting a completed upgrade as "did not complete".


@pytest.mark.asyncio
async def test_controller_refresh_scheduled_only_after_deploy_reports():
    """The self-restart must be handed to systemd AFTER the deploy is terminal: every
    phase recorded, publish done, running=False. Scheduling it from the RESTART phase
    put the +10s timer in the middle of resume/publish (#865)."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.deploy_code_changed = lambda changed: True
    order: list[str] = []
    snapshot: dict = {}

    orig_publish = s.publish_deployment
    def publish_spy(sha, gh_api_json=None):
        order.append("publish")
        return orig_publish(sha, gh_api_json)
    s.publish_deployment = publish_spy

    c_ref: list = []
    def sched_spy(**k):
        order.append("schedule_refresh")
        st = c_ref[0].status
        snapshot["running"] = st.running
        snapshot["phases"] = dict(st.phases)
        return True
    s.schedule_controller_restart = sched_spy

    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s,
                         changed_files_fn=lambda repo, a, b: ["server/deploy/steps.py"])
    c_ref.append(c)
    st = await c.deploy_once()
    assert st.error is None
    assert order == ["publish", "schedule_refresh"], (
        "the refresh must be scheduled after publish, never mid-deploy")
    assert snapshot["running"] is False, "status must be terminal when the timer starts"
    assert all(snapshot["phases"][p.value] == PhaseState.DONE.value for p in Phase)


@pytest.mark.asyncio
async def test_no_controller_refresh_after_failed_deploy():
    """A failed deploy rolls the checkout back to the running code, so there is no new
    deploy code to pick up — restarting the controller would only wipe live status."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=False)
    s.classify_change = lambda files: "daemon_changed"
    s.deploy_code_changed = lambda changed: True
    scheduled: list[bool] = []
    s.schedule_controller_restart = lambda **k: scheduled.append(True) or True
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True,
                         changed_files_fn=lambda repo, a, b: ["server/deploy/steps.py"])
    st = await c.deploy_once()
    assert calls.rolled_back is True and st.error is not None
    assert scheduled == []


@pytest.mark.asyncio
async def test_unscheduled_controller_refresh_is_a_warning():
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    s.deploy_code_changed = lambda changed: True
    s.schedule_controller_restart = lambda **k: False
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s,
                         changed_files_fn=lambda repo, a, b: ["server/deploy/steps.py"])
    st = await c.deploy_once()
    assert st.error is None
    assert any("controller restart" in w for w in st.warnings)


# --- status durability across the self-restart (issue #865) --------------------------


@pytest.mark.asyncio
async def test_final_status_survives_controller_restart(tmp_path):
    """Self-restart after a deploy → /status still reports all phases DONE. The new
    controller process loads the persisted terminal state instead of starting from an
    empty DeployStatus that the UI reads as 'Upgrade failed'."""
    path = str(tmp_path / "deploy-status.json")
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, status_path=path,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None

    reborn = DeployController("/r", health_url="http://h", venv_python="/v/python",
                              git_mod=g, steps_mod=s, status_path=path)
    assert reborn.status.running is False and reborn.status.error is None
    assert reborn.status.target_sha == "b"
    assert all(reborn.status.phases[p.value] == PhaseState.DONE.value for p in Phase)


@pytest.mark.asyncio
async def test_failed_status_survives_controller_restart(tmp_path):
    """The terminal state of a FAILED deploy (error + failed phase + restore result) is
    just as load-bearing — it is the only place the failure is surfaced."""
    path = str(tmp_path / "deploy-status.json")
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=False)
    s.classify_change = lambda files: "daemon_changed"
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, daemon_enabled=True, status_path=path,
                         changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is not None

    reborn = DeployController("/r", health_url="http://h", venv_python="/v/python",
                              git_mod=g, steps_mod=s, status_path=path)
    assert reborn.status.error == st.error
    assert reborn.status.phases["restart"] == PhaseState.FAILED.value
    assert reborn.status.running is False


def test_interrupted_deploy_loads_as_reported_interruption(tmp_path):
    """If the process dies while a deploy is mid-flight, the persisted status says
    running=True with a phase 'running'. Loading that verbatim would freeze the UI
    spinner forever AND 409-block /start. Sanitize it to a terminal, honest state."""
    import json
    path = tmp_path / "deploy-status.json"
    path.write_text(json.dumps({
        "running": True, "error": None, "warnings": [], "targetSha": "457730f",
        "restore": {},
        "phases": {"preflight": "done", "save": "done", "update": "done",
                   "report": "done", "restart": "done", "resume": "running"},
    }))
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, status_path=str(path))
    assert c.status.running is False, "a fresh process is not running any deploy"
    assert c.status.phases["resume"] == PhaseState.FAILED.value
    assert c.status.error and "restart" in c.status.error


def test_missing_or_corrupt_status_file_is_ignored(tmp_path):
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    fresh = DeployController("/r", health_url="http://h", venv_python="/v/python",
                             git_mod=g, steps_mod=s,
                             status_path=str(tmp_path / "nope.json"))
    assert fresh.status.phases == {} and fresh.status.running is False

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, status_path=str(bad))
    assert c.status.phases == {} and c.status.error is None


@pytest.mark.asyncio
async def test_unwritable_status_path_does_not_fail_deploy(tmp_path):
    """Persistence is bookkeeping: a read-only disk must never fail an upgrade."""
    path = str(tmp_path / "no-such-dir" / "deploy-status.json")
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    c = DeployController("/r", health_url="http://h", venv_python="/v/python",
                         git_mod=g, steps_mod=s, status_path=path,
                         changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    assert all(st.phases[p.value] == PhaseState.DONE.value for p in Phase)

import pytest
import threading
from types import SimpleNamespace
from server.deploy.controller import DeployController
from server.deploy.runners import HostProgress, RunnerTarget
from server.deploy.types import Phase, PhaseState


class FakeGit:
    def __init__(self, local, remote, changed, ancestors=None):
        self._local, self._remote, self._changed = local, remote, changed
        self.reset_to = None
        # (ancestor, descendant) pairs is_ancestor answers True for; None => always True.
        self._ancestors = ancestors
        self.ancestor_checks: list[tuple[str, str]] = []
    def fetch(self, repo, ref="master", run=None): pass
    def current_sha(self, repo, run=None): return self._local
    def remote_sha(self, repo, ref="origin/master", run=None): return self._remote
    def short_sha(self, repo, sha, run=None): return sha[:7]
    def is_ancestor(self, repo, ancestor, descendant, run=None):
        self.ancestor_checks.append((ancestor, descendant))
        return self._ancestors is None or (ancestor, descendant) in self._ancestors


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
    # Runner (daemon) restarts — local AND remote — belong to server.deploy.runners now;
    # steps only knows how to restart the EDGE unit.
    def restart_edge(run=None):
        calls.restarted = ["ccswitch.service"]
        return ["ccswitch.service"]
    mod.restart_edge = restart_edge
    mod.remote_runners_configured = lambda hosts=None: False
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
    # The controller selects a poll window by whether a remote RUNNER is PRESENT
    # (daemon_changed OR remote_runners_configured(targets)): the edge must re-attach to
    # a remote daemon over the tailnet after its own restart, outlasting the edge-only
    # window even when the daemon's code did not change.
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


def make_runners(*, targets=None, outcome="done", moved=True, restore=None):
    """Fake runners module: records the fan-out call and returns the same state per host."""
    calls = SimpleNamespace(deploy_kwargs=None, rewound=None, loaded=0)
    mod = SimpleNamespace()
    hosts = targets if targets is not None else [
        RunnerTarget(name="rocky", endpoint="https://rocky:9443", enabled=True, healthy=True,
                     ssh_host="rocky", repo_path="/srv/cc")]

    async def load():
        calls.loaded += 1
        return hosts

    async def deploy(targets, *, on_progress=None, **kw):
        calls.deploy_kwargs = kw
        out = {}
        for t in targets:
            st = outcome(t.name) if callable(outcome) else outcome
            p = HostProgress(state=st, step=None if st == "done" else "merge",
                             error=None if st == "done" else "merge failed (rc=1): boom",
                             sha=kw["target_sha"] if st == "done" else None, moved=moved,
                             restore=(restore(t.name) if callable(restore) else restore))
            if on_progress:
                on_progress(t.name, p)
            out[t.name] = p
        return out

    async def rewind(targets, results, *, sha, **kw):
        calls.rewound = (sorted(n for n, p in results.items() if p.moved), sha)

    mod.load_runner_targets = load
    mod.deploy_runners = deploy
    mod.rewind_runners = rewind
    mod.FAILED_STATES = frozenset({"failed", "unconfigured"})
    return mod, calls


def make_state():
    calls = SimpleNamespace(recorded=[])
    mod = SimpleNamespace()

    async def record(*, sha, short, daemon_changed, by="deploy-controller"):
        calls.recorded.append((sha, short, daemon_changed))
    mod.record_deploy = record
    return mod, calls


def make_controller(g, s, *, runners=None, state=None, daemon_enabled=True,
                    health_url="http://h", **kw):
    r, _ = runners or make_runners()
    st, _ = state or make_state()
    kw.setdefault("changed_files_fn",
                  lambda repo, a, b: list(getattr(s, "_changed_files", []) or []))
    return DeployController("/r", health_url=health_url, venv_python="/v/python",
                            git_mod=g, steps_mod=s, runners_mod=r, state_mod=st,
                            daemon_enabled=daemon_enabled, local_name="dobby", **kw)


@pytest.mark.asyncio
async def test_up_to_date_is_noop():
    g = FakeGit("same", "same", [])
    s, _ = make_steps()
    c = make_controller(g, s)
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
    c = make_controller(g, s)
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
    c = make_controller(g, s)
    st = await c.deploy_once()
    assert st.error is None
    assert any("frontend" in w for w in st.warnings)


@pytest.mark.asyncio
async def test_red_ci_refuses_before_any_change():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(ci="red")
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False
    assert rc.deploy_kwargs is None, "nothing fanned out to the fleet"


@pytest.mark.asyncio
async def test_health_failure_triggers_rollback():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=False)
    # force daemon_changed path so a restart happens
    s.classify_change = lambda files: "daemon_changed"
    c = make_controller(g, s,
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
    c = make_controller(g, s)
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
    c = make_controller(g, s)
    st = await c.deploy_once()
    assert st.phases["preflight"] == PhaseState.FAILED.value
    assert st.error and "RuntimeError" in st.error
    assert calls.rolled_back is False


@pytest.mark.asyncio
async def test_edge_only_restarts_only_the_edge_and_publishes():
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc),
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]   # steps only restarts the edge
    assert rc.deploy_kwargs["daemon_changed"] is False, "runners update their checkout only"
    assert calls.published is True and st.error is None
    # An edge-only deploy restarts no daemon, but the RESUME phase still COMPLETED (there
    # was nothing to restore) — otherwise the frontend's allPhasesDone() gate sees a
    # 'pending' resume and reports a spurious deploy failure.
    assert st.phases["resume"] == PhaseState.DONE.value
    assert all(st.phases[p.value] == PhaseState.DONE.value for p in Phase)


@pytest.mark.asyncio
async def test_edge_only_with_remote_runner_uses_long_health_window():
    """An edge-only deploy must still use the LONG health window when a remote runner is
    configured: after the edge restarts it must re-attach to the daemon over a settling
    tailnet, and /healthz blocks on that re-attach reporting daemon:"unavailable". Sizing the
    window by daemon_changed alone rolled back a healthy frontend-only deploy (#780). The
    daemon-sha assertion still keys off daemon_changed — an edge-only deploy leaves the daemon
    on its prior commit by design, so require_daemon_sha stays False."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.remote_runners_configured = lambda hosts=None: True
    c = make_controller(g, s, health_url="http://h/healthz",
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.restarted == ["ccswitch.service"]
    assert calls.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_WITH_DAEMON
    assert calls.health_kwargs["require_daemon_sha"] is False
    assert st.error is None


@pytest.mark.asyncio
async def test_edge_only_single_host_keeps_short_health_window():
    """No remote runner means nothing slow to re-attach to; keep the fast edge-only window."""
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.remote_runners_configured = lambda hosts=None: False
    c = make_controller(g, s, health_url="http://h/healthz",
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert calls.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_EDGE_ONLY
    assert st.error is None


@pytest.mark.asyncio
async def test_health_window_sized_by_remote_runner_presence():
    """remote_runners_configured is asked about the LOADED targets, not the environment."""
    g = FakeGit("old0000000", "new1111111", ["web/src/App.tsx"])
    s, sc = make_steps(changed=["web/src/App.tsx"])
    s.remote_runners_configured = lambda hosts=None: bool(hosts)
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    await c.deploy_once()
    assert sc.health_kwargs["attempts"] == s.HEALTH_ATTEMPTS_WITH_DAEMON
    assert sc.health_kwargs["require_daemon_sha"] is False


# --- the runners phase (spec §2) ------------------------------------------------------


@pytest.mark.asyncio
async def test_runners_phase_runs_before_edge_restart_and_records_state():
    g = FakeGit("old0000000", "new1111111", ["server/sessions/orchestrator.py"])
    s, sc = make_steps(changed=["server/sessions/orchestrator.py"])
    order = []
    s.restart_edge = lambda run=None: order.append("edge") or ["ccswitch.service"]
    r, rc = make_runners()
    orig = r.deploy_runners
    async def deploy(targets, **kw):
        order.append("runners")
        return await orig(targets, **kw)
    r.deploy_runners = deploy
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error is None
    assert order == ["runners", "edge"]
    assert st.phases["runners"] == "done" and st.phases["restart"] == "done"
    # The runners merge/reset their checkout to the FULL sha (RunnerDeploy verifies /info
    # by prefix, so a short /info sha still matches); the short sha is the edge's health
    # gate and the deploy_state record.
    assert st.hosts["rocky"]["state"] == "done" and st.hosts["rocky"]["sha"] == "new1111111"
    assert rc.deploy_kwargs["daemon_changed"] is True
    assert rc.deploy_kwargs["target_sha"] == "new1111111"
    assert rc.deploy_kwargs["local_name"] == "dobby"
    assert stc.recorded == [("new1111111", "new1111", True)]


@pytest.mark.asyncio
async def test_edge_only_deploy_still_updates_runner_checkouts_without_restart():
    g = FakeGit("old0000000", "new1111111", ["web/src/App.tsx"])
    s, _ = make_steps(changed=["web/src/App.tsx"])
    r, rc = make_runners()
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error is None
    assert rc.deploy_kwargs["daemon_changed"] is False
    assert stc.recorded == [("new1111111", "new1111", False)]


@pytest.mark.asyncio
async def test_deps_changed_is_derived_from_the_diff():
    g = FakeGit("old0000000", "new1111111", ["uv.lock", "server/x.py"])
    s, _ = make_steps(changed=["uv.lock", "server/x.py"])
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    await c.deploy_once()
    assert rc.deploy_kwargs["deps_changed"] is True


@pytest.mark.asyncio
async def test_deps_unchanged_when_the_diff_has_no_lockfile():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    await c.deploy_once()
    assert rc.deploy_kwargs["deps_changed"] is False


@pytest.mark.asyncio
async def test_uncomputable_diff_fails_safe_to_deps_changed():
    """`git diff` failing yields None — classify_change already fails safe to
    daemon_changed, and the dep sync must fail safe the same way."""
    g = FakeGit("old0000000", "new1111111", [])
    s, _ = make_steps()
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc), changed_files_fn=lambda repo, a, b: None)
    await c.deploy_once()
    assert rc.deploy_kwargs["deps_changed"] is True


@pytest.mark.asyncio
async def test_failed_runner_fails_the_deploy_but_the_edge_still_lands():
    """Decision 1a: name the host, keep the edge, never roll back, never stop the others."""
    g = FakeGit("old0000000", "new1111111", ["server/sessions/orchestrator.py"])
    s, sc = make_steps(changed=["server/sessions/orchestrator.py"])
    targets = [RunnerTarget(name=n, endpoint=f"https://{n}", enabled=True, healthy=True,
                            ssh_host=n, repo_path="/srv/cc") for n in ("rocky", "sunny")]
    r, rc = make_runners(targets=targets, outcome=lambda n: "failed" if n == "rocky" else "done")
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.phases["runners"] == "failed"
    assert st.error and "rocky" in st.error and "merge failed" in st.error
    assert sc.restarted == ["ccswitch.service"], "the edge still restarts"
    assert sc.rolled_back is False and rc.rewound is None
    assert st.phases["restart"] == "done" and st.phases["resume"] == "done"
    assert st.hosts["sunny"]["state"] == "done"
    assert stc.recorded, "the fleet's deployed version moved: the failed runner is fenced against it"


@pytest.mark.asyncio
async def test_offline_runner_is_reported_not_failed():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners(outcome="offline")
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.error is None and st.phases["runners"] == "done"
    assert st.hosts["rocky"]["state"] == "offline"


@pytest.mark.asyncio
async def test_unconfigured_online_runner_fails_the_deploy():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners(outcome="unconfigured")
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.phases["runners"] == "failed" and "rocky" in (st.error or "")


@pytest.mark.asyncio
async def test_per_host_progress_is_published_as_it_arrives():
    """The fan-out streams progress: /status must show a host's state mid-deploy, not only
    once every runner finished."""
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, _ = make_steps(changed=["server/x.py"])
    r, rc = make_runners()
    seen: list[dict] = []
    orig = r.deploy_runners
    c_ref: list = []

    async def deploy(targets, *, on_progress=None, **kw):
        def spy(name, p):
            on_progress(name, p)
            seen.append(dict(c_ref[0].status.hosts))
        return await orig(targets, on_progress=spy, **kw)
    r.deploy_runners = deploy
    c = make_controller(g, s, runners=(r, rc))
    c_ref.append(c)
    await c.deploy_once()
    assert seen and seen[0]["rocky"]["state"] == "done"


@pytest.mark.asyncio
async def test_resume_aggregates_every_runner_restore():
    """Per-runner restores run inside the runners phase; the RESUME phase folds them into
    the one status.restore the UI reads, and a lost session is a warning, not a failure."""
    g = FakeGit("old0000000", "new1111111", ["server/sessions/orchestrator.py"])
    s, _ = make_steps(changed=["server/sessions/orchestrator.py"])
    targets = [RunnerTarget(name=n, endpoint=f"https://{n}", enabled=True, healthy=True,
                            ssh_host=n, repo_path="/srv/cc") for n in ("rocky", "sunny")]
    r, rc = make_runners(targets=targets,
                         restore=lambda n: {"restored": [f"{n}-s1"],
                                            "failed": [f"{n}-s2"] if n == "rocky" else []})
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.error is None
    assert sorted(st.restore["restored"]) == ["rocky-s1", "sunny-s1"]
    assert st.restore["failed"] == ["rocky-s2"]
    assert any("rocky-s2" in w for w in st.warnings)
    assert st.phases["resume"] == PhaseState.DONE.value


@pytest.mark.asyncio
async def test_edge_health_failure_rolls_back_edge_and_rewinds_moved_runners():
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, sc = make_steps(healthy=False, changed=["server/x.py"])
    r, rc = make_runners(moved=True)
    st_mod, stc = make_state()
    c = make_controller(g, s, runners=(r, rc), state=(st_mod, stc))
    st = await c.deploy_once()
    assert st.error and "health gate" in st.error
    assert sc.rolled_back is True
    assert rc.rewound == (["rocky"], "old0000000")
    assert stc.recorded == [], "a rolled-back deploy must not move the deployed version"


@pytest.mark.asyncio
async def test_rollback_before_the_runners_phase_rewinds_nothing():
    """A failure in update/report happens before any runner moved: there is nothing to
    rewind, and rewind_runners must not be called with an empty result set."""
    g = FakeGit("old0000000", "new1111111", ["server/x.py"])
    s, sc = make_steps(changed=["server/x.py"])
    def missing_launcher(repo, a, b, *, venv_python, run=None):
        raise FileNotFoundError(2, "No such file or directory", "/r/.venv/bin/pip")
    s.sync_deps_if_changed = missing_launcher
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc))
    st = await c.deploy_once()
    assert st.error and sc.rolled_back is True
    assert rc.rewound is None


# --- explicit target sha: deploy the newest GREEN commit when the tip is red ----------


def _spy_steps_for_target(s, seen):
    """Instrument every sha-consuming step so the test can assert the WHOLE deploy
    targets the explicit sha, never origin/master's tip."""
    def ci(sha, gh_api=None):
        seen.setdefault("ci", []).append(sha)
        return "green"
    s.ci_verdict = ci
    def ff(repo, ref="master", run=None):
        seen["fast_forward_ref"] = ref
    s.fast_forward = ff
    def deps(repo, a, b, *, venv_python, run=None):
        seen["deps_range"] = (a, b)
        return []
    s.sync_deps_if_changed = deps
    def restart_edge(run=None):
        seen["restarted_edge"] = True
        return ["ccswitch.service"]
    s.restart_edge = restart_edge
    def publish(sha, gh_api_json=None):
        seen["published"] = sha
        return None
    s.publish_deployment = publish


@pytest.mark.asyncio
async def test_deploy_once_targets_an_explicit_green_ancestor_sha():
    """deploy_once(target_sha=...) with a green sha inside live..origin/master succeeds,
    and EVERY step — verdict, fast-forward, dep-sync range, changed-files range, runner
    fan-out, publish — sees that sha, not the (red) origin/master tip."""
    g = FakeGit("1111111", "9999999", [])
    s, calls = make_steps()
    seen: dict = {}
    _spy_steps_for_target(s, seen)
    r, rc = make_runners()
    c = make_controller(
        g, s, runners=(r, rc),
        changed_files_fn=lambda repo, a, b: seen.setdefault("changed_range", (a, b)) and ["web/src/App.tsx"])
    st = await c.deploy_once(target_sha="5555555")
    assert st.error is None
    assert st.target_sha == "5555555"
    assert seen["ci"] == ["5555555"], "CI is gated on the TARGET, never the tip"
    assert seen["fast_forward_ref"] == "5555555"
    assert seen["deps_range"] == ("1111111", "5555555")
    assert seen["changed_range"] == ("1111111", "5555555")
    assert rc.deploy_kwargs["target_sha"] == "5555555"
    assert seen["restarted_edge"] is True
    assert seen["published"] == "5555555"
    # Ancestry validated both ways: target inside origin/master, live inside target.
    assert g.ancestor_checks == [("5555555", "9999999"), ("1111111", "5555555")]


@pytest.mark.asyncio
async def test_non_ancestor_target_sha_refused_in_preflight():
    """A target outside live..origin/master (not an ancestor of the tip) is refused in
    preflight — nothing fast-forwarded, nothing restarted, nothing rolled back."""
    g = FakeGit("1111111", "9999999", [],
                ancestors={("1111111", "5555555")})   # target NOT ancestor of tip
    s, calls = make_steps()
    seen: dict = {}
    _spy_steps_for_target(s, seen)
    r, rc = make_runners()
    c = make_controller(g, s, runners=(r, rc),
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once(target_sha="5555555")
    assert st.error and "preflight" in st.error
    assert st.phases["preflight"] == PhaseState.FAILED.value
    assert "fast_forward_ref" not in seen and "restarted_edge" not in seen
    assert rc.deploy_kwargs is None
    assert calls.rolled_back is False


@pytest.mark.asyncio
async def test_target_sha_not_reachable_from_live_refused_in_preflight():
    """A target BEHIND the live sha (or on a rolled-back branch) fails the second
    ancestry leg: live must be an ancestor of the target or the ff-merge cannot land."""
    g = FakeGit("1111111", "9999999", [],
                ancestors={("5555555", "9999999")})   # live NOT ancestor of target
    s, calls = make_steps()
    c = make_controller(g, s,
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once(target_sha="5555555")
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_red_target_sha_refused_in_preflight():
    g = FakeGit("1111111", "9999999", [])
    s, calls = make_steps(ci="red")
    c = make_controller(g, s,
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once(target_sha="5555555")
    assert st.error and "preflight" in st.error
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_malformed_target_sha_refused_before_any_git_command():
    """The sha lands in git argv (merge --ff-only <sha>): a non-hex value is refused
    outright, before even the ancestry checks run."""
    g = FakeGit("1111111", "9999999", [])
    s, calls = make_steps()
    c = make_controller(g, s)
    st = await c.deploy_once(target_sha="--upload-pack=evil")
    assert st.error and "preflight" in st.error
    assert g.ancestor_checks == []
    assert calls.restarted == [] and calls.rolled_back is False


@pytest.mark.asyncio
async def test_no_target_sha_keeps_default_tip_behaviour():
    """Without target_sha nothing consults ancestry and the deploy targets origin/master
    exactly as before."""
    g = FakeGit("1111111", "9999999", [])
    s, calls = make_steps()
    seen: dict = {}
    _spy_steps_for_target(s, seen)
    c = make_controller(g, s,
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None and st.target_sha == "9999999"
    assert g.ancestor_checks == []
    assert seen["ci"] == ["9999999"]
    assert seen["fast_forward_ref"] == "master", "the default path keeps the branch ref"
    assert seen["published"] == "9999999"


@pytest.mark.asyncio
async def test_concurrent_deploy_rejected():
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = make_controller(g, s, changed_files_fn=lambda repo, a, b: ["web/x.tsx"])
    c._lock_acquired = True   # simulate an in-flight deploy
    with pytest.raises(RuntimeError, match="in progress"):
        await c.deploy_once()


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
              "build_frontend", "restart_edge", "wait_healthy", "publish_deployment"):
        setattr(s, m, rec(m, getattr(s, m)))

    c = make_controller(g, s, changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    # The blocking steps actually executed...
    assert "build_frontend" in seen and "wait_healthy" in seen and "git.fetch" in seen
    assert "restart_edge" in seen
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

    c = make_controller(g, s,
                        changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    await c.deploy_once()
    assert seen.get("rollback_to") not in (None, loop_thread)


@pytest.mark.asyncio
async def test_restart_edge_exception_triggers_rollback():
    """If restart_edge raises (e.g. CalledProcessError), rollback is called and
    status.phases['restart'] == 'failed' with status.error set."""
    import subprocess
    g = FakeGit("a", "b", [])
    s, calls = make_steps(healthy=True)
    s.classify_change = lambda files: "daemon_changed"
    def bad_restart(run=None):
        raise subprocess.CalledProcessError(1, ["systemctl"])
    s.restart_edge = bad_restart
    c = make_controller(g, s,
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

    c = make_controller(g, s,
                        changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    c_ref.append(c)
    await c.deploy_once()
    assert running_at_rollback == [False], (
        "running must be False before rollback so the overlay can leave the spinner state")


@pytest.mark.asyncio
async def test_publish_failure_is_a_warning_not_an_error():
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    s.publish_deployment = lambda sha, gh_api_json=None: "gh: 502"
    c = make_controller(g, s, changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    assert any("GitHub deployment" in w for w in st.warnings)


@pytest.mark.asyncio
async def test_unrecordable_deploy_state_is_a_warning_not_an_error():
    """The deploy landed; failing to write the deploy_state row must not fail it — but it
    must be surfaced, since the skew gate reads that row."""
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    st_mod, stc = make_state()
    async def boom(*, sha, short, daemon_changed, by="deploy-controller"):
        raise RuntimeError("db down")
    st_mod.record_deploy = boom
    c = make_controller(g, s, state=(st_mod, stc),
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    assert any("deploy_state" in w for w in st.warnings)


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

    c = make_controller(g, s,
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
    c = make_controller(g, s,
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
    c = make_controller(g, s,
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
    c = make_controller(g, s, status_path=path,
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None

    reborn = make_controller(g, s, status_path=path)
    assert reborn.status.running is False and reborn.status.error is None
    assert reborn.status.target_sha == "b"
    assert all(reborn.status.phases[p.value] == PhaseState.DONE.value for p in Phase)
    assert reborn.status.hosts["rocky"]["state"] == "done", "per-host state survives too"


@pytest.mark.asyncio
async def test_failed_status_survives_controller_restart(tmp_path):
    """The terminal state of a FAILED deploy (error + failed phase + restore result) is
    just as load-bearing — it is the only place the failure is surfaced."""
    path = str(tmp_path / "deploy-status.json")
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=False)
    s.classify_change = lambda files: "daemon_changed"
    c = make_controller(g, s, status_path=path,
                        changed_files_fn=lambda repo, a, b: ["server/sessions/orchestrator.py"])
    st = await c.deploy_once()
    assert st.error is not None

    reborn = make_controller(g, s, status_path=path)
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
        "restore": {}, "hosts": {"rocky": {"state": "running", "step": "merge",
                                           "error": None, "sha": None, "restore": None}},
        "phases": {"preflight": "done", "save": "done", "update": "done",
                   "report": "done", "runners": "done", "restart": "done",
                   "resume": "running"},
    }))
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    c = make_controller(g, s, status_path=str(path))
    assert c.status.running is False, "a fresh process is not running any deploy"
    assert c.status.phases["resume"] == PhaseState.FAILED.value
    assert c.status.error and "restart" in c.status.error
    assert c.status.hosts["rocky"]["step"] == "merge"


def test_missing_or_corrupt_status_file_is_ignored(tmp_path):
    g = FakeGit("a", "b", [])
    s, _ = make_steps()
    fresh = make_controller(g, s, status_path=str(tmp_path / "nope.json"))
    assert fresh.status.phases == {} and fresh.status.running is False

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    c = make_controller(g, s, status_path=str(bad))
    assert c.status.phases == {} and c.status.error is None


@pytest.mark.asyncio
async def test_unwritable_status_path_does_not_fail_deploy(tmp_path):
    """Persistence is bookkeeping: a read-only disk must never fail an upgrade."""
    path = str(tmp_path / "no-such-dir" / "deploy-status.json")
    g = FakeGit("a", "b", [])
    s, _ = make_steps(healthy=True)
    c = make_controller(g, s, status_path=path,
                        changed_files_fn=lambda repo, a, b: ["web/src/App.tsx"])
    st = await c.deploy_once()
    assert st.error is None
    assert all(st.phases[p.value] == PhaseState.DONE.value for p in Phase)


@pytest.mark.asyncio
async def test_status_wire_carries_hosts():
    from server.deploy.types import DeployStatus
    st = DeployStatus(target_sha="x", hosts={"rocky": {"state": "done", "step": None,
                                                      "error": None, "sha": "x", "restore": None}})
    d = st.to_dict()
    assert d["hosts"]["rocky"]["state"] == "done"
    assert DeployStatus.from_dict(d).hosts == st.hosts

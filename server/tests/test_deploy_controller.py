import asyncio
import pytest
from types import SimpleNamespace
from server.deploy.controller import DeployController
from server.deploy.types import PhaseState


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
    def restart(*, daemon_changed, daemon_enabled, run=None):
        u = (["ccswitch-daemon.service"] if daemon_changed and daemon_enabled else []) + ["ccswitch.service"]
        calls.restarted = u; return u
    mod.restart_units = restart
    mod.wait_healthy = lambda sha, **k: healthy
    def rollback(repo, sha, *, daemon_changed, daemon_enabled, run=None):
        calls.rolled_back = True
    mod.rollback_to = rollback
    def publish(sha, gh_api_json=None):
        calls.published = True; return True
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

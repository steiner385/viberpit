import functools

import pytest
from fastapi.testclient import TestClient
from server.deploy import app as deploy_app
from server.deploy import detect
from server.deploy.detect import DetectLoop, PendingState
from server.deploy.types import DeployStatus


class FakeController:
    def __init__(self):
        self.status = DeployStatus(target_sha="def", running=False)
        self.started = 0
        self.target_shas: list[str | None] = []
        self._running = False

    async def deploy_once(self, target_sha=None):
        if self._running:
            raise RuntimeError("deploy already in progress")
        self.started += 1
        self.target_shas.append(target_sha)
        self.status = DeployStatus(target_sha="def", running=False, phases={"update": "done"})
        return self.status


@pytest.fixture
def client():
    loop = DetectLoop("/r", compute=lambda r: PendingState(behind=True, target_sha="def",
                                                            commits=[{"sha": "1"}], ci="green"))
    loop.state = PendingState(behind=True, target_sha="def", commits=[{"sha": "1"}], ci="green")
    ctrl = FakeController()
    app, _, _ = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_python="/v", daemon_enabled=False,
        detect_loop=loop, controller=ctrl)
    return TestClient(app), ctrl


def test_status_exposes_pending_and_deploy(client):
    c, _ = client
    body = c.get("/status").json()
    assert body["pending"]["behind"] is True
    assert body["pending"]["commits"][0]["sha"] == "1"
    assert "running" in body["deploy"]


def test_start_triggers_deploy(client):
    c, ctrl = client
    r = c.post("/start")
    assert r.status_code == 200 and r.json()["started"] is True
    # background task ran
    assert ctrl.started == 1


def test_start_forwards_target_sha_from_body(client):
    c, ctrl = client
    r = c.post("/start", json={"targetSha": "abc1234"})
    assert r.status_code == 200 and r.json()["started"] is True
    assert ctrl.target_shas == ["abc1234"]


def test_start_without_body_passes_no_target(client):
    c, ctrl = client
    assert c.post("/start").status_code == 200
    assert ctrl.target_shas == [None]


def test_start_with_empty_json_body_passes_no_target(client):
    c, ctrl = client
    assert c.post("/start", json={}).status_code == 200
    assert ctrl.target_shas == [None]


def test_start_conflict_when_running(client):
    c, ctrl = client
    ctrl._running = True
    r = c.post("/start")
    assert r.status_code == 409 and r.json()["started"] is False


def test_status_includes_restore(client):
    c, ctrl = client
    ctrl.status.restore = {"restored": ["s1"], "failed": ["s2"]}
    body = c.get("/status").json()
    assert body["deploy"]["restore"] == {"restored": ["s1"], "failed": ["s2"]}

def test_build_controller_app_wires_the_real_runner_and_state_modules():
    """The fan-out and the deploy_state write are the controller's only remaining
    collaborators (per-daemon RPCs moved into server.deploy.runners). The default build
    must wire the real modules and this host's own fleet identity."""
    from server.deploy import runners as runners_mod
    from server.deploy import state as state_mod
    from server.hostname import self_host_name

    _, _, ctrl = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_python="/v", daemon_enabled=True)
    assert ctrl._runners is runners_mod
    assert ctrl._state is state_mod
    assert ctrl._local_name == self_host_name()
    assert ctrl.daemon_enabled is True


def test_lifespan_initialises_the_db_only_when_a_url_is_given(monkeypatch):
    """The controller reads the fleet registry, so the engine must be built on the loop
    uvicorn runs — an asyncpg pool is loop-bound.

    Three cases, because the difference is load-bearing: an explicit url is used as-is; the
    EMPTY string (what serve.py passes when CCSWITCH_DB_URL is unset) still initialises and
    lets init_db fall back to settings.db_url, exactly as `python -m server.deploy --once`
    does; None means "no DB at all" and is for tests only.
    """
    import server.db as db_mod
    calls: list[str | None] = []

    async def fake_init_db(db_url=None):
        calls.append(db_url)
    monkeypatch.setattr(db_mod, "init_db", fake_init_db)

    def _run(**kw):
        app, _, _ = deploy_app.build_controller_app(
            repo="/r", health_url="http://h", venv_python="/v", daemon_enabled=False,
            controller=FakeController(), **kw)
        with TestClient(app):
            pass

    _run(init_db_url="sqlite+aiosqlite:///:memory:")
    assert calls == ["sqlite+aiosqlite:///:memory:"]

    # serve.py's unset-env case: initialise, with the settings fallback.
    _run(init_db_url="")
    assert calls == ["sqlite+aiosqlite:///:memory:", None]

    _run()
    assert calls == ["sqlite+aiosqlite:///:memory:", None], "no url → no DB touched"


def test_default_detect_loop_probes_edge_healthz_for_live_sha():
    """The default DetectLoop must derive liveSha from the edge's /healthz — not the
    deploy checkout's HEAD, which lies whenever a deploy advanced the checkout without
    restarting the units (#859)."""
    _, loop, _ = deploy_app.build_controller_app(
        repo="/r", health_url="http://edge/healthz", venv_python="/v", daemon_enabled=False,
        controller=FakeController())
    comp = loop._compute
    assert isinstance(comp, functools.partial) and comp.func is detect.compute_pending
    probe = comp.keywords["live_sha_fn"]
    assert isinstance(probe, functools.partial) and probe.func is detect.edge_live_sha
    assert probe.args == ("http://edge/healthz",)


def test_poll_interval_default_and_env(monkeypatch):
    monkeypatch.delenv("CCSWITCH_DEPLOY_POLL_INTERVAL", raising=False)
    assert deploy_app._poll_interval() == 30.0
    monkeypatch.setenv("CCSWITCH_DEPLOY_POLL_INTERVAL", "45")
    assert deploy_app._poll_interval() == 45.0
    monkeypatch.setenv("CCSWITCH_DEPLOY_POLL_INTERVAL", "not-a-number")
    assert deploy_app._poll_interval() == 30.0   # bad value falls back to default

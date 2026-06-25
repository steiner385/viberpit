import pytest
from fastapi.testclient import TestClient
from server.deploy import app as deploy_app
from server.deploy.detect import DetectLoop, PendingState
from server.deploy.types import DeployStatus


class FakeController:
    def __init__(self):
        self.status = DeployStatus(target_sha="def", running=False)
        self.started = 0
        self._running = False

    async def deploy_once(self):
        if self._running:
            raise RuntimeError("deploy already in progress")
        self.started += 1
        self.status = DeployStatus(target_sha="def", running=False, phases={"update": "done"})
        return self.status


@pytest.fixture
def client():
    loop = DetectLoop("/r", compute=lambda r: PendingState(behind=True, target_sha="def",
                                                            commits=[{"sha": "1"}], ci="green"))
    loop.state = PendingState(behind=True, target_sha="def", commits=[{"sha": "1"}], ci="green")
    ctrl = FakeController()
    app, _, _ = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_pip="/v", daemon_enabled=False,
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

def test_build_controller_app_wires_daemon_factory_when_enabled():
    """build_controller_app with daemon_enabled=True yields a controller with a non-None
    factory that returns a DaemonClient; daemon_enabled=False yields None factory."""
    from server.ipc.client import DaemonClient
    from server.deploy import app as deploy_app

    _, _, ctrl_enabled = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_pip="/v", daemon_enabled=True)
    assert ctrl_enabled._client_factory is not None
    # The factory must be callable and return a DaemonClient instance.
    client = ctrl_enabled._client_factory()
    assert isinstance(client, DaemonClient)

    _, _, ctrl_disabled = deploy_app.build_controller_app(
        repo="/r", health_url="http://h", venv_pip="/v", daemon_enabled=False)
    assert ctrl_disabled._client_factory is None


def test_poll_interval_default_and_env(monkeypatch):
    monkeypatch.delenv("CCSWITCH_DEPLOY_POLL_INTERVAL", raising=False)
    assert deploy_app._poll_interval() == 30.0
    monkeypatch.setenv("CCSWITCH_DEPLOY_POLL_INTERVAL", "45")
    assert deploy_app._poll_interval() == 45.0
    monkeypatch.setenv("CCSWITCH_DEPLOY_POLL_INTERVAL", "not-a-number")
    assert deploy_app._poll_interval() == 30.0   # bad value falls back to default

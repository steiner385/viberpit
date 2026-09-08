from server.deploy import __main__ as cli
from server.deploy.types import DeployStatus


def test_once_returns_0_on_success(monkeypatch):
    async def fake_deploy(self):
        self.status = DeployStatus(target_sha="abc", running=False, error=None)
        return self.status
    monkeypatch.setattr("server.deploy.controller.DeployController.deploy_once", fake_deploy)
    assert cli.main(["--once", "--repo", "/tmp", "--daemon-enabled", "false"]) == 0


def test_once_returns_1_on_error(monkeypatch):
    async def fake_deploy(self):
        self.status = DeployStatus(target_sha="abc", running=False, error="restart: boom")
        return self.status
    monkeypatch.setattr("server.deploy.controller.DeployController.deploy_once", fake_deploy)
    assert cli.main(["--once", "--repo", "/tmp", "--daemon-enabled", "false"]) == 1


def test_deploy_socket_default(monkeypatch):
    from server import hostpaths
    from server.deploy import serve
    monkeypatch.delenv("CCSWITCH_DEPLOY_SOCKET", raising=False)
    assert serve.deploy_socket() == hostpaths.DEPLOY_SOCKET


def test_deploy_socket_env_override(monkeypatch):
    from server.deploy import serve
    monkeypatch.setenv("CCSWITCH_DEPLOY_SOCKET", "/tmp/x.sock")
    assert serve.deploy_socket() == "/tmp/x.sock"


def test_serve_subcommand_calls_serve(monkeypatch):
    from server.deploy import __main__ as cli
    called = {}
    monkeypatch.setattr("server.deploy.serve.serve", lambda socket_path=None: called.setdefault("ran", True))
    assert cli.main(["serve"]) == 0 and called.get("ran") is True

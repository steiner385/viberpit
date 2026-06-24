import asyncio
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

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from server.api.deploy import router, get_deploy_client
from server.auth.middleware import current_identity
from server.deploy.client import DeployControllerUnavailable


class FakeClient:
    def __init__(self, status_body=None, start_ret=(200, {"started": True}), unavailable=False):
        self._status = status_body or {"pending": {"behind": True}, "deploy": {"running": False}}
        self._start = start_ret
        self._unavailable = unavailable

    async def get_status(self):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        return self._status

    async def start(self):
        if self._unavailable:
            raise DeployControllerUnavailable("x")
        return self._start


def make_app(client):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: "tester"
    app.dependency_overrides[get_deploy_client] = lambda: client
    return TestClient(app)


def test_status_proxies_controller():
    c = make_app(FakeClient())
    body = c.get("/api/deploy/status").json()
    assert body["pending"]["behind"] is True


def test_status_soft_degrades_when_unavailable():
    c = make_app(FakeClient(unavailable=True))
    r = c.get("/api/deploy/status")
    assert r.status_code == 200 and r.json()["deploy"]["error"] == "controller unavailable"


def test_start_proxies_status_code():
    c = make_app(FakeClient(start_ret=(409, {"started": False, "reason": "in progress"})))
    r = c.post("/api/deploy/start")
    assert r.status_code == 409 and r.json()["started"] is False


def test_start_unavailable_is_503():
    c = make_app(FakeClient(unavailable=True))
    assert c.post("/api/deploy/start").status_code == 503

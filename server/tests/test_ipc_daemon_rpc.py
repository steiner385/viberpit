from fastapi.testclient import TestClient
from server.ipc.daemon import create_daemon_app, register_rpc


def test_ping_rpc():
    app = create_daemon_app()
    with TestClient(app) as c:
        r = c.post("/rpc/ping", json={})
    assert r.status_code == 200
    assert r.json() == {"result": {"pong": True}}


def test_unknown_method_returns_error_envelope():
    app = create_daemon_app()
    with TestClient(app) as c:
        r = c.post("/rpc/nope", json={})
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "UnknownMethod"


def test_handler_exception_returns_error_envelope():
    app = create_daemon_app()

    async def boom(params):
        raise ValueError("kaboom")

    register_rpc(app, "boom", boom)
    with TestClient(app) as c:
        r = c.post("/rpc/boom", json={})
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["type"] == "ValueError"
    assert "kaboom" in body["error"]["message"]


def test_registered_handler_receives_params():
    app = create_daemon_app()

    async def add(params):
        return {"sum": params["a"] + params["b"]}

    register_rpc(app, "add", add)
    with TestClient(app) as c:
        r = c.post("/rpc/add", json={"a": 2, "b": 3})
    assert r.json() == {"result": {"sum": 5}}

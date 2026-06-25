import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.obs.context import request_id_var
from server.obs.middleware import RequestContextMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping():
        return {"rid": request_id_var.get()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return app


def test_sets_request_id_header_and_contextvar():
    client = TestClient(_app())
    r = client.get("/ping")
    assert r.status_code == 200
    assert r.headers["X-Request-ID"]
    assert r.json()["rid"] == r.headers["X-Request-ID"]


def test_contextvar_reset_between_requests():
    client = TestClient(_app())
    client.get("/ping")
    assert request_id_var.get() is None  # reset in finally


def test_emits_http_request_breadcrumb(caplog):
    client = TestClient(_app())
    with caplog.at_level(logging.INFO):
        client.get("/ping")
    evts = [r for r in caplog.records if getattr(r, "event", None) == "http.request"]
    assert evts and evts[-1].fields["status"] == 200
    assert evts[-1].fields["path"] == "/ping"

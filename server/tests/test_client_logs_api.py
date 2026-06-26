import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.client_logs import router


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CCSWITCH_DEV", "1")
    # Reset the shared per-identity rate-limiter so each test starts with a full
    # bucket — isolation is structural, not dependent on intra-file test ordering.
    from server.api.ratelimit import _client_logs_limiter
    _client_logs_limiter._buckets.clear()
    app = FastAPI()
    # Override auth dependency to a fixed identity for the test.
    from server.auth.middleware import current_identity
    app.dependency_overrides[current_identity] = lambda: "tester@example.com"
    app.include_router(router)
    return TestClient(app)


def test_ingest_logs_to_pipeline(client, caplog):
    batch = {"entries": [
        {"level": "error", "msg": "render crash", "sessionId": "s9", "stack": "at X"},
        {"level": "warn", "msg": "ws closed 1006"},
    ]}
    with caplog.at_level(logging.WARNING):
        r = client.post("/api/client-logs", json=batch)
    assert r.status_code == 202
    fe = [rec for rec in caplog.records if getattr(rec, "source", None) == "frontend"]
    assert len(fe) == 2
    assert any(getattr(rec, "session_id", None) == "s9" for rec in fe)


def test_redaction_applies_to_client_logs(client, caplog):
    batch = {"entries": [{"level": "error", "msg": "leak sk-ant-api03-XYZSECRET"}]}
    with caplog.at_level(logging.ERROR):
        client.post("/api/client-logs", json=batch)
    # The record message is redacted by the RedactingFilter on emit.
    rec = [r for r in caplog.records if getattr(r, "source", None) == "frontend"][-1]
    # Filters run on emit to handlers; assert formatter output is clean:
    from server.obs.jsonlog import JSONFormatter, ContextFilter
    ContextFilter().filter(rec)
    assert "sk-ant" not in JSONFormatter().format(rec)


def test_batch_size_cap(client):
    big = {"entries": [{"level": "error", "msg": "x"} for _ in range(1000)]}
    r = client.post("/api/client-logs", json=big)
    assert r.status_code == 413


def test_requires_auth():
    app = FastAPI()
    app.include_router(router)  # no dependency override → real auth
    r = TestClient(app).post("/api/client-logs", json={"entries": []})
    assert r.status_code in (401, 403)


def test_rate_limit_returns_429_with_retry_after(client):
    # The `client` fixture starts with a fresh bucket (capacity 60), so the 61st
    # request in a burst is the first to be limited.
    body = {"entries": [{"level": "error", "msg": "x"}]}
    last = None
    for _ in range(61):
        last = client.post("/api/client-logs", json=body)
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1


def test_within_limit_still_accepted(client):
    r = client.post("/api/client-logs", json={"entries": [{"level": "error", "msg": "x"}]})
    assert r.status_code == 202

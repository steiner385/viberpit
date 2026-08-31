"""MCP auth routes — unit tests using FakeMgr on app.state."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api import library
from server.mcp_auth import McpAuthBusy, McpAuthState


class FakeMgr:
    def __init__(self) -> None:
        self.calls: list = []

    async def start(self, server: str) -> McpAuthState:
        if server == "busy":
            raise McpAuthBusy("other")
        self.calls.append(("start", server))
        return McpAuthState(state="starting", server=server)

    def status(self, server: str) -> McpAuthState:
        return McpAuthState(state="awaiting_redirect", server=server, auth_url="https://x")

    async def submit_redirect(self, server: str, url: str) -> McpAuthState:
        self.calls.append(("redirect", server, url))
        return McpAuthState(state="success", server=server, accounts=["a"])

    async def cancel(self, server: str) -> McpAuthState:
        self.calls.append(("cancel", server))
        return McpAuthState(state="cancelled", server=server)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(library, "valid_mcp_name", lambda n: True)
    app = FastAPI()
    app.include_router(library.router)
    app.state.mcp_auth = FakeMgr()
    from server.auth.middleware import current_identity
    from server.db import get_db

    app.dependency_overrides[current_identity] = lambda: "tester"
    # Provide a no-op DB session so record_audit doesn't blow up.
    # Use an AsyncMock-compatible object: the audit call does `await db.execute()`
    # and `await db.commit()` — stub them out.
    class _FakeDB:
        def add(self, *a, **kw) -> None:  # noqa: ANN001
            return None

        async def execute(self, *a, **kw):  # noqa: ANN001
            return None

        async def commit(self, *a, **kw):  # noqa: ANN001
            return None

    app.dependency_overrides[get_db] = lambda: _FakeDB()
    return TestClient(app)


def test_start_returns_202(client: TestClient) -> None:
    r = client.post("/api/library/mcp/notion/auth")
    assert r.status_code == 202
    assert r.json()["server"] == "notion"


def test_start_busy_returns_409(client: TestClient) -> None:
    assert client.post("/api/library/mcp/busy/auth").status_code == 409


def test_status_scoped(client: TestClient) -> None:
    r = client.get("/api/library/mcp/notion/auth")
    assert r.json()["authUrl"] == "https://x"


def test_redirect_submits(client: TestClient) -> None:
    r = client.post(
        "/api/library/mcp/notion/auth/redirect",
        json={"redirectUrl": "http://localhost/cb?code=1"},
    )
    assert r.json()["state"] == "success"
    assert r.json()["accounts"] == ["a"]


def test_cancel(client: TestClient) -> None:
    assert client.post("/api/library/mcp/notion/auth/cancel").json()["state"] == "cancelled"

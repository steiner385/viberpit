"""The operational smoke must not mutate an edge with unsafe archive retention."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "smoke_assistant", Path(__file__).resolve().parents[2] / "scripts/smoke_assistant.py"
)
assert _SPEC and _SPEC.loader
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@pytest.mark.asyncio
async def test_requires_disposable_environment_acknowledgement(monkeypatch):
    monkeypatch.setattr("sys.argv", ["smoke_assistant.py", "--db-url", "unused"])
    client = AsyncMock()
    monkeypatch.setattr(smoke.httpx, "AsyncClient", client)
    assert await smoke.main() == 2
    client.assert_not_called()


UNSAFE: list[object] = [
    {}, [], None, {"maxArchivedSessions": 100}, {"maxArchivedSessions": 0},
    {"maxArchivedSessions": False}, {"maxArchivedSessions": "null"},
    {"maxArchivedSessions": []}, "invalid-json", "http-error", "read-error",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", UNSAFE)
async def test_unsafe_preflight_never_creates_or_resumes(monkeypatch, settings):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path != "/api/settings":
            return httpx.Response(500)
        return settings_response(request, settings)

    client = httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("sys.argv", [
        "smoke_assistant.py", "--db-url", "unused", "--disposable-environment",
    ])
    assert await smoke.main() == 1
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/api/settings")]


def settings_response(request, settings):
    if settings == "read-error":
        raise httpx.ConnectError("unavailable", request=request)
    if settings == "http-error":
        return httpx.Response(503)
    if settings == "invalid-json":
        return httpx.Response(200, text="not JSON")
    return httpx.Response(200, json=settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_settings", [*UNSAFE, {"maxArchivedSessions": None}])
async def test_cleanup_rechecks_retention(monkeypatch, cleanup_settings, capsys):
    requests = []
    settings_reads = 0

    def handle(request):
        nonlocal settings_reads
        requests.append(request)
        if request.url.path == "/api/settings":
            settings_reads += 1
            settings = {"maxArchivedSessions": None} if settings_reads == 1 else cleanup_settings
            return settings_response(request, settings)
        if request.method == "POST":
            return httpx.Response(201, json={"id": "owned-test", "state": "created"})
        if request.method == "GET":
            return httpx.Response(200, json={"status": "idle"})
        return httpx.Response(204)

    client = httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke.httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr("sys.argv", [
        "smoke_assistant.py", "--db-url", "unused", "--disposable-environment",
    ])
    socket = AsyncMock()
    monkeypatch.setattr(smoke.websockets, "connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr(smoke, "_drain_until", AsyncMock(return_value=[{
        "type": "permissionRequest", "alwaysAllowed": True, "permId": "p",
    }]))
    monkeypatch.setattr(smoke, "_read_tool_call_audit", AsyncMock(return_value=True))
    safe = cleanup_settings == {"maxArchivedSessions": None}
    assert await smoke.main() == (0 if safe else 1)
    assert settings_reads == 2
    deletes = [r for r in requests if r.method == "DELETE"]
    assert len(deletes) == (1 if safe else 0)
    if not safe:
        assert "left intact" in capsys.readouterr().out

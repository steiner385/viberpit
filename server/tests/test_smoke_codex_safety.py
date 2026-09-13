"""Operational Codex smoke safety, using only offline HTTP and socket doubles."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts import smoke_codex_interactive as smoke

UNSAFE = [
    {}, [], None, {"maxArchivedSessions": 100}, {"maxArchivedSessions": 0},
    {"maxArchivedSessions": False}, {"maxArchivedSessions": "null"},
    {"maxArchivedSessions": []}, "invalid-json", "http-error", "read-error",
]
SAFE = {"maxArchivedSessions": None}


def settings_response(request, value):
    if value == "read-error":
        raise httpx.ConnectError("unavailable", request=request)
    if value == "http-error":
        return httpx.Response(503)
    if value == "invalid-json":
        return httpx.Response(200, text="not JSON")
    return httpx.Response(200, json=value)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("CCSWITCH_TEST_CODEX_INTERACTIVE", "1")
    monkeypatch.setattr("sys.argv", [
        "smoke_codex_interactive.py", "--account", "test", "--repo", "test/disposable",
        "--runner-home-base", str(tmp_path), "--disposable-environment",
    ])
    monkeypatch.setattr(smoke, "_doctor", lambda _: (True, "offline test"))
    monkeypatch.setattr(smoke.asyncio, "sleep", AsyncMock())
    socket = AsyncMock()
    monkeypatch.setattr(smoke.websockets, "connect", lambda *a, **kw: socket)
    monkeypatch.setattr(smoke, "_drain_until", AsyncMock(return_value=[
        {"type": "text", "text": "READY STEERED"}, {"type": "turnEnd"},
    ]))


@pytest.mark.asyncio
async def test_explicit_disposable_acknowledgement_precedes_all_work(harness, monkeypatch):
    monkeypatch.setattr("sys.argv", sys.argv[:-1])
    doctor = AsyncMock()
    client = AsyncMock()
    monkeypatch.setattr(smoke, "_doctor", doctor)
    monkeypatch.setattr(smoke.httpx, "AsyncClient", client)
    assert await smoke.main() == 2
    doctor.assert_not_called()
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", UNSAFE)
async def test_unsafe_preflight_prevents_all_account_and_session_requests(harness, monkeypatch, settings):
    requests = []

    def handle(request):
        requests.append((request.method, request.url.path))
        return settings_response(request, settings)

    client = httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke.httpx, "AsyncClient", lambda **kw: client)
    assert await smoke.main() == 1
    assert requests == [("GET", "/api/settings")]


@pytest.mark.asyncio
@pytest.mark.parametrize("early_failure", [None, "second-create", "turn-timeout"])
@pytest.mark.parametrize("cleanup_settings", [*UNSAFE, SAFE])
async def test_normal_and_early_cleanup_fail_closed(harness, monkeypatch, capsys,
                                                  early_failure, cleanup_settings):
    requests = []
    settings_reads = 0
    creates = 0
    if early_failure == "turn-timeout":
        monkeypatch.setattr(smoke, "_drain_until", AsyncMock(side_effect=[
            [{"type": "replayComplete"}], TimeoutError("offline timeout"),
        ]))

    def handle(request):
        nonlocal settings_reads, creates
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/settings":
            settings_reads += 1
            return settings_response(request, SAFE if settings_reads == 1 else cleanup_settings)
        if request.url.path == "/api/accounts":
            return httpx.Response(200, json=[{
                "name": "test", "provider": "openai", "usage": {"fetchedAt": "now"},
            }])
        if request.url.path == "/api/models":
            return httpx.Response(200, json=[{"id": "test-model"}])
        if request.url.path == "/api/sessions" and request.method == "POST":
            creates += 1
            if early_failure == "second-create" and creates == 2:
                return httpx.Response(503)
            return httpx.Response(201, json={"id": f"owned-{creates}"})
        if request.method == "GET":
            return httpx.Response(200, json={"engine": "codex", "reasoningEffort": "low"})
        return httpx.Response(204)

    client = httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(smoke.httpx, "AsyncClient", lambda **kw: client)
    safe = cleanup_settings == SAFE
    assert await smoke.main() == (0 if safe and not early_failure else 1)
    expected_archives = (1 if early_failure == "second-create" else 2) if safe else 0
    assert [path for method, path in requests if method == "DELETE"] == [
        f"/api/sessions/owned-{i}" for i in range(1, expected_archives + 1)
    ]
    assert not any(path.endswith("/archive") for _, path in requests)
    assert settings_reads == 1 + (expected_archives if safe else 1)
    if not safe:
        assert "left intact" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rechecks_between_owned_archives(monkeypatch, capsys):
    requests = []
    settings_reads = 0

    def handle(request):
        nonlocal settings_reads
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            settings_reads += 1
            return settings_response(request, SAFE if settings_reads == 1 else {"maxArchivedSessions": 1})
        return httpx.Response(204)

    steps = []
    async with httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle)) as client:
        await smoke._cleanup(client, ["owned-1", "owned-2"], steps)
    assert requests == [("GET", "/api/settings"), ("DELETE", "/api/sessions/owned-1"),
                        ("GET", "/api/settings")]
    assert smoke._summarize(steps) == 1
    assert "owned-2" in capsys.readouterr().out


@pytest.mark.parametrize("script", ["smoke_assistant.py", "smoke_codex_interactive.py"])
def test_direct_script_entrypoint_imports_shared_helper(script):
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run([sys.executable, str(root / "scripts" / script), "--help"],
                            cwd="/tmp", capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--disposable-environment" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http-error", "read-error"])
async def test_archive_failure_is_not_reported_as_success(failure):
    def handle(request):
        if request.method == "GET":
            return settings_response(request, SAFE)
        return settings_response(request, failure)

    steps = []
    async with httpx.AsyncClient(base_url="http://isolated.test", transport=httpx.MockTransport(handle)) as client:
        await smoke._cleanup(client, ["owned-1"], steps)
    assert smoke._summarize(steps) == 1

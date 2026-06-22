"""Health endpoint tests — identity now required (T018)."""
import os
from unittest import mock

import pytest
from httpx import AsyncClient, ASGITransport
from server.app import create_app, running_git_sha


@pytest.mark.asyncio
async def test_health_returns_200_with_identity():
    """Health endpoint returns 200 when a valid Tailscale identity header is present."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/health",
            headers={"Tailscale-User-Login": "testuser@example.com"},
        )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True


@pytest.mark.asyncio
async def test_health_returns_401_without_identity():
    """Health endpoint returns 401 when no identity header is present."""
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 401


def test_running_git_sha_prefers_env_override():
    """CCSWITCH_GIT_SHA wins, so the deployer can inject the exact deployed commit
    and assert it via /healthz. (Cache cleared since the value is resolved once.)"""
    running_git_sha.cache_clear()
    try:
        with mock.patch.dict(os.environ, {"CCSWITCH_GIT_SHA": "deadbeef"}):
            assert running_git_sha() == "deadbeef"
    finally:
        running_git_sha.cache_clear()


def test_running_git_sha_default_is_nonempty():
    """Without an override, returns the real short SHA or 'unknown' — never empty,
    so the /healthz body always carries a usable marker."""
    running_git_sha.cache_clear()
    try:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CCSWITCH_GIT_SHA", None)
            sha = running_git_sha()
        assert isinstance(sha, str) and sha
    finally:
        running_git_sha.cache_clear()

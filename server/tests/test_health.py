"""Health endpoint tests — identity now required (T018)."""
import pytest
from httpx import AsyncClient, ASGITransport
from server.app import create_app


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

import pytest
from httpx import AsyncClient, ASGITransport
from server.app import create_app


@pytest.mark.asyncio
async def test_health_returns_200():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

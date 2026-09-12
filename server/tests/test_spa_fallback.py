"""SPA fallback: clean-path deep links serve index.html; assets and /api still work."""
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.spa import mount_spa


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>viberpit</title>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('hi')")
    return dist


@pytest.mark.asyncio
async def test_spa_serves_index_for_client_route(tmp_path):
    app = FastAPI()
    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/session/abc123")
    assert resp.status_code == 200
    assert "viberpit" in resp.text


@pytest.mark.asyncio
async def test_spa_serves_real_asset(tmp_path):
    app = FastAPI()
    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/assets/app.js")
    assert resp.status_code == 200
    assert "console.log" in resp.text


@pytest.mark.asyncio
async def test_api_routes_take_precedence_over_spa(tmp_path):
    app = FastAPI()

    @app.get("/api/ping")
    async def ping():
        return {"pong": True}

    mount_spa(app, _make_dist(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": True}


@pytest.mark.asyncio
async def test_mount_spa_noop_when_dist_missing(tmp_path):
    app = FastAPI()
    mount_spa(app, tmp_path / "nonexistent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/session/abc")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_webmanifest_is_served_as_manifest_json(tmp_path):
    """Python's mimetypes has no .webmanifest entry, so without an explicit
    registration StaticFiles serves it as text/plain and Chrome rejects it."""
    dist = _make_dist(tmp_path)
    (dist / "manifest.webmanifest").write_text('{"name": "ViberPit"}')
    app = FastAPI()
    mount_spa(app, dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")

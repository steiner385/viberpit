"""Route tests: GET/PUT /api/pipeline/deploy-config (SPEC §3 env config).

Auth mirrors test_api_pipeline_repos.py: create_app() with CCSWITCH_DEV=1 and
an empty allowlist so any identity passes; Tailscale-User-Login header
authenticates. A real orchestrator is used (methods hit the DB directly), with a
temp-file DB so the seed connection and the request connection share state.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}


@pytest.fixture
def _db_env(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    env = {
        "CCSWITCH_DB_URL": url,
        "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "",
    }
    with patch.dict(os.environ, env):
        # Fresh per-test DB under tmp_path — explicit url, no wiping (#728).
        asyncio.run(init_db(url))
        yield env


def _make_client(env):
    with patch.dict(os.environ, env):
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=lambda name: None,
        )
        app = create_app(orchestrator=orch)
    return TestClient(app, raise_server_exceptions=False)


def test_deploy_config_requires_identity(_db_env):
    client = _make_client(_db_env)
    with patch.dict(os.environ, _db_env), client:
        assert client.get("/api/pipeline/deploy-config").status_code == 401


def test_deploy_config_put_then_get_roundtrip(_db_env):
    client = _make_client(_db_env)
    body = {
        "repo": "o/r",
        "environments": [
            {"name": "qa", "healthUrl": "https://qa.example/health", "auto": True},
            {"name": "prod", "healthUrl": "https://p.example/health", "auto": False,
             "shaKey": "gitSha"},
        ],
        "enabled": True,
    }
    with patch.dict(os.environ, _db_env), client:
        put = client.put("/api/pipeline/deploy-config", json=body, headers=_H)
        assert put.status_code == 200, put.text
        got = client.get("/api/pipeline/deploy-config", headers=_H)
    assert got.status_code == 200
    data = got.json()
    assert "o/r" in data
    assert data["o/r"]["enabled"] is True
    envs = data["o/r"]["environments"]
    assert [e["name"] for e in envs] == ["qa", "prod"]
    assert envs[1]["shaKey"] == "gitSha"


def test_deploy_config_put_upserts(_db_env):
    client = _make_client(_db_env)
    first = {"repo": "o/r", "environments": [
        {"name": "qa", "healthUrl": "https://qa/health", "auto": True}]}
    second = {"repo": "o/r", "environments": [
        {"name": "prod", "healthUrl": "https://p/health", "auto": True}],
        "enabled": False}
    with patch.dict(os.environ, _db_env), client:
        client.put("/api/pipeline/deploy-config", json=first, headers=_H)
        client.put("/api/pipeline/deploy-config", json=second, headers=_H)
        got = client.get("/api/pipeline/deploy-config", headers=_H).json()
    assert got["o/r"]["enabled"] is False
    assert [e["name"] for e in got["o/r"]["environments"]] == ["prod"]


def test_deploy_config_rejects_bad_health_url(_db_env):
    client = _make_client(_db_env)
    body = {"repo": "o/r", "environments": [
        {"name": "qa", "healthUrl": "ftp://qa/health", "auto": True}]}
    with patch.dict(os.environ, _db_env), client:
        r = client.put("/api/pipeline/deploy-config", json=body, headers=_H)
    assert r.status_code == 422


def test_deploy_config_rejects_missing_name(_db_env):
    client = _make_client(_db_env)
    body = {"repo": "o/r", "environments": [
        {"healthUrl": "https://qa/health", "auto": True}]}
    with patch.dict(os.environ, _db_env), client:
        r = client.put("/api/pipeline/deploy-config", json=body, headers=_H)
    assert r.status_code == 422


def test_deploy_config_get_empty(_db_env):
    client = _make_client(_db_env)
    with patch.dict(os.environ, _db_env), client:
        r = client.get("/api/pipeline/deploy-config", headers=_H)
    assert r.status_code == 200
    assert r.json() == {}

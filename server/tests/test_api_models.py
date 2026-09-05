"""GET /api/models — one endpoint, two engines.

The regression this guards is compatibility: every existing caller omits
``engine``, so the Claude path must keep returning exactly what it always did.
The Codex path is additive and account-scoped.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from tests.helpers import temp_env  # noqa: E402

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1"}

_CODEX_MODELS = [
    {
        "id": "gpt-5.1-codex",
        "label": "GPT-5.1 Codex",
        "description": "",
        "default": True,
        "reasoningEfforts": ["low", "medium", "high"],
        "defaultReasoningEffort": "medium",
        "inputModalities": ["text", "image"],
    }
]


class _FakeSessions:
    def __init__(self) -> None:
        self.codex_models = AsyncMock(
            return_value={"state": "ok", "models": _CODEX_MODELS}
        )
        self.usage = AsyncMock(return_value=None)

    def daemon_epoch(self):
        return None


def _make_client() -> tuple[TestClient, _FakeSessions]:
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory

    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions()
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_no_engine_still_returns_the_claude_catalog():
    """Every existing caller omits `engine`; the legacy default must hold."""
    with temp_env():
        client, fake = _make_client()
        with client:
            resp = client.get("/api/models", headers=IDENTITY_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert all("id" in m and "label" in m for m in body)
    fake.codex_models.assert_not_awaited()


def test_explicit_claude_code_engine_matches_the_default():
    with temp_env():
        client, _ = _make_client()
        with client:
            default = client.get("/api/models", headers=IDENTITY_HEADERS).json()
            explicit = client.get(
                "/api/models?engine=claude_code", headers=IDENTITY_HEADERS
            ).json()
    assert default == explicit


def test_codex_engine_returns_the_account_scoped_options():
    with temp_env():
        client, fake = _make_client()
        with client:
            resp = client.get(
                "/api/models?engine=codex&account=chatgpt-pro&refresh=true",
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["id"] == "gpt-5.1-codex"
    assert body[0]["reasoningEfforts"] == ["low", "medium", "high"]
    fake.codex_models.assert_awaited_once_with("chatgpt-pro", refresh=True)


def test_codex_engine_requires_an_account():
    """Codex model lists differ by plan, so 'which account' is not optional."""
    with temp_env():
        client, _ = _make_client()
        with client:
            resp = client.get("/api/models?engine=codex", headers=IDENTITY_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "account_required"


def test_unknown_engine_is_a_400_not_a_500():
    with temp_env():
        client, _ = _make_client()
        with client:
            resp = client.get("/api/models?engine=gpt4all", headers=IDENTITY_HEADERS)
    assert resp.status_code == 400


def test_codex_unavailable_is_an_empty_list_not_an_error():
    """Codex simply not being offered here is not a failure the picker should toast."""
    with temp_env():
        client, fake = _make_client()
        fake.codex_models = AsyncMock(
            return_value={"state": "unavailable", "models": []}
        )
        with client:
            resp = client.get(
                "/api/models?engine=codex&account=chatgpt-pro",
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 200
    assert resp.json() == []


def test_codex_model_list_failure_is_a_502_with_the_reason():
    with temp_env():
        client, fake = _make_client()
        fake.codex_models = AsyncMock(
            return_value={"state": "error", "models": [], "detail": "app-server died"}
        )
        with client:
            resp = client.get(
                "/api/models?engine=codex&account=chatgpt-pro",
                headers=IDENTITY_HEADERS,
            )
    assert resp.status_code == 502
    assert resp.json()["detail"]["error"] == "codex_models_unavailable"


def test_the_codex_wire_shape_is_a_superset_of_the_claude_one():
    """One picker component, one shape. This is what lets that stay true."""
    with temp_env():
        client, _ = _make_client()
        with client:
            claude = client.get("/api/models", headers=IDENTITY_HEADERS).json()
            codex = client.get(
                "/api/models?engine=codex&account=chatgpt-pro",
                headers=IDENTITY_HEADERS,
            ).json()
    if claude:
        assert set(claude[0]) <= set(codex[0])

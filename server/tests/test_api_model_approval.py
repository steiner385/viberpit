"""API tests — POST /api/sessions/{id}/model-approval.

Tests: approve flow → 200 + status not pending_approval; bad decision → 422;
nothing-pending → 409; unknown session → 404.

ModelBlockedError → 409 on create is also covered here.

Mirror of test_api.py harness: inject a real SessionOrchestrator with fake SDK,
drive the gated model path directly on the orchestrator to set up hold state,
then hit the HTTP endpoint.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from ccswitch.selector import Picked
from server.tests.fakes.fake_factory import fake_sdk_factory

IDENTITY_HEADERS = {"Tailscale-User-Login": "testuser@example.com"}


def _seed_account(name: str, tmp_path: Path) -> None:
    from ccswitch import paths
    from ccswitch.creds import write_json_secure

    write_json_secure(
        paths.slot_path(name),
        {
            "accessToken": f"{name}-tok",
            "refreshToken": f"{name}-ref",
            "expiresAt": int(time.time() * 1000) + 9_000_000,
            "subscriptionType": "max",
        },
    )
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot

    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _make_harness(tmp_path: Path, extra_env: dict | None = None):
    """Build env + orchestrator patches for a gated-model test harness.

    Returns a context-manager dict with env, cfg_dir, and an async maker.
    Callers are responsible for entering and exiting the patches.
    """
    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)
    db_path = tmp_path / "api_gate_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    env = {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_MODEL_APPROVAL_ACTION": "require_approval",
        "CCSWITCH_MODEL_APPROVAL_DENYLIST": "fable",
    }
    if extra_env:
        env.update(extra_env)

    return env, cfg_dir, store_home, db_url


# ---------------------------------------------------------------------------
# Harness — the app lifespan OWNS the orchestrator's start/stop.
#
# Pre-starting the orchestrator in a throwaway ``asyncio.run`` loop and then
# handing it to ``create_app`` double-starts it (the lifespan starts it again)
# and leaks aiosqlite worker threads bound to the closed loop — which passes in
# isolation but hangs the full suite at teardown (300s pytest-timeout). Mirror
# test_api.py / test_settings_model_approval: never pre-start, let ``with
# client:`` drive the lifespan, and set up session state over HTTP.
# ---------------------------------------------------------------------------


@contextmanager
def _api(tmp_path, extra_env: dict | None = None):
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator

    env, cfg_dir, store_home, db_url = _make_harness(tmp_path, extra_env)
    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            yield client


def _create_gated(client) -> str:
    """Create a fable (gated) session over HTTP → parked pending_approval; return id."""
    cr = client.post(
        "/api/sessions", json={"name": "gated", "model": "fable"}, headers=IDENTITY_HEADERS)
    assert cr.status_code in (200, 201), cr.text
    body = cr.json()
    assert body["status"] == "pending_approval", body
    assert body["pendingModel"] == "fable", body
    return body["id"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_approve_flow(tmp_path):
    """approve → 200, status leaves pending_approval."""
    with _api(tmp_path) as client:
        sid = _create_gated(client)
        r = client.post(
            f"/api/sessions/{sid}/model-approval",
            json={"decision": "approve"}, headers=IDENTITY_HEADERS)
        body = r.json()
    assert r.status_code == 200, r.text
    assert body["status"] != "pending_approval"
    assert body["id"] == sid


def test_deny_flow(tmp_path):
    """deny → 200, status=ended."""
    with _api(tmp_path) as client:
        sid = _create_gated(client)
        r = client.post(
            f"/api/sessions/{sid}/model-approval",
            json={"decision": "deny"}, headers=IDENTITY_HEADERS)
        body = r.json()
    assert r.status_code == 200, r.text
    assert body["status"] == "ended"


def test_bad_decision_422(tmp_path):
    """Invalid decision value → 422 (Literal validation, before the route body)."""
    with _api(tmp_path) as client:
        sid = _create_gated(client)
        r = client.post(
            f"/api/sessions/{sid}/model-approval",
            json={"decision": "maybe"}, headers=IDENTITY_HEADERS)
    assert r.status_code == 422


def test_nothing_pending_409(tmp_path):
    """Approving a session with no pending approval → 409."""
    with _api(tmp_path) as client:
        # A non-gated (account-default) session spawns running, no pending hold.
        cr = client.post(
            "/api/sessions", json={"name": "running"}, headers=IDENTITY_HEADERS)
        assert cr.status_code in (200, 201), cr.text
        assert cr.json()["status"] != "pending_approval"
        sid = cr.json()["id"]
        r = client.post(
            f"/api/sessions/{sid}/model-approval",
            json={"decision": "approve"}, headers=IDENTITY_HEADERS)
    assert r.status_code == 409


def test_unknown_session_404(tmp_path):
    """Approving an unknown session id → 404."""
    with _api(tmp_path) as client:
        r = client.post(
            "/api/sessions/does-not-exist/model-approval",
            json={"decision": "approve"}, headers=IDENTITY_HEADERS)
    assert r.status_code == 404


def test_create_session_blocked_returns_409(tmp_path):
    """POST /api/sessions with a blocked model → 409 (ModelBlockedError→409)."""
    with _api(tmp_path, extra_env={"CCSWITCH_MODEL_APPROVAL_ACTION": "block"}) as client:
        r = client.post(
            "/api/sessions", json={"name": "blocked", "model": "fable"},
            headers=IDENTITY_HEADERS)
    assert r.status_code == 409
    assert "blocked" in r.json().get("detail", "").lower()

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
    from ccswitch.oauth import Usage

    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


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
# Tests
# ---------------------------------------------------------------------------


def test_approve_flow(tmp_path):
    """POST /model-approval?decision=approve → 200, status != pending_approval."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env, cfg_dir, store_home, db_url = _make_harness(tmp_path)

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())
        rec = asyncio.run(orch.create_session(name="gated", account=None, model="fable"))

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                f"/api/sessions/{rec.id}/model-approval",
                json={"decision": "approve"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] != "pending_approval"
    assert body["id"] == rec.id


def test_deny_flow(tmp_path):
    """POST /model-approval?decision=deny → 200, status=ended."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env, cfg_dir, store_home, db_url = _make_harness(tmp_path)

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())
        rec = asyncio.run(orch.create_session(name="gated", account=None, model="fable"))

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                f"/api/sessions/{rec.id}/model-approval",
                json={"decision": "deny"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ended"


def test_bad_decision_422(tmp_path):
    """Invalid decision value → 422 (Literal validation)."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env, cfg_dir, store_home, db_url = _make_harness(tmp_path)

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())
        rec = asyncio.run(orch.create_session(name="gated", account=None, model="fable"))

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                f"/api/sessions/{rec.id}/model-approval",
                json={"decision": "maybe"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 422


def test_nothing_pending_409(tmp_path):
    """Approving a session that has no pending approval → 409."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    store_home = tmp_path / "store"
    store_home.mkdir()
    shared_base = tmp_path / "dot-claude"
    shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True)
    db_path = tmp_path / "nothing_pending.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    env = {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
    }

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())
        # Create a NORMAL (non-gated) session — no pending approval
        running_rec = asyncio.run(orch.create_session(name="running", account=None, model=None))

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                f"/api/sessions/{running_rec.id}/model-approval",
                json={"decision": "approve"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 409


def test_unknown_session_404(tmp_path):
    """Approving an unknown session id → 404."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env, cfg_dir, store_home, db_url = _make_harness(tmp_path)

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                "/api/sessions/does-not-exist/model-approval",
                json={"decision": "approve"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 404


def test_create_session_blocked_returns_409(tmp_path):
    """POST /api/sessions with a blocked model → 409 (ModelBlockedError→409)."""
    import asyncio
    from server.db import init_db
    from server.sessions.orchestrator import SessionOrchestrator
    from server.app import create_app

    env, cfg_dir, store_home, db_url = _make_harness(
        tmp_path, extra_env={"CCSWITCH_MODEL_APPROVAL_ACTION": "block"}
    )

    with patch.dict(os.environ, env, clear=False), \
         patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")), \
         patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir.trust_project"):
        _seed_account("alpha", store_home)
        asyncio.run(init_db(db_url=db_url))
        orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(),
            poll_interval=9999.0,
            usage_fn=_fake_usage,
            repo_root=str(tmp_path / "repos"),
        )
        asyncio.run(orch.start())

        app = create_app(orchestrator=orch)
        client = TestClient(app, raise_server_exceptions=False)
        with client:
            r = client.post(
                "/api/sessions",
                json={"name": "blocked", "model": "fable"},
                headers=IDENTITY_HEADERS,
            )

    assert r.status_code == 409
    assert "blocked" in r.json().get("detail", "").lower()

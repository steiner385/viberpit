"""POST /api/assistant/session · GET /api/assistant/status (spec §4.1, sub-spec B §7)."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from ccswitch.selector import Picked
from server.app import create_app
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "u@example.com"}
_ENV = {"CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:", "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "", "CCSWITCH_SESSION_SECRET": "x" * 32,
        "CCSWITCH_ASSISTANT_ENABLED": "true"}


def _seed_account(name: str) -> None:
    """Mirrors test_assistant_orchestrator.py's helper: a real cred slot so
    ``select_account`` (unpatched paths in ``_isolated_dir_for``) has something
    to isolate."""
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


@contextmanager
def _client(tmp_path, env=None):
    # The env patch must stay live for the whole request lifecycle, not just app
    # construction: assistant_enabled()/resolve_model_policy() re-read
    # get_settings() (env) on every request, and the TestClient lifespan
    # (init_db) itself runs lazily on `__enter__`, after this function would
    # otherwise have already returned and unwound the patch.
    shared = tmp_path / "dot-claude"
    shared.mkdir(exist_ok=True)
    (shared / "settings.json").write_text("{}")
    (shared / "projects").mkdir(exist_ok=True)
    cfg_dir = tmp_path / "cfg" / "alpha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / ".claude.json").write_text("{}")
    full_env = {**_ENV, "CCSWITCH_HOME": str(tmp_path / "store"),
                "CLAUDE_CONFIG_DIR": str(shared), **(env or {})}
    with patch.dict(os.environ, full_env):
        _seed_account("alpha")
        # create_session's account/compose/isolation plumbing is exercised for
        # real (see test_assistant_orchestrator.py); only account SELECTION
        # (which would otherwise hit this host's real, possibly-exhausted
        # account ledger) and cred-store composition are faked out.
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir.trust_project"), \
             patch("server.sessions.orchestrator.select_account",
                   return_value=Picked("alpha")):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)
            app = create_app(orchestrator=orch)
            with TestClient(app) as c:
                yield c


def test_disabled_is_404(tmp_path):
    with _client(tmp_path, {"CCSWITCH_ASSISTANT_ENABLED": "false"}) as c:
        assert c.get("/api/assistant/status", headers=_H).json()["enabled"] is False
        r = c.post("/api/assistant/session", json={}, headers=_H)
        assert r.status_code == 404 and r.json()["error"]["code"] == "ASSISTANT_DISABLED"


def test_create_is_idempotent_per_identity_and_hidden(tmp_path):
    with _client(tmp_path) as c:
        r1 = c.post("/api/assistant/session", json={}, headers=_H)
        assert r1.status_code == 201 and r1.json()["state"] == "created"
        r2 = c.post("/api/assistant/session", json={}, headers=_H)
        assert r2.json()["id"] == r1.json()["id"] and r2.json()["state"] == "existing"
        other = c.post("/api/assistant/session", json={},
                       headers={"Tailscale-User-Login": "v@example.com"})
        assert other.json()["id"] != r1.json()["id"]
        ids = {s["id"] for s in c.get("/api/sessions", headers=_H).json()}
        assert r1.json()["id"] not in ids
        assert c.get(f"/api/sessions/{r1.json()['id']}", headers=_H).json()["kind"] == "assistant"


def test_fresh_archives_and_recreates(tmp_path):
    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        second = c.post("/api/assistant/session", json={"fresh": True}, headers=_H).json()
        assert second["state"] == "created" and second["id"] != first
        assert c.get("/api/assistant/status", headers=_H).json()["sessionId"] == second["id"]


async def _end_row(session_id: str) -> None:
    """Park the row exactly as the idle reap does (spec §4.7: status=ended, driver
    closed, CLI transcript kept)."""
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == session_id)
                         .values(status="ended"))
        await db.commit()


def test_reaped_thread_is_reused_after_a_daemon_restart(tmp_path):
    """#1122: reap (status=ended) + restart used to lose the thread — `rehydrate`
    skips ended rows, so the drawer created a new assistant and orphaned the old
    transcript. The endpoint must hand back the SAME session, resumed."""
    with _client(tmp_path) as c:
        first = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        # app.state.sessions is the facade; the records live on the real orchestrator.
        orch = c.app.state.sessions._orch
        c.portal.call(_end_row, first)
        orch._records.clear()          # daemon restart: nothing in memory…
        orch._drivers.clear()
        c.portal.call(orch.rehydrate)  # …and rehydrate does not bring an ended row back
        assert first not in orch._records

        again = c.post("/api/assistant/session", json={}, headers=_H).json()
        assert again["id"] == first and again["state"] == "resumed"
        assert c.get("/api/assistant/status", headers=_H).json()["sessionId"] == first


def test_resume_threads_the_callers_identity(tmp_path):
    """The resume path must build the driver under the REAL identity.

    ensure_driver defaults to SYSTEM_IDENTITY, and a driver built under "system"
    registers its questions/permissions as owned by "system" — every answer the
    real user then sends is rejected ("question no longer pending", #1080). The WS
    attach re-threads identity moments later, so the window is narrow, but anything
    the resumed driver asks before the drawer attaches is unanswerable."""
    from unittest.mock import AsyncMock

    with _client(tmp_path) as c:
        sid = c.post("/api/assistant/session", json={}, headers=_H).json()["id"]
        orch = c.app.state.sessions._orch
        orch._records[sid].status = "ended"
        with patch.object(SessionOrchestrator, "ensure_driver", new=AsyncMock()) as ed:
            r = c.post("/api/assistant/session", json={}, headers=_H)
        assert r.json()["state"] == "resumed"
        assert ed.await_args.kwargs.get("identity") == "u@example.com"


def test_gated_model_is_400(tmp_path):
    with _client(tmp_path, {"CCSWITCH_MODEL_APPROVAL_DENYLIST": "fable"}) as c:
        r = c.post("/api/assistant/session", json={"model": "fable"}, headers=_H)
        assert r.status_code == 400 and r.json()["error"]["code"] == "MODEL_GATED"

"""Manual dispatch: backlog_runner_dispatch + /candidates + /dispatch API."""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

H = {"Tailscale-User-Login": "testuser@example.com"}

# ---------------------------------------------------------------------------
# Unit tests for SessionOrchestrator.backlog_runner_dispatch
# ---------------------------------------------------------------------------

@pytest.fixture()
def dispatch_orch(tmp_path):
    """SessionOrchestrator with mocked backlog seams — no network, no LLM."""
    db_url = f"sqlite+aiosqlite:///{tmp_path}/test.db"
    with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                  "CCSWITCH_TAILNET_ALLOWLIST": ""}):
        from server.sessions.orchestrator import SessionOrchestrator
        from server.tests.fakes.fake_factory import fake_sdk_factory
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        # Patch the seams used by backlog_runner_dispatch
        orch._backlog_read_config = AsyncMock(return_value=(
            "auto",
            {"enabled_repos": ["o/r"], "eligible_label": "ready",
             "max_concurrent": 1, "max_spawns_per_day": 10,
             "max_issues_per_pr": 3, "default_model": None},
        ))
        orch._backlog_claim = AsyncMock(return_value=True)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        rec = MagicMock()
        rec.id = "sess-42"
        rec.name = "backlog o/r#7"
        rec.branch = "backlog/o-r-7"
        orch._backlog_spawn = AsyncMock(return_value=rec)
        # Patch _backlog_ctx so no DB initialisation is needed in unit tests
        ctx = MagicMock()
        ctx.get_target_state = AsyncMock(return_value={})
        ctx.set_target_state = AsyncMock()
        ctx.delete_target_state = AsyncMock()
        ctx.emit = AsyncMock()
        orch._backlog_ctx = ctx
        return orch


_CANDIDATES = [
    {"ref": "o/r#7", "number": 7, "repo": "o/r", "title": "Fix seven",
     "body": "...", "url": "https://github.com/o/r/issues/7"},
    {"ref": "o/r#8", "number": 8, "repo": "o/r", "title": "Fix eight",
     "body": "...", "url": "https://github.com/o/r/issues/8"},
]


@pytest.mark.asyncio
async def test_dispatch_single_ref_spawns(dispatch_orch):
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "spawned"
    assert r["session_id"] == "sess-42"
    assert r["session_name"] == "backlog o/r#7"
    dispatch_orch._backlog_claim.assert_awaited_once()
    dispatch_orch._backlog_spawn.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_multi_ref_bundles_all(dispatch_orch):
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7", "o/r#8"])
    assert r["status"] == "spawned"
    # The batch passed to _backlog_spawn must include both refs
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.issues == ["o/r#7", "o/r#8"]
    assert batch.primary == "o/r#7"
    assert batch.titles == {"o/r#7": "Fix seven", "o/r#8": "Fix eight"}


@pytest.mark.asyncio
async def test_dispatch_claim_failed(dispatch_orch):
    dispatch_orch._backlog_claim = AsyncMock(return_value=False)
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "claim_failed"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_no_account(dispatch_orch):
    dispatch_orch._backlog_healthy_account = AsyncMock(return_value=None)
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#7"])
    assert r["status"] == "no_account"
    dispatch_orch._backlog_spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_ref_not_in_candidates_uses_fallback_repo(dispatch_orch):
    """A custom ref not in current candidates must still dispatch (uses ref split for repo)."""
    with patch("server.automations.backlog_github.query_candidates", return_value=[]):
        r = await dispatch_orch.backlog_runner_dispatch(["o/r#99"])
    assert r["status"] == "spawned"
    batch = dispatch_orch._backlog_spawn.call_args[0][0]
    assert batch.repo == "o/r"
    assert batch.titles == {}


# ---------------------------------------------------------------------------
# API tests: GET /candidates + POST /dispatch
# ---------------------------------------------------------------------------

@contextmanager
def _api_client(monkeypatch_orch=None):
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/test.db"
        with patch.dict(os.environ, {"CCSWITCH_DB_URL": db_url, "CCSWITCH_DEV": "1",
                                      "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            from fastapi.testclient import TestClient
            from server.app import create_app
            from server.sessions.orchestrator import SessionOrchestrator
            from server.tests.fakes.fake_factory import fake_sdk_factory
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
            if monkeypatch_orch:
                monkeypatch_orch(orch)
            app = create_app(orchestrator=orch)
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_candidates_returns_list():
    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"], "eligible_label": "ready",
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 1, "default_model": None,
        }))
    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        with _api_client(patch_orch) as c:
            _seed(c)
            r = c.get("/api/automations/backlog-runner/candidates", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "candidates" in body
    assert body["candidates"][0]["ref"] == "o/r#7"


def test_dispatch_api_happy_path():
    rec = MagicMock()
    rec.id = "sess-1"
    rec.name = "backlog o/r#7"
    rec.branch = "b"

    def patch_orch(orch):
        orch._backlog_read_config = AsyncMock(return_value=("auto", {
            "enabled_repos": ["o/r"], "eligible_label": "ready",
            "max_concurrent": 1, "max_spawns_per_day": 10,
            "max_issues_per_pr": 3, "default_model": None,
        }))
        orch._backlog_claim = AsyncMock(return_value=True)
        orch._backlog_healthy_account = AsyncMock(return_value="acct-1")
        orch._backlog_spawn = AsyncMock(return_value=rec)

    with patch("server.automations.backlog_github.query_candidates",
               return_value=_CANDIDATES):
        with _api_client(patch_orch) as c:
            _seed(c)
            r = c.post("/api/automations/backlog-runner/dispatch",
                       json={"refs": ["o/r#7"]}, headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "spawned"
    assert r.json()["session_id"] == "sess-1"


def test_dispatch_api_empty_refs_returns_400():
    with _api_client() as c:
        _seed(c)
        r = c.post("/api/automations/backlog-runner/dispatch",
                   json={"refs": []}, headers=H)
    assert r.status_code == 400


def test_dispatch_api_requires_auth():
    with _api_client() as c:
        r = c.post("/api/automations/backlog-runner/dispatch", json={"refs": ["o/r#1"]})
    assert r.status_code == 401


def test_remote_orch_has_backlog_candidates():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_candidates")


def test_remote_orch_has_backlog_runner_dispatch():
    from server.sessions.remote import RemoteOrchestrator
    assert hasattr(RemoteOrchestrator, "backlog_runner_dispatch")


async def _seed_automation():
    """Ensure the backlog-runner row exists so _automation_or_404 passes."""
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(Automation).where(Automation.id == "backlog-runner")
        )).scalar_one_or_none()
        if row is None:
            db.add(Automation(id="backlog-runner", enabled=True))
            await db.commit()


def _seed(c) -> None:
    """Seed on the app's own loop, never via a bare ``asyncio.run``.

    The live TestClient holds the module-level engine on its portal loop; a
    throwaway loop shares that pool's ``asyncio.Queue``, which binds to whichever
    loop first *awaits* a checkout. It only fails once startup saturates the pool
    ("bound to a different event loop"), so it hides locally and dies on CI (#1386).
    """
    assert c.portal is not None
    c.portal.call(_seed_automation)

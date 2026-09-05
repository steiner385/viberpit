"""Tests for persisting initial_prompt on the Session DB row (issue #503).

Covers:
  1. create_session stores the initial_prompt on the DB row (both human and
     automation-origin sessions share the same DbSession constructor path).
  2. load_archived_rows includes initial_prompt in the returned dict.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from sqlalchemy import select

from ccswitch.selector import Picked
from server.db import init_db, db_session
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


# ---------------------------------------------------------------------------
# Helpers shared with sibling tests (same pattern as test_create_session_model.py)
# ---------------------------------------------------------------------------

def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from ccswitch import paths
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.slot_path(name), {
        "accessToken": f"{name}-tok", "refreshToken": f"{name}-ref",
        "expiresAt": int(_time.time() * 1000) + 9_000_000, "subscriptionType": "max",
    })
    if not paths.shared_mcp_path().exists():
        write_json_secure(paths.shared_mcp_path(), {})


def _run_create(tmp_path, *, initial_prompt, origin=None, origin_ref=None):
    """Create one session; return the initial_prompt value read back from the DB row."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ip_test.db'}"

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
    })
    compose_patch = patch("server.sessions.orchestrator.compose", return_value=cfg_dir)
    trust_patch = patch("server.sessions.orchestrator.configdir.trust_project")
    select_patch = patch("server.sessions.orchestrator.select_account",
                         return_value=Picked("alpha"))

    with env_patch, compose_patch, trust_patch, select_patch:
        _seed_account("alpha")

        async def _run():
            from server.db.models import Session as DbSession
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(
                sdk_factory=fake_sdk_factory(),
                poll_interval=9999.0,
                usage_fn=_fake_usage,
                repo_root=str(repo_root),
            )
            await orch.start()
            try:
                rec = await orch.create_session(
                    "test-session",
                    initial_prompt=initial_prompt,
                    origin=origin,
                    origin_ref=origin_ref,
                )
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id)
                    )).scalar_one()
                return row.initial_prompt
            finally:
                await orch.stop()

        return asyncio.run(_run())


# ---------------------------------------------------------------------------
# DB persistence tests
# ---------------------------------------------------------------------------

def test_create_session_persists_initial_prompt_human(tmp_path):
    """Human session (origin=None) persists initial_prompt on the DB row."""
    prompt = "Fix the broken CI pipeline"
    result = _run_create(tmp_path, initial_prompt=prompt, origin=None)
    assert result == prompt


def test_create_session_persists_initial_prompt_automation(tmp_path):
    """Automation session (origin set) persists initial_prompt on the DB row."""
    prompt = "Implement the backlog task for repo/main#42"
    result = _run_create(tmp_path, initial_prompt=prompt,
                         origin="backlog-runner", origin_ref="repo/main#42")
    assert result == prompt


def test_create_session_initial_prompt_null_when_not_provided(tmp_path):
    """Sessions created without an initial_prompt store NULL on the DB row."""
    result = _run_create(tmp_path, initial_prompt=None, origin=None)
    assert result is None


# ---------------------------------------------------------------------------
# load_archived_rows exposes initial_prompt
# ---------------------------------------------------------------------------

def test_load_archived_rows_includes_initial_prompt():
    """load_archived_rows must include initial_prompt in the returned dict."""
    import asyncio
    from server.automations import archive_audit as aa

    class FakeRow:
        id = "s1"; repo = "o/r"; branch = "feat/x"
        authored_branches = ["feat/x"]
        active_goal = "fix ci"; inferred_goal = None; name = "sess"
        initial_prompt = "Fix the broken CI pipeline"

    class FakeScalars:
        def all(self):
            return [FakeRow()]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDb:
        async def execute(self, _q):
            return FakeResult()

    class FakeFactory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return FakeDb()
        async def __aexit__(self, *a):
            return False

    rows = asyncio.run(aa.load_archived_rows(FakeFactory()))
    assert rows[0]["initial_prompt"] == "Fix the broken CI pipeline"


def test_load_archived_rows_initial_prompt_null_when_absent():
    """load_archived_rows maps initial_prompt=None when the row has no prompt."""
    import asyncio
    from server.automations import archive_audit as aa

    class FakeRow:
        id = "s2"; repo = "o/r"; branch = "feat/y"
        authored_branches = []
        active_goal = None; inferred_goal = None; name = "sess2"
        initial_prompt = None

    class FakeScalars:
        def all(self):
            return [FakeRow()]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDb:
        async def execute(self, _q):
            return FakeResult()

    class FakeFactory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return FakeDb()
        async def __aexit__(self, *a):
            return False

    rows = asyncio.run(aa.load_archived_rows(FakeFactory()))
    assert rows[0]["initial_prompt"] is None

"""orchestrator.create_session persists origin/origin_ref onto the Session row."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from sqlalchemy import select

from ccswitch.selector import Picked
from server.db import init_db, db_session
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


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


def test_create_session_persists_origin(tmp_path):
    """create_session with origin/origin_ref persists those onto the Session DB row."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_path = tmp_path / "origin_test.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

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
                    "auto",
                    origin="pr-doctor",
                    origin_ref="a/b#1",
                )
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id)
                    )).scalar_one()
                return rec, row.origin, row.origin_ref
            finally:
                await orch.stop()

        rec, row_origin, row_origin_ref = asyncio.run(_run())

    assert row_origin == "pr-doctor", f"Expected 'pr-doctor', got {row_origin!r}"
    assert row_origin_ref == "a/b#1", f"Expected 'a/b#1', got {row_origin_ref!r}"
    # Also check the in-memory record
    assert rec.origin == "pr-doctor"
    assert rec.origin_ref == "a/b#1"

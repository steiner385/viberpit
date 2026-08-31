"""orchestrator.create_session applies + persists the chosen model.

A model picked in the New Session dialog must be applied to the driver before the
first turn AND persisted (so it survives rehydrate and driver rebuilds). The
"default" alias means "account default" → no model is applied or stored.
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


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from ccswitch.oauth import Usage
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return Usage(10.0, reset, 10.0, reset, False)


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


def _run_create(tmp_path, *, model):
    """Create one session with the given model; return (record, db_row_model,
    driver_client_model)."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_path = tmp_path / "model_test.db"
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
                rec = await orch.create_session("auto", model=model)
                driver_model = orch._drivers[rec.id]._client.model
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id)
                    )).scalar_one()
                return rec, row.model, driver_model
            finally:
                await orch.stop()

        return asyncio.run(_run())


def test_create_session_applies_and_persists_model(tmp_path):
    # A non-gated tier: the model-approval gate parks gated models (e.g. fable) as
    # pending_approval with no driver instead of applying them — that path is covered
    # by test_orchestrator_model_gate. Here we assert the ordinary apply+persist path.
    rec, row_model, driver_model = _run_create(tmp_path, model="opus")
    assert driver_model == "opus"    # applied to the live driver before the first turn
    assert row_model == "opus"       # persisted onto the DB row
    assert rec.model == "opus"       # mirrored on the in-memory record


def test_create_session_default_model_is_account_default(tmp_path):
    # "default" (and None) mean "account default": nothing applied, nothing stored.
    rec, row_model, driver_model = _run_create(tmp_path, model="default")
    assert driver_model is None
    assert row_model is None
    assert rec.model is None

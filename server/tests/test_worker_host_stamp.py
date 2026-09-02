"""A WORKER daemon stamps its own host name onto sessions it creates locally.

The dispatcher stamps ``Session.host`` for sessions it routes to a worker, but a
worker's own in-process spawns (backlog-runner, pr-doctor, pm-classify …) never
pass through it and landed with host=NULL. NULL means "the primary's" to
``_owns_record`` and to a worker's ``rehydrate`` filter, so such a session was
(a) acted on by the primary's rotation/stuck loops, (b) dropped by its own host on
restart, and (c) absent from the primary's ``_records`` → invisible on the
dashboard. Stamping at creation makes ``host`` the truth from the first write.

The primary keeps writing NULL (its own spawns are "primary's" by definition), so
single-host installs are unchanged.

Every path also seeds ``created_at`` on the in-memory record: the API serialised
``createdAt: null`` for fresh sessions until the next restart re-read the row, and
the dashboard's age sort turned that into epoch 0 — the card sank to the bottom.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from sqlalchemy import select

from ccswitch.selector import Picked
from server.db import db_session, init_db
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


def _create_on(tmp_path, *, role: str, host_name: str):
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'host_stamp.db'}"

    env_patch = patch.dict(os.environ, {
        "CCSWITCH_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DAEMON_ROLE": role,
        "CCSWITCH_HOST_NAME": host_name,
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
                rec = await orch.create_session("auto", origin="pr-doctor",
                                                origin_ref="a/b#1")
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id)
                    )).scalar_one()
                return rec, row.host, row.created_at
            finally:
                await orch.stop()

        return asyncio.run(_run())


def test_worker_stamps_own_host_on_local_create(tmp_path):
    rec, row_host, row_created = _create_on(tmp_path, role="worker", host_name="rocky-test")
    assert row_host == "rocky-test", f"DB row should be stamped with the worker, got {row_host!r}"
    assert rec.host == "rocky-test", f"in-memory record should carry the host, got {rec.host!r}"


def test_primary_leaves_host_null_on_local_create(tmp_path):
    # NULL host == "the primary's" per _owns_record; do not change that contract.
    rec, row_host, _ = _create_on(tmp_path, role="primary", host_name="dobby-test")
    assert row_host is None
    assert rec.host is None


def test_fresh_record_carries_created_at(tmp_path):
    rec, _, row_created = _create_on(tmp_path, role="primary", host_name="dobby-test")
    assert rec.created_at is not None, "createdAt must not serialise as null on a fresh session"
    assert row_created is not None


@pytest.mark.parametrize("role", ["worker", "primary"])
def test_queued_record_carries_host_and_created_at(monkeypatch, role):
    """The queued path builds its record/row without a driver — same stamping rules."""
    from datetime import datetime

    from server.sessions import orchestrator as mod

    monkeypatch.setenv("CCSWITCH_DAEMON_ROLE", role)
    monkeypatch.setenv("CCSWITCH_HOST_NAME", "h-test")
    o = SessionOrchestrator(sdk_factory=lambda *a, **k: None)

    captured: dict = {}

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def add(self, row):
            captured["row"] = row

    async def _enqueue(session_id, payload, **kw):
        return 0

    monkeypatch.setattr(mod, "db_session", lambda: _DB())
    from server.sessions import session_queue as sq
    monkeypatch.setattr(sq, "enqueue", _enqueue)

    rec = asyncio.run(o._enqueue_session(
        "sid-q", name="n", account="acct", initial_prompt=None, cols=80, rows=24,
        cwd="/tmp", branch=None, repo=None, identity="me", permission_mode="interactive",
        attachment_ids=[], name_is_auto=True, origin="backlog-runner",
        origin_ref="x#1", model=None))

    expected = "h-test" if role == "worker" else None
    assert captured["row"].host == expected
    assert rec.host == expected
    assert isinstance(rec.created_at, datetime)

"""Automation-spawned sessions must actually START without a human.

A session created with an ``initial_prompt`` normally DEFERS that prompt until
the first WebSocket client attaches, so the turn's events stream live to a
watching client (issue #4). But an automation-spawned session (``origin`` set —
backlog-runner / pr-doctor) has no human who will ever attach, so the deferred
prompt was never sent and the session sat idle forever (driver running, no work).

So: when ``origin`` is set, ``create_session`` sends the initial prompt
immediately (the driver persists its transcript and fires status callbacks with
no live subscriber; a later human attach replays it). A human-created session
(``origin`` is None) keeps the deferred-until-attach behavior.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from ccswitch.selector import Picked
from server.db import init_db
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


def _run(tmp_path, *, origin, initial_prompt):
    """Create one session; return (prompts_sent_to_driver, deferred_in_pending)."""
    store_home = tmp_path / "store"; store_home.mkdir()
    shared_base = tmp_path / "dot-claude"; shared_base.mkdir()
    (shared_base / "settings.json").write_text("{}")
    (shared_base / "projects").mkdir()
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    repo_root = tmp_path / "reporoot"; repo_root.mkdir()
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'autostart.db'}"

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

        async def _go():
            await init_db(db_url=db_url)
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                       usage_fn=_fake_usage, repo_root=str(repo_root))
            await orch.start()
            try:
                rec = await orch.create_session("auto", initial_prompt=initial_prompt,
                                                origin=origin, origin_ref="a/b#1")
                sent = list(orch._drivers[rec.id]._client.sent)
                deferred = rec.id in orch._pending_initial_prompt
                return sent, deferred
            finally:
                await orch.stop()

        return asyncio.run(_go())


def test_origin_session_sends_prompt_immediately(tmp_path):
    sent, deferred = _run(tmp_path, origin="backlog-runner", initial_prompt="Implement a/b#1")
    assert sent == ["Implement a/b#1"]  # ran without any human attach
    assert deferred is False            # NOT parked in the pending map


def test_human_session_defers_prompt_until_attach(tmp_path):
    sent, deferred = _run(tmp_path, origin=None, initial_prompt="Hello!")
    assert sent == []       # nothing sent yet — waits for the first WS attach
    assert deferred is True  # parked in the pending map for the attach handler

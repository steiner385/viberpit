"""Every session created with an initial prompt must START on its own.

A session created with an ``initial_prompt`` sends it as soon as the driver is up
and the row is durable — for EVERY origin. Historically (issue #4) a human-created
session (``origin`` None) parked its prompt in ``_pending_initial_prompt`` until the
first WebSocket client attached, so the turn would stream live to a watching
client. In practice that meant a session created from the dispatch bar, a preset
or the mobile UI sat at "Ready" doing nothing until somebody navigated into it —
and a session nobody opened never ran at all. Automation-spawned sessions (``origin``
set — backlog-runner / pr-doctor) already sent immediately, and a later human
attach replays the turn from the driver snapshot (mid-turn) or the transcript
(settled turn), which is the SAME path a human session now relies on.

The one thing that still defers is a bare slash command typed as the initial
prompt (``/model opus``, ``/help`` …): it has to run through the WS bridge's
command dispatch, not go to the model as literal text (#943), and that dispatch
only exists on a live socket.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from viberpit.selector import Picked
from server.db import init_db
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory


def _fake_usage(account: str):
    from datetime import datetime, timedelta, timezone
    from viberpit.quota import anthropic_snapshot
    reset = datetime.now(timezone.utc) + timedelta(hours=5)
    return anthropic_snapshot(10.0, reset, 10.0, reset, False)


def _seed_account(name: str) -> None:
    import time as _time
    from viberpit import paths
    from viberpit.creds import write_json_secure
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
        "VIBERPIT_HOME": str(store_home),
        "CLAUDE_CONFIG_DIR": str(shared_base),
        "VIBERPIT_DB_URL": db_url,
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


def test_human_session_sends_prompt_immediately(tmp_path):
    """A session created by a human must start working without being opened."""
    sent, deferred = _run(tmp_path, origin=None, initial_prompt="Hello!")
    assert sent == ["Hello!"], (
        "a human-created session parked its initial prompt instead of running it — "
        "it sits at 'Ready' until someone navigates into the session")
    assert deferred is False


def test_human_slash_command_prompt_stays_deferred_for_the_bridge(tmp_path):
    """A bare slash command as the initial prompt is NOT model input: it must run
    through the WS bridge's command dispatch (#943), which only exists on a live
    socket — so it is the one shape that still waits for the first attach."""
    sent, deferred = _run(tmp_path, origin=None, initial_prompt="/model opus")
    assert sent == []        # never sent to the model as literal text
    assert deferred is True  # parked for ws.py's _send_deferred_initial_prompt

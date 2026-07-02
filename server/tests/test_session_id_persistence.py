"""Tests for Task 1: persist claude_session_id to DB the moment the driver captures it."""
import asyncio
from ccswitch.selector import Picked
from server.sessions.driver import SessionDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.events import TurnEnd
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from claude_agent_sdk import TextBlock


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


def test_driver_fires_on_session_id_once_when_captured():
    msgs = [f.sys_init("sess-77"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-77", uuid="t1")]
    calls = []

    async def on_sid(session_id, claude_session_id):
        calls.append((session_id, claude_session_id))

    async def run():
        d = SessionDriver("local-1", "primary", "/tmp/r", "/tmp/cfg",
                          mode="interactive", sdk_factory=_factory(msgs),
                          registry=PermissionRegistry(), identity="me",
                          on_session_id=on_sid)
        await d.start()
        await d.send_prompt("hi")
        async for e in d.events():
            if isinstance(e, TurnEnd):
                break
        await d.aclose()

    asyncio.run(run())
    assert calls == [("local-1", "sess-77")]   # exactly once, with both ids


def test_orchestrator_persists_claude_session_id_to_db(tmp_path):
    """The orchestrator persists claude_session_id once the driver captures it.

    Uses send_prompt (not initial_prompt) because initial_prompt is now deferred
    until first WS attach — the on_session_id callback fires once the turn drains.
    """
    from sqlalchemy import select
    from server.db import init_db, db_session
    from server.db.models import Session as DbSession
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    from server.tests.fakes import fake_sdk as f
    from claude_agent_sdk import TextBlock
    from unittest.mock import patch

    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    msgs = [f.sys_init("claude-abc"), f.stream_start(uuid="t1"),
            f.stream_text("ok", uuid="t1"),
            f.assistant(TextBlock(text="ok"), uuid="t1"),
            f.result(session_id="claude-abc", uuid="t1")]

    async def run():
        await init_db(db_url=db_url)
        with patch("server.sessions.orchestrator.compose", return_value=tmp_path/"cfg"), \
             patch("server.sessions.orchestrator.configdir.trust_project"), \
             patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")):
            orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(msgs),
                                       work_dir=str(tmp_path))
            rec = await orch.create_session("s")
            # Send the prompt explicitly (initial_prompt is now deferred until first attach)
            await orch.send_prompt(rec.id, "hi")
            # let the turn drain so the id is captured + persisted
            row = None
            for _ in range(50):
                async with db_session() as db:
                    row = (await db.execute(
                        select(DbSession).where(DbSession.id == rec.id))).scalar_one()
                    if row.claude_session_id == "claude-abc":
                        break
                await asyncio.sleep(0.02)
            await orch.close_session(rec.id)
            return row.claude_session_id

    assert asyncio.run(run()) == "claude-abc"

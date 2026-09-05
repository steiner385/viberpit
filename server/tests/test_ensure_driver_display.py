"""Regression: ensure_driver must allocate an X11 display for a resumed session.

The bug: create_session and adopt_session both call x11.start_display() before
building the driver, so the per-session factory closure injects DISPLAY/XAUTHORITY
into the spawned claude subprocess. ensure_driver — the path that re-attaches a
*detached* (rehydrated) session — built the driver with the same display-injecting
factory but never called start_display. So x11.env_for() returned {} and the
resumed claude process launched with NO DISPLAY, silently routing X rendering off
the virtual viewport.

This matters in production: every deploy restarts the daemon, which detaches all
live sessions; re-attaching them goes through ensure_driver. So after the first
restart, essentially every session ran without a display.
"""

import asyncio
from unittest.mock import patch

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient


class FakeX11:
    """Mirrors X11Manager's contract: env_for is empty until start_display runs."""

    def __init__(self):
        self.started = []
        self.stopped = []
        self._env = {}

    async def start_display(self, sid):
        self.started.append(sid)
        self._env[sid] = {"DISPLAY": ":100", "XAUTHORITY": "/x"}
        return object()

    def env_for(self, sid):
        return self._env.get(sid, {})

    def display_for(self, sid):
        return object() if sid in self._env else None

    async def stop_display(self, sid):
        self.stopped.append(sid)
        self._env.pop(sid, None)


def _recording_factory(captured):
    """A factory that records the extra_env it was built with (the subprocess env)."""

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        captured["extra_env"] = extra_env
        return FakeSDKClient([], on_can_use_tool=can_use_tool)

    return make


async def _ensure(orch, tmp_path, sid):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"):
        return await orch.ensure_driver(sid)


def test_ensure_driver_allocates_and_injects_display(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    captured = {}

    async def run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id="s1", name="S1", account_name="acct1",
                             engine_session_id="cs1", status="idle", cwd=str(tmp_path)))
        x11 = FakeX11()
        orch = SessionOrchestrator(sdk_factory=_recording_factory(captured), x11=x11)
        await orch.rehydrate()
        await _ensure(orch, tmp_path, "s1")
        return x11.started

    started = asyncio.run(run())
    assert "s1" in started, "ensure_driver must allocate a display for the resumed session"
    assert captured["extra_env"] == {"DISPLAY": ":100", "XAUTHORITY": "/x"}, (
        "the resumed session's SDK subprocess must receive DISPLAY/XAUTHORITY"
    )

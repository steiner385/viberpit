"""``SessionRecord.detached`` is derived from driver presence, never hand-maintained.

Regression (frozen "Answer needed" session, 2026-09-02): the edge decides
"read-only disk replay" vs "live attach" off ``record.detached``. It was a shadow
boolean: set True when a record was rebuilt from its DB row at daemon boot, and
cleared at exactly ONE line of ``ensure_driver`` — after the driver was already
registered and after an awaited SDK ``set_model`` that can raise. The reassign
path (``_install_driver``) never cleared it at all. Once a driver existed with the
flag still True, ``ensure_driver``'s fast path returned that driver forever without
touching the flag, so every attach took the detached shortcut: no pending-question
re-emit, no live status — a session blocked on AskUserQuestion showed a read-only
card the user could not answer.

Now ``get_session`` / ``list_sessions`` report ``detached`` as "no driver in
``_drivers``", which is exactly the condition ``ensure_driver`` would branch on.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.db import db_session, init_db
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_sdk import FakeSDKClient


class _SetModelExplodes(FakeSDKClient):
    async def set_model(self, model=None):
        raise RuntimeError("control request timed out")


def _factory(client_cls):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return client_cls([], on_can_use_tool=can_use_tool)
    return make


async def _build_orch(tmp_path, *, client_cls=FakeSDKClient, **row):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"
    await init_db(db_url=db_url)
    base = dict(id="s1", name="S1", account_name="acct1", engine_session_id="cs1",
                status="idle", cwd=str(tmp_path))
    base.update(row)
    async with db_session() as db:
        db.add(DbSession(**base))
    orch = SessionOrchestrator(sdk_factory=_factory(client_cls))
    await orch.rehydrate()
    return orch


async def _ensure(orch, tmp_path, sid):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"):
        return await orch.ensure_driver(sid, "me")


def test_detached_tracks_driver_presence_on_every_read_path(tmp_path):
    async def run():
        orch = await _build_orch(tmp_path)
        assert orch.get_session("s1").detached is True          # rehydrated, no driver
        # Any install path — ensure_driver, _reassign/_install_driver, adopt — ends
        # with a driver in _drivers. The flag must follow that, not the path taken.
        orch._drivers["s1"] = object()
        assert orch.get_session("s1").detached is False
        assert orch.get_session("s1").to_dict()["detached"] is False
        assert [r.detached for r in orch.list_sessions()] == [False]
        orch._drivers.pop("s1")
        assert orch.get_session("s1").detached is True
        assert [r.to_dict()["detached"] for r in orch.list_sessions()] == [True]

    asyncio.run(run())


def test_ensure_driver_reports_live_even_when_set_model_raises(tmp_path):
    """The exact window that stranded the production session: the driver is
    registered, then the persisted model is re-applied through the SDK. A failure
    there must neither lose the (running) driver nor leave the record detached."""
    async def run():
        orch = await _build_orch(tmp_path, client_cls=_SetModelExplodes, model="fable")
        driver = await _ensure(orch, tmp_path, "s1")
        assert driver is not None
        assert orch._drivers["s1"] is driver
        assert orch.get_session("s1").detached is False
        assert (await _ensure(orch, tmp_path, "s1")) is driver   # fast path, still live
        assert orch.get_session("s1").detached is False
        await orch.close_session("s1")

    asyncio.run(run())

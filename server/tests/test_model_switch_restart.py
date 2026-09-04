# server/tests/test_model_switch_restart.py
"""Model switch on a BUSY session must stop → switch → replay (same dance _reassign
does across drivers, here on the same driver); on a detached session it must persist
so the model applies on resume; and ensure_driver must re-apply the persisted model
(it previously dropped it while _reassign re-applied it)."""
import asyncio
import time
from unittest.mock import patch

import pytest

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


class _FakeDriver:
    """Records the interrupt/set_model/send_prompt call order."""
    def __init__(self, turn_active: bool = False):
        self.turn_active = turn_active
        self.model = "untouched"
        self.calls: list = []

    async def interrupt(self):
        self.calls.append("interrupt")
        self.turn_active = False

    async def set_model(self, m):
        self.calls.append(("set_model", m))
        self.model = m

    async def send_prompt(self, p):
        self.calls.append(("send_prompt", p))


async def _init_mini_db():
    """set_model_for_session now consults resolve_model_policy (the mid-session
    model-approval gate), which opens a db_session(); init an in-memory DB (on the
    caller's own loop) so these driver-mechanics tests reach the apply path. Default
    policy = allow for the un-gated tiers (opus/sonnet) these tests switch to."""
    await init_db(db_url="sqlite+aiosqlite:///:memory:")


def _mini_orch(driver=None, last_prompt=None, session_id="s1"):
    """Minimal orchestrator: record + optional driver, persistence patched to memory."""
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = SessionRecord(id=session_id, name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp")
    orch._records = {session_id: rec}
    orch._drivers = {session_id: driver} if driver is not None else {}
    orch._last_prompt = {session_id: last_prompt} if last_prompt else {}
    orch._turn_restart_at = {}
    orch._clock = time.time
    persisted = {}

    async def _persist(sid, model):
        persisted[sid] = model
    orch._persist_model = _persist  # type: ignore[attr-defined]
    return orch, rec, persisted


# -----------------------------------------------------------------------
# a) busy live session with a stored last prompt → stop, switch, replay
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_busy_session_interrupts_switches_and_resends():
    driver = _FakeDriver(turn_active=True)
    orch, rec, persisted = _mini_orch(driver, last_prompt="do the thing")
    await _init_mini_db()

    restarted = await orch.set_model_for_session("s1", "opus")

    assert restarted is True
    assert driver.calls == ["interrupt", ("set_model", "opus"),
                            ("send_prompt", "do the thing")]
    assert rec.model == "opus"
    assert persisted == {"s1": "opus"}


# -----------------------------------------------------------------------
# b) busy live session with NO last prompt → stop + switch, nothing resent
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_busy_session_without_last_prompt_does_not_resend():
    driver = _FakeDriver(turn_active=True)
    orch, rec, persisted = _mini_orch(driver, last_prompt=None)
    await _init_mini_db()

    restarted = await orch.set_model_for_session("s1", "opus")

    assert restarted is False
    assert driver.calls == ["interrupt", ("set_model", "opus")]
    assert rec.model == "opus"
    assert persisted == {"s1": "opus"}


# -----------------------------------------------------------------------
# c) idle live session → plain set_model, no interrupt, no resend
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idle_session_switches_without_interrupt():
    driver = _FakeDriver(turn_active=False)
    # A stored last prompt must NOT be replayed when the session is idle.
    orch, rec, persisted = _mini_orch(driver, last_prompt="old prompt")
    await _init_mini_db()

    restarted = await orch.set_model_for_session("s1", "sonnet")

    assert restarted is False
    assert driver.calls == [("set_model", "sonnet")]
    assert rec.model == "sonnet"
    assert persisted == {"s1": "sonnet"}


# -----------------------------------------------------------------------
# d) detached session: run_command('/model') persists to record + DB and
#    says the model applies on resume (previously swallowed by the
#    "isn't live" guard, so the picker was a silent no-op)
# -----------------------------------------------------------------------

def test_run_command_model_on_detached_session_persists(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s-detached"

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="t", account_name="acct1",
                claude_session_id="csid", status="idle", cwd=cwd,
            ))
        from server.tests.fakes.fake_factory import fake_sdk_factory
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()

        res = await orch.run_command(session_id, "model", "opus")

        from sqlalchemy import select as sa_select
        async with db_session() as db:
            row = (await db.execute(
                sa_select(DbSession).where(DbSession.id == session_id)
            )).scalar_one()
            row_model = row.model
        return orch, res, row_model

    orch, res, row_model = asyncio.run(_run())

    assert res == {"name": "model", "render": "notice",
                   "data": {"text": "Model → opus — saved; applies when the session resumes"}}
    assert orch._records[session_id].model == "opus"
    assert row_model == "opus"


# -----------------------------------------------------------------------
# e) busy live session via run_command → "stopped … and restarted" notice
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_command_model_on_busy_session_reports_restart():
    driver = _FakeDriver(turn_active=True)
    orch, rec, persisted = _mini_orch(driver, last_prompt="keep going")
    await _init_mini_db()

    res = await orch.run_command("s1", "model", "opus")

    assert res == {"name": "model", "render": "notice",
                   "data": {"text": "Model → opus — stopped the current activity "
                                    "and restarted it on the new model"}}
    assert ("send_prompt", "keep going") in driver.calls
    assert persisted == {"s1": "opus"}


@pytest.mark.asyncio
async def test_run_command_model_on_idle_session_keeps_plain_notice():
    driver = _FakeDriver(turn_active=False)
    orch, rec, persisted = _mini_orch(driver)
    await _init_mini_db()

    res = await orch.run_command("s1", "model", "opus")

    assert res == {"name": "model", "render": "notice",
                   "data": {"text": "Model → opus"}}


# -----------------------------------------------------------------------
# f) ensure_driver re-applies the persisted model on resume (mirrors _reassign)
# -----------------------------------------------------------------------

def test_ensure_driver_reapplies_persisted_model(tmp_path):
    from server.tests.fakes.fake_sdk import FakeSDKClient
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s-model"
    clients = []

    def factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool)
        clients.append(c)
        return c

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="t", account_name="acct1",
                claude_session_id="csid", status="idle", cwd=cwd,
                model="opus",
            ))
        orch = SessionOrchestrator(sdk_factory=factory)
        await orch.rehydrate()
        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id)
        return driver

    driver = asyncio.run(_run())
    assert driver is not None
    assert clients, "factory was never called"
    assert clients[-1].model == "opus", (
        "ensure_driver must re-apply the persisted model to the resumed driver "
        "(rec.model was previously dropped on resume)"
    )


def test_ensure_driver_no_persisted_model_skips_set_model(tmp_path):
    """No persisted model → the driver stays on the account default (set_model
    must not be called with None, which would clobber an SDK-side default)."""
    from server.tests.fakes.fake_sdk import FakeSDKClient
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'ccswitch.db'}"
    cwd = str(tmp_path)
    session_id = "s-no-model"
    clients = []

    def factory(sdk_cwd, cfg_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        c = FakeSDKClient([], on_can_use_tool=can_use_tool)
        clients.append(c)
        return c

    async def _run():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(
                id=session_id, name="t", account_name="acct1",
                claude_session_id="csid", status="idle", cwd=cwd,
            ))
        orch = SessionOrchestrator(sdk_factory=factory)
        await orch.rehydrate()
        cfg_dir = tmp_path / "cfg" / "acct1"
        cfg_dir.mkdir(parents=True)
        with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
             patch("server.sessions.orchestrator.configdir"):
            driver = await orch.ensure_driver(session_id)
        return driver

    driver = asyncio.run(_run())
    assert driver is not None
    assert clients[-1].model is None  # FakeSDKClient default — set_model never called


# -----------------------------------------------------------------------
# g) when="after_turn": a busy session switches WITHOUT interrupting — the
#    binary applies set_model on the NEXT turn (verified against claude
#    2.1.251: a mid-turn set_model leaves the in-flight turn on the old
#    model). The record flags the switch as pending until the turn ends.
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_after_turn_on_busy_session_switches_without_interrupt():
    driver = _FakeDriver(turn_active=True)
    orch, rec, persisted = _mini_orch(driver, last_prompt="do the thing")
    await _init_mini_db()

    restarted = await orch.set_model_for_session("s1", "opus", when="after_turn")

    assert restarted is False
    assert driver.calls == [("set_model", "opus")]      # no interrupt, no resend
    assert rec.model == "opus"
    assert rec.model_pending is True
    assert persisted == {"s1": "opus"}


@pytest.mark.asyncio
async def test_after_turn_on_idle_session_is_not_pending():
    driver = _FakeDriver(turn_active=False)
    orch, rec, _ = _mini_orch(driver)
    await _init_mini_db()

    await orch.set_model_for_session("s1", "opus", when="after_turn")

    assert driver.calls == [("set_model", "opus")]
    assert rec.model_pending is False


@pytest.mark.asyncio
async def test_switch_now_clears_an_earlier_pending_switch():
    driver = _FakeDriver(turn_active=True)
    orch, rec, _ = _mini_orch(driver, last_prompt="p")
    await _init_mini_db()
    await orch.set_model_for_session("s1", "opus", when="after_turn")
    assert rec.model_pending is True

    await orch.set_model_for_session("s1", "sonnet", when="now")

    assert rec.model_pending is False
    assert rec.model == "sonnet"


def test_pending_switch_settles_when_the_turn_ends():
    orch, rec, _ = _mini_orch(_FakeDriver(turn_active=True))
    rec.model_pending = True
    # Still mid-turn → stays pending.
    orch._settle_model_pending("s1", "in_progress")
    assert rec.model_pending is True
    orch._settle_model_pending("s1", "condensing")
    assert rec.model_pending is True
    # Turn handed back → settled.
    orch._settle_model_pending("s1", "done")
    assert rec.model_pending is False
    # Unknown session: no-op, no crash.
    orch._settle_model_pending("ghost", "done")


@pytest.mark.asyncio
async def test_run_command_model_after_turn_flag_on_busy_session():
    driver = _FakeDriver(turn_active=True)
    orch, rec, _ = _mini_orch(driver, last_prompt="p")
    await _init_mini_db()

    res = await orch.run_command("s1", "model", "opus --after-turn")

    assert "interrupt" not in driver.calls
    assert driver.calls == [("set_model", "opus")]
    assert rec.model == "opus"
    assert "finishes" in res["data"]["text"]
    assert "opus" in res["data"]["text"]


@pytest.mark.asyncio
async def test_run_command_model_default_alias_means_account_default():
    driver = _FakeDriver(turn_active=False)
    orch, rec, persisted = _mini_orch(driver)
    await _init_mini_db()

    res = await orch.run_command("s1", "model", "default")

    # "default" is the picker's special row, not a real alias: it clears the
    # override (mirrors create_session, which treats "default" as None).
    assert driver.calls == [("set_model", None)]
    assert rec.model is None
    assert persisted == {"s1": None}
    assert "default" in res["data"]["text"]


def test_record_roundtrips_model_pending():
    rec = SessionRecord(id="s1", name="n", account="a", claude_session_id="c1",
                        cols=80, rows=24, cwd="/tmp", model="opus", model_pending=True)
    d = rec.to_dict()
    assert d["modelPending"] is True
    assert SessionRecord.from_dict(d).model_pending is True
    assert SessionRecord.from_dict({**d, "modelPending": False}).model_pending is False

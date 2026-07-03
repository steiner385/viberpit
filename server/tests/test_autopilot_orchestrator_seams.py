"""Autopilot orchestrator seams — the injected dependencies AutopilotLoop calls.

Drives the pure seams (`_autopilot_read_config`, `_autopilot_apply`) against a bare
orchestrator built with ``__new__`` (no full init, no network), mirroring the
backlog-heartbeat test harness.
"""
from __future__ import annotations

import asyncio

from server.automations.autopilot_policy import Decision


def _bare_orch():
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    return orch


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ap.db'}")
    await registry.ensure_rows()


async def _set_row(*, enabled: bool, mode: str) -> None:
    from sqlalchemy import update as sa_update
    from server.db import db_session
    from server.db.models import Automation
    async with db_session() as db:
        await db.execute(sa_update(Automation).where(Automation.id == "autopilot")
                         .values(enabled=enabled, mode=mode))


def test_read_config_defaults_to_off_when_disabled(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        mode, config = await orch._autopilot_read_config()
        assert mode == "off"
        assert config["max_actions_per_session_per_hour"] == 12
    asyncio.run(run())


def test_read_config_off_while_draining(tmp_path):
    async def run():
        await _init_db(tmp_path)
        orch = _bare_orch()
        await _set_row(enabled=True, mode="auto")
        orch._draining = True
        mode, _ = await orch._autopilot_read_config()
        assert mode == "off"                       # draining forces off
        orch._draining = False
        mode2, _ = await orch._autopilot_read_config()
        assert mode2 == "auto"                     # enabled+auto once drain clears
    asyncio.run(run())


def test_apply_noops_for_terminal_actions(tmp_path):
    async def run():
        orch = _bare_orch()
        # escalate / complete / skip never mutate — return False without a driver.
        assert await orch._autopilot_apply("missing", Decision(action="escalate")) is False
        assert await orch._autopilot_apply("missing", Decision(action="complete")) is False
        assert await orch._autopilot_apply("missing", Decision(action="skip")) is False
    asyncio.run(run())


def _perm_ctx(perm_id, command):
    from server.automations.autopilot_policy import DecisionContext
    return DecisionContext(
        session_id="s1", status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="g", goal_source="explicit",
        stuck={"kind": "permission", "perm_id": perm_id,
               "tool_name": "Bash", "tool_input": {"command": command}},
        transcript_tail=[], ledger={})


def test_apply_resolves_decide_time_perm_and_rechecks_denylist(tmp_path):
    """TOCTOU guard: apply resolves exactly the decide-time perm_id, and a
    deny-listed command can never be approved even if it reaches apply."""
    async def run():
        orch = _bare_orch()
        calls: list = []

        async def _ensure(sid, ident=None):
            return object()

        orch.ensure_driver = _ensure
        orch.resolve_permission = (
            lambda sid, pid, decision, ident: calls.append((pid, decision)) or True)

        # Safe perm → approved via the SAME perm_id passed in (no re-fetch).
        assert await orch._autopilot_apply(
            "s1", Decision(action="approve"), _perm_ctx("P1", "pytest")) is True
        assert calls == [("P1", True)]

        # A destructive command reaching apply is blocked by the deny-list re-check;
        # resolve_permission is never called.
        calls.clear()
        assert await orch._autopilot_apply(
            "s1", Decision(action="approve"), _perm_ctx("P2", "git push --force")) is False
        assert calls == []
    asyncio.run(run())

"""Autopilot must not inject into a session whose turn is mid-restart.

A model swap on a busy session runs interrupt → set_model → replay (and an account
reassign runs the same dance across drivers). The interrupt flips the session through
an actionable status, which fires the status-change autopilot nudge tick — so autopilot
could inject a competing prompt into the very session that is about to replay its own.
The fix: stamp the restart start, and have both governance entry points (list_governed
and apply) skip the session until the dance has had time to unwind."""
from __future__ import annotations

import asyncio

from server.automations.autopilot_policy import Decision, DecisionContext
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord

_NOW = 1000.0


def _rec(sid, status="waiting_input"):
    return SessionRecord(id=sid, name="n", account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="",
                         cols=80, rows=24, cwd="/tmp", status=status)


def _bare_orch(now=_NOW):
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._draining = False
    orch._records = {}
    orch._drivers = {}
    orch._turn_restart_at = {}
    orch._swapping = {}
    orch._clock = lambda: now
    return orch


async def _init_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ap.db'}")
    await registry.ensure_rows()


def _ctx(sid="s1"):
    return DecisionContext(
        session_id=sid, status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="g", goal_source="explicit", stuck={"kind": "waiting_input"},
        transcript_tail=[], ledger={})


def test_list_governed_skips_session_mid_turn_restart(tmp_path):
    """A session inside the restart-defer window is invisible to the loop; once the
    window elapses it is governed again (a swap with nothing to replay must not
    strand the session ungoverned forever)."""
    async def run():
        from server.sessions.orchestrator import TURN_RESTART_DEFER_S
        await _init_db(tmp_path)
        orch = _bare_orch()
        orch._records = {"swapping": _rec("swapping"), "steady": _rec("steady")}
        orch._turn_restart_at["swapping"] = _NOW - 1.0
        governed = set(await orch._autopilot_list_governed())
        assert "swapping" not in governed
        assert "steady" in governed

        orch._turn_restart_at["swapping"] = _NOW - TURN_RESTART_DEFER_S - 1.0
        governed = set(await orch._autopilot_list_governed())
        assert "swapping" in governed
    asyncio.run(run())


def test_apply_refuses_mid_restart_session(tmp_path):
    """The decider's await window can outlive a swap that starts mid-tick, so apply
    itself must re-check: no prompt is injected while the restart is in flight."""
    async def run():
        orch = _bare_orch()
        orch._records = {"s1": _rec("s1")}
        sent: list[str] = []

        async def _ensure(sid, ident=None):
            return object()
        orch.ensure_driver = _ensure

        async def _send(sid, text, attachment_ids=None):
            sent.append(text)
            return "sent"
        orch.send_prompt = _send

        orch._turn_restart_at["s1"] = _NOW - 1.0
        ok = await orch._autopilot_apply("s1", Decision(action="nudge", prompt="go"), _ctx())
        assert ok is False
        assert sent == []
    asyncio.run(run())


def test_set_model_stamps_restart_on_live_session():
    """set_model_for_session on a live driver marks the restart window; a detached
    (driverless) session persists the model with no dance, so no stamp."""
    async def run():
        class _Driver:
            turn_active = True
            async def interrupt(self): ...
            async def set_model(self, m): ...
            async def send_prompt(self, p): ...

        orch = _bare_orch()
        orch._records = {"s1": _rec("s1"), "s2": _rec("s2")}
        orch._drivers = {"s1": _Driver()}
        orch._last_prompt = {"s1": "do the thing"}

        async def _persist(sid, model): ...
        orch._persist_model = _persist

        await orch.set_model_for_session("s1", "opus")
        assert orch._turn_restart_at.get("s1") == _NOW

        await orch.set_model_for_session("s2", "opus")   # detached: persist only
        assert "s2" not in orch._turn_restart_at
    asyncio.run(run())


def test_reassign_stamps_restart():
    """_reassign (rotation/recompose/manual account switch) is the same
    stop → switch → replay dance, stamped before it starts unwinding."""
    async def run():
        import server.sessions.orchestrator as orch_mod
        from unittest.mock import patch

        orch = _bare_orch()
        rec = _rec("s1")
        orch._records = {"s1": rec}

        def _boom(account):
            raise RuntimeError("stop early — stamp must already be set")
        with patch.object(orch_mod, "compose", _boom):
            await orch._reassign(rec, "other", reason="manual")
        assert orch._turn_restart_at.get("s1") == _NOW
    asyncio.run(run())

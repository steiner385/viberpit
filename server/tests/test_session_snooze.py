"""Session snooze end to end on the orchestrator and the API
(docs/superpowers/specs/2026-11-06-session-snooze-design.md)."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server.sessions.orchestrator as orch_mod
from server.sessions.engine import AgentEngine
from server.sessions.remote import RemoteOrchestrator
from server.sessions.snooze import build_snooze
from server.sessions.types import SessionRecord

NOW = datetime.now(timezone.utc)


def _rec(sid="s1", status="idle", **kw) -> SessionRecord:
    return SessionRecord(id=sid, name="Wait for deploy", account="a",
                         engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80,
                         rows=24, cwd="/tmp", repo=None, status=status, **kw)


def _snooze(by="user", hours=2, reason="PR #9 to merge"):
    until = None if hours is None else (NOW + timedelta(hours=hours)).isoformat()
    return build_snooze(until=until, reason=reason, by=by, now=NOW)


class _Orch:
    """A bare SessionOrchestrator over an isolated DB, with the driver/prompt/push
    seams recorded instead of real."""

    def __init__(self, records, *, send_result="sent"):
        self.o = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
        self.o._records = {r.id: r for r in records}
        self.prompts: list[tuple[str, str, object]] = []
        self.pushes: list[tuple] = []
        self.send_result = send_result

        async def ensure_driver(sid, identity):
            return object()

        async def send_prompt(sid, text, *a, **k):
            # The snooze is cleared BEFORE the wake prompt goes out, so the turn it
            # starts can never end parked on the old snooze.
            self.prompts.append((sid, text, self.o._records[sid].snooze))
            return self.send_result
        self.o.ensure_driver = ensure_driver
        self.o.send_prompt = send_prompt


@pytest.fixture
def orch(isolated_db, monkeypatch):
    async def seed(*records, send_result="sent"):
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            for r in records:
                db.add(DbSession(id=r.id, name=r.name, account_name="a", cwd="/tmp",
                                 snooze=r.snooze))
        h = _Orch(records, send_result=send_result)

        async def notify(*a, **k):
            h.pushes.append((a, k))
        monkeypatch.setattr(orch_mod.push_mod, "notify_event", notify)
        return h
    return seed


async def _row_snooze(sid):
    from sqlalchemy import select

    from server.db import db_session
    from server.db.models import Session as DbSession
    async with db_session() as db:
        return (await db.execute(select(DbSession.snooze).where(DbSession.id == sid))).scalar_one()


def test_snooze_persists_on_record_and_row_and_survives_rehydrate(orch):
    async def run():
        h = await orch(_rec())
        s = _snooze()
        await h.o.snooze_session("s1", s, identity="u@example.com")
        assert h.o._records["s1"].snooze == s
        assert await _row_snooze("s1") == s
        from sqlalchemy import select

        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            row = (await db.execute(select(DbSession).where(DbSession.id == "s1"))).scalar_one()
        assert orch_mod.SessionOrchestrator._record_from_row(row).snooze == s
    asyncio.run(run())


def test_snooze_of_unknown_session_is_a_key_error(orch):
    async def run():
        h = await orch()
        with pytest.raises(KeyError):
            await h.o.snooze_session("nope", _snooze())
    asyncio.run(run())


def test_user_wake_clears_without_a_prompt_or_push(orch):
    async def run():
        h = await orch(_rec(snooze=_snooze()))
        assert await h.o.wake_session("s1", cause="user") is True
        assert h.o._records["s1"].snooze is None and await _row_snooze("s1") is None
        assert h.prompts == [] and h.pushes == []
        assert await h.o.wake_session("s1", cause="user") is False   # already awake
    asyncio.run(run())


def test_timer_wake_of_a_self_snooze_sends_the_wake_prompt_after_clearing(orch):
    async def run():
        h = await orch(_rec(snooze=_snooze(by="session")))
        assert await h.o.wake_session("s1", cause="timer") is True
        [(sid, text, snooze_at_send)] = h.prompts
        assert sid == "s1" and snooze_at_send is None
        assert text.startswith("<snooze-wake>") and "PR #9 to merge" in text
        assert h.pushes == []
    asyncio.run(run())


def test_a_person_prompting_a_self_snooze_is_the_wake_no_extra_prompt(orch):
    async def run():
        h = await orch(_rec(snooze=_snooze(by="session")))
        assert await h.o.wake_session("s1", cause="prompt") is True
        assert h.prompts == [] and h.o._records["s1"].snooze is None
    asyncio.run(run())


def test_timer_wake_during_a_drain_keeps_the_snooze_for_the_next_tick(orch):
    async def run():
        s = _snooze(by="session")
        h = await orch(_rec(snooze=s), send_result="draining")
        assert await h.o.wake_session("s1", cause="timer") is False
        assert h.o._records["s1"].snooze == s and await _row_snooze("s1") == s
    asyncio.run(run())


def test_undeliverable_wake_prompt_wakes_anyway_and_tells_the_person(orch):
    async def run():
        h = await orch(_rec(snooze=_snooze(by="session")), send_result="no_driver")
        assert await h.o.wake_session("s1", cause="timer") is True
        assert h.o._records["s1"].snooze is None
        [(args, _)] = h.pushes
        assert args[0] == "needs_input" and "could not be resumed" in args[2]
    asyncio.run(run())


def test_timer_ending_a_persons_snooze_pushes_a_needs_you(orch):
    async def run():
        h = await orch(_rec(snooze=_snooze(by="user")))
        await h.o.wake_session("s1", cause="timer")
        [(args, kw)] = h.pushes
        assert args[0] == "needs_input" and "snooze ended" in args[2]
        assert kw["session_id"] == "s1"
    asyncio.run(run())


def test_sweep_wakes_only_due_timed_snoozes_and_drops_ended_ones(orch):
    async def run():
        due = _rec("due", snooze=_snooze(hours=1))
        later = _rec("later", snooze=_snooze(hours=48))
        forever = _rec("forever", snooze=_snooze(hours=None))
        gone = _rec("gone", status="archived", snooze=_snooze(hours=1))
        awake = _rec("awake")
        h = await orch(due, later, forever, gone, awake)
        woken = await h.o._wake_due_snoozes_once(NOW + timedelta(hours=3))
        assert woken == 1
        assert h.o._records["due"].snooze is None
        assert h.o._records["later"].snooze and h.o._records["forever"].snooze
        assert h.o._records["gone"].snooze is None and h.pushes and len(h.pushes) == 1
    asyncio.run(run())


def test_a_snoozed_session_is_parked_on_either_engine():
    o = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    o._records = {"s1": _rec(snooze=_snooze())}
    assert asyncio.run(o._session_is_parked("s1")) is True


def test_completion_auto_archive_skips_a_snoozed_session():
    o = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    o._auto_archiving = set()
    rec = _rec(status="complete", snooze=_snooze())
    o._maybe_autoarchive_completed("s1", "complete", rec)
    assert o._auto_archiving == set()


def test_snooze_crosses_the_daemon_wire_and_the_proxy_has_both_methods():
    rec = _rec(snooze=_snooze(by="session"))
    wire = json.loads(json.dumps(rec.to_dict()))
    assert wire["snooze"] == rec.snooze
    assert SessionRecord.from_dict(wire).snooze == rec.snooze
    assert SessionRecord.from_dict(_rec().to_dict()).snooze is None
    assert hasattr(RemoteOrchestrator, "snooze_session")
    assert hasattr(RemoteOrchestrator, "wake_session")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": ""}


class _FakeSessions:
    def __init__(self):
        self.records = {"s1": _rec(), "q1": _rec("q1", status="queued")}
        self.calls: list[tuple] = []

    async def get_session(self, sid):
        return self.records.get(sid)

    async def snooze_session(self, sid, snooze, identity="system"):
        self.calls.append(("snooze", sid, snooze, identity))
        self.records[sid].snooze = snooze

    async def wake_session(self, sid, *, cause="user", identity="system"):
        self.calls.append(("wake", sid, cause, identity))
        self.records[sid].snooze = None
        return True


@pytest.fixture
def api():
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with patch.dict(os.environ, _ENV):
        real = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=real)
    fake = _FakeSessions()
    app.state.sessions = fake
    with patch.dict(os.environ, _ENV), TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake


def test_post_snooze_validates_and_returns_the_session_with_its_snooze(api):
    c, fake = api
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = c.post("/api/sessions/s1/snooze", json={"until": until, "reason": "deploy soak"},
               headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()["snooze"]
    assert body["by"] == "user" and body["reason"] == "deploy soak" and body["until"]
    [(kind, sid, _snooze_arg, identity)] = fake.calls
    assert (kind, sid, identity) == ("snooze", "s1", "testuser@example.com")


def test_post_snooze_indefinitely(api):
    c, _ = api
    r = c.post("/api/sessions/s1/snooze", json={"until": None}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["snooze"]["until"] is None


def test_post_snooze_refusals(api):
    c, fake = api
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert c.post("/api/sessions/s1/snooze", json={"until": past}, headers=_H).status_code == 422
    assert c.post("/api/sessions/q1/snooze", json={"until": None}, headers=_H).status_code == 409
    assert c.post("/api/sessions/zz/snooze", json={"until": None}, headers=_H).status_code == 404
    assert fake.calls == []


def test_delete_snooze_wakes_and_is_idempotent(api):
    c, fake = api
    fake.records["s1"].snooze = _snooze()
    r = c.delete("/api/sessions/s1/snooze", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["snooze"] is None
    assert fake.calls == [("wake", "s1", "user", "testuser@example.com")]
    assert c.delete("/api/sessions/s1/snooze", headers=_H).status_code == 200
    assert len(fake.calls) == 1          # already awake: no second wake
    assert c.delete("/api/sessions/zz/snooze", headers=_H).status_code == 404


# ---------------------------------------------------------------------------
# Session tool
# ---------------------------------------------------------------------------

def _call_tool(snooze_cb, args):
    from server.sessions.tools.context import SessionToolContext
    from server.sessions.tools.tools import build_session_tools
    ctx = SessionToolContext(session_id="s", identity="u", kind="coding", engine="claude_code",
                             record=lambda: None, signalling_tools=True, snooze=snooze_cb)
    tool = next(t for t in build_session_tools(ctx) if t.name == "snooze")
    return asyncio.run(tool.handler(args))


def test_snooze_tool_stores_a_session_snooze_and_says_end_the_turn():
    seen = []

    async def cb(s):
        seen.append(s)
    until = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    out = _call_tool(cb, {"until": until, "reason": "waiting for PR #9 to merge"})
    [s] = seen
    assert s["by"] == "session" and s["reason"] == "waiting for PR #9 to merge"
    data = json.loads(out["content"][0]["text"])["data"]
    assert data["snoozedUntil"] == s["until"] and "end your turn" in data["next"]


@pytest.mark.parametrize("args", [
    {"until": "garbage", "reason": "x"},
    {"until": (datetime.now(timezone.utc) + timedelta(days=45)).isoformat(), "reason": "x"},
    {"until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), "reason": ""},
])
def test_snooze_tool_refuses_bad_args_without_snoozing(args):
    seen = []

    async def cb(s):
        seen.append(s)
    assert _call_tool(cb, args).get("is_error")
    assert seen == []


def test_snooze_tool_is_mounted_with_the_signals_and_is_not_a_turn_signal():
    from server.sessions.tools.registry import TURN_SIGNAL_NAMES, mounted_names
    assert "snooze" in mounted_names(signalling_tools=True, self_model_switch=False)
    assert "snooze" not in mounted_names(signalling_tools=False, self_model_switch=False)
    assert "snooze" not in TURN_SIGNAL_NAMES

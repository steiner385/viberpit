"""Remote Control: a ViberPit Claude session also shows in the Claude web / desktop /
mobile apps (plan ``docs/superpowers/plans/2026-11-24-remote-control-sessions.md``).

- The driver sends ``{"subtype": "remote_control", "enabled": …}`` on the SDK's private
  control channel (the shape the real CLI answered in the 2026-11-24 spike) and maps
  every failure onto one ``REMOTE_CONTROL_CODES`` code logged at INFO.
- Effective = the session's override, else ``AppSettings.remote_control_default`` (NULL
  = on). Kill switch ``VIBERPIT_REMOTE_CONTROL_ENABLED``. Claude only; never a Copilot
  account or the Assistant drawer.
- Every spawn enables it in the background when effectively on; a failure never fails
  the spawn.

Only the CLI is faked — no ``claude`` ever runs. conftest turns the kill switch off for
the suite; the ``rc_on`` fixture turns it back on here.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from claude_agent_sdk._errors import CLIConnectionError
from fastapi.testclient import TestClient
from sqlalchemy import select

from viberpit.selector import Picked
from server.app import create_app
from server.db import db_session, dispose_db, init_db
from server.db.models import AppSettings
from server.db.models import Session as DbSession
from server.ipc.client import DaemonClient, DaemonError
from server.ipc.daemon import create_daemon_app
from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.sessions import remote_control as rc
from server.sessions.engine import AgentEngine
from server.sessions.facade import InProcessOrchestratorAdapter
from server.sessions.fleet_orchestrator import _MOVE_BLOCKED, _PER_SESSION, FleetOrchestrator
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.types import SessionRecord
from server.tests.ipc_harness import serve_daemon_uds
from server.tests.test_fast_mode import (FakeControl, Spawns, _control_error, _driver,
                                         _fake_usage, _no_title, _no_warnings, _payload,
                                         _seed_account, _timeout_error)

_H = {"Tailscale-User-Login": "testuser@example.com"}
URL = "https://claude.ai/code/session_abc"
ON_REPLY = {"session_url": URL, "connect_url": "https://claude.ai/connect/x",
            "environment_id": "env_1"}
SLOT_TOKEN = "sk-ant-oat01-SLOT-ACCESS"
SLOT_ENV = {"CLAUDE_CODE_OAUTH_TOKEN": SLOT_TOKEN, "CLAUDE_CODE_ORGANIZATION_UUID": "org-1"}


@pytest.fixture
def rc_on(monkeypatch):
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "true")
    rc._cache = None
    yield
    rc._cache = None


def _rc_sent(control: FakeControl) -> list[dict]:
    return [r for r in control.sent if r.get("subtype") == "remote_control"]


# ---------------------------------------------------------------- pure semantics


def test_effective_is_the_override_else_the_default() -> None:
    assert rc.effective(None, True) is True
    assert rc.effective(None, False) is False
    assert rc.effective(False, True) is False
    assert rc.effective(True, False) is True


def test_a_null_default_reads_as_on() -> None:
    assert rc.stored_default(None) is True
    assert rc.stored_default(True) is True
    assert rc.stored_default(False) is False


def test_session_url_comes_from_the_enable_reply() -> None:
    assert rc.session_url(ON_REPLY) == URL
    assert rc.session_url({}) is None
    assert rc.session_url(None) is None
    assert rc.session_url({"session_url": ""}) is None


def test_error_carries_its_code_and_rejects_unknown_ones() -> None:
    err = rc.RemoteControlError(rc.REMOTE_CONTROL_FAILED, "why")
    assert err.code == rc.REMOTE_CONTROL_FAILED and err.error_data == {"code": err.code}
    with pytest.raises(ValueError):
        rc.RemoteControlError("WAT")


def test_eligibility(rc_on, monkeypatch) -> None:
    assert rc.eligible(engine=AgentEngine.CLAUDE_CODE, kind=None, provider="anthropic")
    assert rc.eligible(engine=AgentEngine.CLAUDE_CODE, kind=None, provider=None)
    assert not rc.eligible(engine=AgentEngine.CODEX, kind=None, provider="openai")
    assert not rc.eligible(engine=AgentEngine.CLAUDE_CODE, kind=None, provider="copilot")
    assert not rc.eligible(engine=AgentEngine.CLAUDE_CODE, kind="assistant",
                           provider="anthropic")
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
    assert not rc.eligible(engine=AgentEngine.CLAUDE_CODE, kind=None, provider="anthropic")


# ---------------------------------------------------------------- driver


@pytest.mark.asyncio
async def test_driver_sends_the_raw_control_request(rc_on) -> None:
    control = FakeControl(ON_REPLY, {})
    d = _driver(control)
    await d.start()
    try:
        assert await d.set_remote_control(True) == ON_REPLY
        assert await d.set_remote_control(False) == {}
    finally:
        await d.aclose()
    assert _rc_sent(control) == [{"subtype": "remote_control", "enabled": True},
                                 {"subtype": "remote_control", "enabled": False}]
    assert control.timeouts[-2:] == [rc.REMOTE_CONTROL_TIMEOUT_S] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raised,code", [
    (_control_error("remote control is not available"), rc.REMOTE_CONTROL_REFUSED),
    (_timeout_error(), rc.REMOTE_CONTROL_FAILED),
    (CLIConnectionError("closed"), rc.REMOTE_CONTROL_NOT_LIVE),
    (RuntimeError("reader died"), rc.REMOTE_CONTROL_FAILED),
])
async def test_driver_failures_map_to_one_code_at_info(rc_on, raised, code, caplog) -> None:
    caplog.set_level(logging.INFO)
    d = _driver(FakeControl(raised))
    await d.start()
    try:
        with pytest.raises(rc.RemoteControlError) as ei:
            await d.set_remote_control(True)
    finally:
        await d.aclose()
    assert ei.value.code == code
    assert any("remote_control refused" in r.getMessage() for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_driver_not_live_or_no_channel(rc_on) -> None:
    d = _driver(FakeControl())
    with pytest.raises(rc.RemoteControlError) as before:
        await d.set_remote_control(True)
    assert before.value.code == rc.REMOTE_CONTROL_NOT_LIVE
    no_channel = _driver(None)
    await no_channel.start()
    try:
        with pytest.raises(rc.RemoteControlError) as ei:
            await no_channel.set_remote_control(True)
    finally:
        await no_channel.aclose()
    assert ei.value.code == rc.REMOTE_CONTROL_NOT_LIVE


@pytest.mark.asyncio
async def test_driver_kill_switch_refuses_enable_but_still_sends_disable(monkeypatch) -> None:
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
    control = FakeControl()
    d = _driver(control)
    await d.start()
    try:
        with pytest.raises(rc.RemoteControlError) as ei:
            await d.set_remote_control(True)
        assert await d.set_remote_control(False) == {}
    finally:
        await d.aclose()
    assert ei.value.code == rc.REMOTE_CONTROL_UNAVAILABLE
    assert _rc_sent(control) == [{"subtype": "remote_control", "enabled": False}]


# ---------------------------------------------------------------- orchestrator


@pytest_asyncio.fixture
async def env(tmp_path: Path, monkeypatch):
    """A real orchestrator, Claude driver and SQLite DB on the fake CLI. The kill switch
    is on; ``make`` creates a session (its spawn-time enable is awaited)."""
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "true")
    rc._cache = None
    store = tmp_path / "store"; store.mkdir()
    shared = tmp_path / "dot-claude"; (shared / "projects").mkdir(parents=True)
    (shared / "settings.json").write_text("{}")
    cfg_dir = tmp_path / "cfg" / "alpha"; cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text("{}")
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'rc.db'}"
    patches: list = [
        patch.dict(os.environ, {"VIBERPIT_HOME": str(store), "CLAUDE_CONFIG_DIR": str(shared),
                                "VIBERPIT_DB_URL": db_url}),
        patch("server.sessions.orchestrator.compose", return_value=cfg_dir),
        patch("server.sessions.orchestrator.configdir.trust_project"),
        patch("server.sessions.orchestrator.select_account", return_value=Picked("alpha")),
        patch("server.sessions.orchestrator.generate_title", _no_title),
        # The account has a usable claude.ai login: sessions launch on the slot token
        # (Task B), the only credential a bridge opens on. Login-less cases patch these.
        patch("server.sessions.orchestrator.remote_control_env_for",
              side_effect=lambda account, **kw: dict(SLOT_ENV)),
        patch("server.sessions.orchestrator.remote_control_readiness", return_value=None),
    ]
    for p in patches:
        p.start()
    _seed_account("alpha")
    await init_db(db_url=db_url)
    control = FakeControl()
    spawns = Spawns(control)
    orch = SessionOrchestrator(sdk_factory=spawns, poll_interval=9999.0,
                               usage_fn=_fake_usage, repo_root=str(tmp_path))

    async def make(name: str = "RC", **kw) -> SessionRecord:
        rec = await orch.create_session(name, cwd=str(tmp_path), permission_mode="full-auto",
                                        **kw)
        await settle(rec.id)
        return rec

    async def settle(session_id: str) -> None:
        task = orch._remote_control_task_map().get(session_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    try:
        yield SimpleNamespace(orch=orch, control=control, make=make, settle=settle)
    finally:
        await orch.stop()
        await dispose_db()
        for p in reversed(patches):
            p.stop()
        rc._cache = None


async def _row(sid: str) -> DbSession:
    async with db_session() as db:
        return (await db.execute(select(DbSession).where(DbSession.id == sid))).scalar_one()


async def _set_default(value: bool | None) -> None:
    async with db_session() as db:
        row = (await db.execute(select(AppSettings).where(
            AppSettings.id == "singleton"))).scalar_one_or_none()
        if row is None:
            row = AppSettings(id="singleton")
            db.add(row)
        row.remote_control_default = value
    rc._cache = None


@pytest.mark.asyncio
async def test_a_spawn_enables_remote_control_by_default_and_stores_the_url(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    assert _rc_sent(env.control) == [{"subtype": "remote_control", "enabled": True}]
    assert rec.remote_control_url == URL and rec.remote_control_available is True
    assert (await _row(rec.id)).remote_control_url == URL
    p = _payload(rec)
    assert p["remoteControl"] is True and p["remoteControlOverride"] is None
    assert p["remoteControlUrl"] == URL and p["remoteControlAvailable"] is True


@pytest.mark.asyncio
async def test_a_spawn_sends_nothing_when_the_default_is_off(env) -> None:
    await _set_default(False)
    rec = await env.make()
    assert _rc_sent(env.control) == []
    assert rec.remote_control_url is None
    assert _payload(rec)["remoteControl"] is False


@pytest.mark.asyncio
async def test_a_spawn_sends_nothing_with_the_kill_switch_off(env, monkeypatch) -> None:
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
    rec = await env.make()
    assert _rc_sent(env.control) == []
    assert rec.remote_control_available is False
    p = _payload(rec)
    assert p["remoteControlAvailable"] is False and p["remoteControl"] is False


@pytest.mark.asyncio
async def test_a_copilot_session_is_never_enabled(env) -> None:
    with patch("server.sessions.orchestrator._provider_of", return_value="copilot"):
        rec = await env.make()
        assert env.orch.remote_control_available(rec.id) is False
        with pytest.raises(rc.RemoteControlError) as ei:
            await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_UNAVAILABLE
    assert _rc_sent(env.control) == []


@pytest.mark.asyncio
async def test_a_respawn_onto_an_ineligible_account_drops_the_stale_url(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    assert rec.remote_control_url == URL
    with patch("server.sessions.orchestrator._provider_of", return_value="copilot"):
        env.orch._schedule_remote_control(rec.id)
        await asyncio.gather(*list(env.orch._bg_tasks), return_exceptions=True)
    assert rec.remote_control_url is None
    assert (await _row(rec.id)).remote_control_url is None
    assert len(_rc_sent(env.control)) == 1


@pytest.mark.asyncio
async def test_a_failed_spawn_enable_is_logged_and_never_fails_the_spawn(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [_control_error("not logged in to claude.ai")]
    rec = await env.make()
    assert rec.id in env.orch._drivers
    assert rec.remote_control_url is None
    assert any("session.remote_control_failed" in r.getMessage() for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_resume_enables_it_again(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers.pop(rec.id)
    await driver.aclose()
    env.control.replies = [{"session_url": URL + "2"}]
    await env.orch.ensure_driver(rec.id)
    await env.settle(rec.id)
    assert _rc_sent(env.control) == [{"subtype": "remote_control", "enabled": True}] * 2
    assert (await _row(rec.id)).remote_control_url == URL + "2"


@pytest.mark.asyncio
async def test_set_remote_control_off_then_on_live(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    env.control.replies = [{}]
    assert await env.orch.set_remote_control(rec.id, False) == {"enabled": False,
                                                                 "url": None}
    row = await _row(rec.id)
    assert row.remote_control is False and row.remote_control_url is None
    assert _payload(rec)["remoteControl"] is False
    assert _payload(rec)["remoteControlOverride"] is False
    env.control.replies = [ON_REPLY]
    assert await env.orch.set_remote_control(rec.id, True) == {"enabled": True, "url": URL}
    row = await _row(rec.id)
    assert row.remote_control is True and row.remote_control_url == URL
    assert [r["enabled"] for r in _rc_sent(env.control)] == [True, False, True]
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_an_override_beats_the_default_at_spawn(env) -> None:
    await _set_default(False)
    rec = await env.make()
    env.control.replies = [ON_REPLY]
    await env.orch.set_remote_control(rec.id, True)
    driver = env.orch._drivers.pop(rec.id)
    await driver.aclose()
    env.control.replies = [ON_REPLY]
    await env.orch.ensure_driver(rec.id)
    await env.settle(rec.id)
    assert [r["enabled"] for r in _rc_sent(env.control)] == [True, True]
    p = _payload(rec)
    assert p["remoteControl"] is True and p["remoteControlOverride"] is True


@pytest.mark.asyncio
async def test_an_override_off_suppresses_the_spawn_enable(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    env.control.replies = [{}]
    await env.orch.set_remote_control(rec.id, False)
    driver = env.orch._drivers.pop(rec.id)
    await driver.aclose()
    await env.orch.ensure_driver(rec.id)
    await env.settle(rec.id)
    assert [r["enabled"] for r in _rc_sent(env.control)] == [True, False]


@pytest.mark.asyncio
async def test_a_driverless_session_persists_for_the_next_spawn(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers.pop(rec.id)
    await driver.aclose()
    assert await env.orch.set_remote_control(rec.id, False) == {"enabled": False,
                                                                 "url": None}
    assert (await _row(rec.id)).remote_control is False
    assert await env.orch.set_remote_control(rec.id, True) == {"enabled": True, "url": None}
    assert (await _row(rec.id)).remote_control is True
    assert [r["enabled"] for r in _rc_sent(env.control)] == [True]   # spawn only


@pytest.mark.asyncio
async def test_a_refused_live_enable_raises_and_leaves_no_url(env) -> None:
    await _set_default(False)
    rec = await env.make()
    env.control.replies = [_control_error("nope")]
    with pytest.raises(rc.RemoteControlError) as ei:
        await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_REFUSED
    row = await _row(rec.id)
    assert row.remote_control is True and row.remote_control_url is None


@pytest.mark.asyncio
async def test_unknown_session_is_a_keyerror(env) -> None:
    with pytest.raises(KeyError):
        await env.orch.set_remote_control("nope", True)


@pytest.mark.asyncio
async def test_the_assistant_session_is_unavailable(env) -> None:
    rec = await env.make()
    rec.kind = "assistant"
    assert env.orch.remote_control_available(rec.id) is False


@pytest.mark.asyncio
async def test_the_override_and_url_survive_a_rehydrate(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    env.control.replies = [ON_REPLY]
    await env.orch.set_remote_control(rec.id, True)
    row = await _row(rec.id)
    back = SessionOrchestrator._record_from_row(row)
    assert back.remote_control is True and back.remote_control_url == URL


# ---------------------------------------------------------------- RPC / fleet / facade


class _RecordingClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, *, timeout=None, **kwargs):
        self.calls.append((method, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_remote_forwards_and_rebuilds_errors() -> None:
    client = _RecordingClient(result={"enabled": True, "url": URL})
    assert await RemoteOrchestrator(client).set_remote_control("s", True, identity="u") == {
        "enabled": True, "url": URL}
    assert client.calls == [("set_remote_control",
                             {"session_id": "s", "enabled": True, "identity": "u"})]
    for code in sorted(rc.REMOTE_CONTROL_CODES):
        with pytest.raises(rc.RemoteControlError) as ei:
            await RemoteOrchestrator(_RecordingClient(error=DaemonError(
                "RemoteControlError", "why", {"code": code}))).set_remote_control("s", True)
        assert ei.value.code == code
    with pytest.raises(KeyError):
        await RemoteOrchestrator(_RecordingClient(
            error=DaemonError("KeyError", "s"))).set_remote_control("s", True)
    with pytest.raises(DaemonError):
        await RemoteOrchestrator(_RecordingClient(error=DaemonError(
            "RemoteControlError", "x", {"code": "WAT"}))).set_remote_control("s", True)


@pytest.mark.asyncio
async def test_uds_round_trip_on_the_owner_daemon(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    app = create_daemon_app()
    register_orchestrator_rpc(app, env.orch)
    async with serve_daemon_uds(app) as sock:
        client = DaemonClient(sock)
        try:
            ro = RemoteOrchestrator(client)
            env.control.replies = [{}]
            assert await ro.set_remote_control(rec.id, False) == {"enabled": False,
                                                                  "url": None}
            with patch("server.sessions.orchestrator._provider_of", return_value="copilot"):
                with pytest.raises(rc.RemoteControlError) as refused:
                    await ro.set_remote_control(rec.id, True)
        finally:
            await client.aclose()
    assert refused.value.code == rc.REMOTE_CONTROL_UNAVAILABLE


@pytest.mark.asyncio
async def test_fleet_routes_to_the_owning_host_and_facade_forwards() -> None:
    assert "set_remote_control" in _PER_SESSION
    assert "set_remote_control" in _MOVE_BLOCKED

    class _Host:
        def __init__(self, name):
            self.name, self.calls = name, []

        async def set_remote_control(self, session_id, enabled, **kw):
            self.calls.append((session_id, enabled, kw))
            return {"enabled": enabled, "url": None, "host": self.name}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky = _Host("dobby"), _Host("rocky")
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky}), primary="dobby")  # type: ignore[arg-type]
    fo._host_cache["s"] = "rocky"
    assert (await fo.set_remote_control("s", True, identity="u"))["host"] == "rocky"
    assert dobby.calls == []
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert (await adapter.set_remote_control("s", False))["host"] == "dobby"


def test_the_orchestrator_method_signatures_agree() -> None:
    own = inspect.signature(SessionOrchestrator.set_remote_control).parameters
    proxy = inspect.signature(RemoteOrchestrator.set_remote_control).parameters
    assert list(own) == list(proxy)


# ---------------------------------------------------------------- HTTP

_ENV = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
        "VIBERPIT_TAILNET_ALLOWLIST": "", "VIBERPIT_REMOTE_CONTROL_ENABLED": "true"}
SID = "66666666-6666-4666-8666-666666666666"


def _client():
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=Spawns(FakeControl()), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    orch._records[SID] = SessionRecord(id=SID, name="n", account="a",
                                       engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                                       cols=80, rows=24, cwd=None)  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def _post(c, body, sid=SID):
    return c.post(f"/api/sessions/{sid}/remote-control", json=body, headers=_H)


def test_http_remote_control_is_200_with_enabled_and_url() -> None:
    async def fake(self, session_id, enabled, *, identity="system"):
        assert (session_id, enabled, identity) == (SID, True, "testuser@example.com")
        return {"enabled": True, "url": URL}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_remote_control", fake), \
            patch("server.api.sessions.record_audit") as audit:
        r = _post(c, {"enabled": True})
    assert r.status_code == 200 and r.json() == {"enabled": True, "url": URL}
    assert audit.call_args.kwargs["type"] == "session.remote_control"


@pytest.mark.parametrize("code", sorted(rc.REMOTE_CONTROL_CODES))
def test_http_refusals_are_409_with_their_code(code: str) -> None:
    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_remote_control",
                         side_effect=rc.RemoteControlError(code, "why")):
        r = _post(c, {"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == code


def test_http_unknown_session_old_daemon_and_bad_body() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert _post(c, {"enabled": False}, sid="nope").status_code == 404
        assert _post(c, {"enabled": "maybe"}).status_code == 422
        with patch.object(SessionOrchestrator, "set_remote_control",
                          side_effect=DaemonError("UnknownMethod", "set_remote_control")):
            r = _post(c, {"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "daemon_outdated"


def test_http_requires_identity() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        assert c.post(f"/api/sessions/{SID}/remote-control",
                      json={"enabled": True}).status_code == 401


def test_settings_round_trip_the_default_and_the_capability() -> None:
    with patch.dict(os.environ, _ENV), _client() as c:
        before = c.get("/api/settings", headers=_H).json()
        off = c.put("/api/settings", json={"remoteControlDefault": False}, headers=_H).json()
        again = c.get("/api/settings", headers=_H).json()
        untouched = c.put("/api/settings", json={"thinkingCollapsed": True},
                          headers=_H).json()
        on = c.put("/api/settings", json={"remoteControlDefault": True}, headers=_H).json()
    assert before["remoteControlDefault"] is True and before["remoteControlCapability"] is True
    assert off["remoteControlDefault"] is False and again["remoteControlDefault"] is False
    assert untouched["remoteControlDefault"] is False
    assert on["remoteControlDefault"] is True
    rc._cache = None


def test_settings_hide_the_capability_with_the_kill_switch_off() -> None:
    with patch.dict(os.environ, {**_ENV, "VIBERPIT_REMOTE_CONTROL_ENABLED": "false"}), \
            _client() as c:
        body = c.get("/api/settings", headers=_H).json()
    assert body["remoteControlCapability"] is False


def test_payload_follows_the_cached_default() -> None:
    rec: Any = SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                             engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    rec.remote_control_available = True
    with patch.dict(os.environ, _ENV):
        rc.remember_default(False)
        try:
            assert _payload(rec)["remoteControl"] is False
            rec.remote_control = True
            assert _payload(rec)["remoteControl"] is True
            rc.remember_default(True)
            rec.remote_control = None
            assert _payload(rec)["remoteControl"] is True
        finally:
            rc._cache = None


# ── Person-only enable: Remote Control opens a path around ViberPit's auth ─────────────

def _as_via(monkeypatch, via: str) -> None:
    from server.auth import identity
    monkeypatch.setattr(identity, "human_gate_via", lambda _req: via)
    monkeypatch.setattr("server.api.sessions.human_gate_via", lambda _req: via)


def _as_local_agent(monkeypatch) -> None:
    # A process on the ViberPit host asserting an identity, WITHOUT the operator's
    # VIBERPIT_RULES_TRUST_LOCAL_HOST opt-in: not provably a person.
    from server.auth import identity
    _as_via(monkeypatch, identity.LOCAL_PROCESS_VIA)


def test_the_trusted_local_browser_may_enable_and_set_the_default(monkeypatch) -> None:
    # With the operator's local-host opt-in (LOCAL_TRUSTED_VIA), the browser on the
    # ViberPit host itself is admitted, as it is for rules.
    from server.auth import identity
    _as_via(monkeypatch, identity.LOCAL_TRUSTED_VIA)

    async def fake(self, session_id, enabled, *, identity="system"):
        return {"enabled": enabled, "url": None}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_remote_control", fake), \
            patch("server.api.sessions.record_audit"):
        on = _post(c, {"enabled": True})
        c.put("/api/settings", json={"remoteControlDefault": False}, headers=_H)
        default_on = c.put("/api/settings", json={"remoteControlDefault": True}, headers=_H)
    assert on.status_code == 200
    assert default_on.status_code == 200 and default_on.json()["remoteControlDefault"] is True
    rc._cache = None


def test_a_non_person_cannot_enable_but_can_disable(monkeypatch) -> None:
    _as_local_agent(monkeypatch)
    calls: list[bool] = []

    async def fake(self, session_id, enabled, *, identity="system"):
        calls.append(enabled)
        return {"enabled": enabled, "url": None}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "set_remote_control", fake), \
            patch("server.api.sessions.record_audit"):
        on = _post(c, {"enabled": True})
        off = _post(c, {"enabled": False})
    assert on.status_code == 403
    assert on.json()["detail"]["code"] == "SESSION_CONTROL_HUMAN_ONLY"
    assert off.status_code == 200 and calls == [False]


def test_a_non_person_cannot_turn_the_default_on_but_can_turn_it_off(monkeypatch) -> None:
    _as_local_agent(monkeypatch)
    with patch.dict(os.environ, _ENV), _client() as c:
        off = c.put("/api/settings", json={"remoteControlDefault": False}, headers=_H)
        on = c.put("/api/settings", json={"remoteControlDefault": True}, headers=_H)
        unchanged_off = c.put("/api/settings", json={"remoteControlDefault": False},
                              headers=_H)
    assert off.status_code == 200 and off.json()["remoteControlDefault"] is False
    assert on.status_code == 403
    assert unchanged_off.status_code == 200
    rc._cache = None


# ── Fail closed: an UNKNOWN app default never opens a bridge ────────────────────────────


@pytest.mark.asyncio
async def test_an_unreadable_default_with_nothing_cached_reads_as_off(rc_on, caplog) -> None:
    caplog.set_level(logging.INFO)
    rc._cache = None
    with patch("server.db.db_session", side_effect=RuntimeError("db down")):
        assert await rc.read_default() is False
        assert await rc.refresh_default() is False
    assert rc.cached_default() is None          # nothing was learned
    assert any("remote_control default" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_an_unreadable_default_keeps_the_last_known_value(rc_on) -> None:
    rc.remember_default(True)
    with patch("server.db.db_session", side_effect=RuntimeError("db down")):
        assert await rc.read_default() is True


@pytest.mark.asyncio
async def test_a_spawn_opens_no_bridge_when_the_default_is_unknown(env) -> None:
    rc._cache = None
    with patch("server.db.db_session", side_effect=RuntimeError("db down")):
        assert await rc.read_default() is False
    with patch("server.sessions.remote_control.read_default", return_value=False):
        rec = await env.make()
    assert _rc_sent(env.control) == []
    assert rec.remote_control_url is None


def test_the_payload_does_not_claim_on_before_the_default_was_read() -> None:
    rec: Any = SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                             engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    rec.remote_control_available = True
    with patch.dict(os.environ, _ENV):
        rc._cache = None
        assert _payload(rec)["remoteControl"] is False
        rec.remote_control = True                 # an explicit override needs no default
        assert _payload(rec)["remoteControl"] is True


# ── Revocation: the default turned off tears down bridges of sessions that follow it ───


@pytest.mark.asyncio
async def test_default_off_tears_down_followers_and_keeps_overrides(env) -> None:
    env.control.replies = [ON_REPLY]
    follower = await env.make("follower")
    env.control.replies = [ON_REPLY]
    pinned = await env.make("pinned")
    env.control.replies = [ON_REPLY]
    await env.orch.set_remote_control(pinned.id, True)
    sent_before = len(_rc_sent(env.control))
    env.control.replies = [{}]
    result = await env.orch.apply_remote_control_default(False)
    assert result == {"sessions": 1, "failed": 0}
    assert _rc_sent(env.control)[sent_before:] == [{"subtype": "remote_control",
                                                    "enabled": False}]
    assert follower.remote_control is None            # no override was written
    assert follower.remote_control_url is None
    row = await _row(follower.id)
    assert row.remote_control is None and row.remote_control_url is None
    assert pinned.remote_control_url == URL           # an explicit override is untouched


@pytest.mark.asyncio
async def test_default_off_tears_down_even_with_the_kill_switch_off(env, monkeypatch) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
    env.control.replies = [{}]
    assert (await env.orch.apply_remote_control_default(False))["sessions"] == 1
    assert _rc_sent(env.control)[-1] == {"subtype": "remote_control", "enabled": False}
    assert rec.remote_control_url is None


@pytest.mark.asyncio
async def test_default_on_opens_bridges_for_followers(env) -> None:
    env.control.replies = [_timeout_error()]
    rec = await env.make()                    # on the claude.ai login, but no bridge yet
    assert rec.launch_credential == "slot" and rec.remote_control_url is None
    env.control.replies = [ON_REPLY]
    assert (await env.orch.apply_remote_control_default(True))["sessions"] == 1
    assert rec.remote_control_url == URL and rec.remote_control is None


@pytest.mark.asyncio
async def test_default_off_revokes_followers_onto_the_setup_token(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()                    # launched on the claude.ai login
    first = env.orch._drivers[rec.id]
    env.control.replies = [{}]
    await env.orch.apply_remote_control_default(False)
    assert rec.remote_control_url is None
    assert env.orch._drivers[rec.id] is not first          # respawned...
    assert rec.launch_credential != "slot"                  # ...off the login


@pytest.mark.asyncio
async def test_default_on_skips_followers_running_on_the_setup_token(env) -> None:
    """A follower launched while the default was off runs on the setup-token, which no
    bridge opens on: a default change never respawns it; its next spawn uses the login."""
    await _set_default(False)
    rec = await env.make()
    assert rec.launch_credential != "slot"   # no setup-token seeded here: "cfg"
    assert (await env.orch.apply_remote_control_default(True))["sessions"] == 0
    assert _rc_sent(env.control) == []
    await _set_default(True)
    driver = env.orch._drivers.pop(rec.id)
    await driver.aclose()
    env.control.replies = [ON_REPLY]
    await env.orch.ensure_driver(rec.id)
    await env.settle(rec.id)
    assert rec.launch_credential == "slot" and rec.remote_control_url == URL


@pytest.mark.asyncio
async def test_a_failed_teardown_is_counted_and_logged_not_raised(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    env.control.replies = [_control_error("busy")]
    assert await env.orch.apply_remote_control_default(False) == {"sessions": 0,
                                                                   "failed": 1}
    # The teardown request failed, but the revocation still moved the session off the
    # login, and its bridge died with the old CLI.
    assert rec.launch_credential != "slot" and rec.remote_control_url is None
    assert any("remote_control_default_failed" in r.getMessage() for r in caplog.records)
    assert _no_warnings(caplog) == []


@pytest.mark.asyncio
async def test_default_off_reaps_an_in_flight_spawn_enable(env) -> None:
    env.control.gate = asyncio.Event()
    env.control.replies = [ON_REPLY, {}]
    rec = await env.orch.create_session("gated", cwd="/tmp", permission_mode="full-auto")
    for _ in range(50):
        await asyncio.sleep(0)
    task = env.orch._remote_control_task_map().get(rec.id)
    assert task is not None and not task.done()
    env.control.gate.set()
    await env.orch.apply_remote_control_default(False)
    assert rec.id not in env.orch._remote_control_task_map()
    assert rec.remote_control_url is None


@pytest.mark.asyncio
async def test_remote_and_fleet_fan_out_the_default_to_every_host(caplog) -> None:
    client = _RecordingClient(result={"sessions": 2, "failed": 0})
    assert await RemoteOrchestrator(client).apply_remote_control_default(False) == {
        "sessions": 2, "failed": 0}
    assert client.calls == [("apply_remote_control_default", {"enabled": False})]

    class _Host:
        def __init__(self, name, boom=False):
            self.name, self.boom, self.calls = name, boom, []

        async def apply_remote_control_default(self, enabled):
            self.calls.append(enabled)
            if self.boom:
                raise RuntimeError("host down")
            return {"sessions": 1, "failed": 0}

    class _Router:
        def __init__(self, orchs):
            self._orchs = orchs

        def get(self, name):
            return self._orchs[name]

        def names(self):
            return sorted(self._orchs)

    dobby, rocky, gone = _Host("dobby"), _Host("rocky"), _Host("gone", boom=True)
    fo = FleetOrchestrator(_Router({"dobby": dobby, "rocky": rocky, "gone": gone}),  # type: ignore[arg-type]
                           primary="dobby")
    caplog.set_level(logging.INFO)
    out = await fo.apply_remote_control_default(False)
    assert out == {"sessions": 2, "failed": 0, "failedHosts": ["gone"]}
    assert dobby.calls == rocky.calls == gone.calls == [False]
    adapter = InProcessOrchestratorAdapter(dobby)  # type: ignore[arg-type]
    assert await adapter.apply_remote_control_default(True) == {"sessions": 1, "failed": 0}


def test_settings_put_applies_a_changed_default_to_live_sessions() -> None:
    calls: list[bool] = []

    async def fake(self, enabled):
        calls.append(enabled)
        return {"sessions": 0, "failed": 0}

    with patch.dict(os.environ, _ENV), _client() as c, \
            patch.object(SessionOrchestrator, "apply_remote_control_default", fake):
        c.put("/api/settings", json={"remoteControlDefault": False}, headers=_H)
        c.put("/api/settings", json={"remoteControlDefault": False}, headers=_H)  # unchanged
        c.put("/api/settings", json={"thinkingCollapsed": True}, headers=_H)
        c.put("/api/settings", json={"remoteControlDefault": True}, headers=_H)
    assert calls == [False, True]
    rc._cache = None


# ── Runtime truth: an account whose login the CLI rejects (inference-only setup-token) ──

# What the real CLI answered in production for a setup-token session (2026-11-24).
LOGIN_REJECTED = "Claude.ai login was rejected — run /login, then /remote-control"


def test_refusal_reason_reads_the_cli_message() -> None:
    assert rc.refusal_reason(LOGIN_REJECTED) == rc.REASON_LOGIN_REJECTED
    assert rc.refusal_reason("something else") == rc.REASON_REFUSED
    assert rc.LOGIN_REJECTED_MESSAGE.startswith("claude.ai rejected this account's login")
    assert rc.refusal_message(rc.REASON_LOGIN_NEEDED) == rc.LOGIN_NEEDED_MESSAGE
    assert rc.refusal_message(rc.REASON_RUNNER) == rc.RUNNER_MESSAGE


@pytest.mark.asyncio
async def test_a_rejected_login_at_spawn_is_recorded_and_not_live(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    rec = await env.make()
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED
    assert rec.remote_control_url is None
    p = _payload(rec)
    assert p["remoteControlUnavailableReason"] == rc.REASON_LOGIN_REJECTED
    assert p["remoteControlAvailable"] is True        # static eligibility is unchanged
    assert p["remoteControl"] is False                 # but it is not live


@pytest.mark.asyncio
async def test_a_rejected_account_is_not_retried_on_later_spawns(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    await env.make("first")
    second = await env.make("second")
    assert len(_rc_sent(env.control)) == 1             # the second spawn sent nothing
    assert second.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED


@pytest.mark.asyncio
async def test_a_generic_refusal_does_not_block_the_account(env) -> None:
    # Only a rejected login is a property of the credential; anything else may be
    # transient, so the next spawn on the same account tries again.
    env.control.replies = [_control_error("bridge busy, try later"), {"session_url": URL}]
    first = await env.make("first")
    second = await env.make("second")
    assert first.remote_control_unavailable_reason == rc.REASON_REFUSED
    assert len(_rc_sent(env.control)) == 2
    assert second.remote_control_unavailable_reason is None
    assert second.remote_control_url == URL


@pytest.mark.asyncio
async def test_a_toggle_on_for_a_rejected_account_is_refused_without_sending(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    rec = await env.make()
    with pytest.raises(rc.RemoteControlError) as ei:
        await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_REFUSED
    assert str(ei.value) == rc.LOGIN_REJECTED_MESSAGE
    assert len(_rc_sent(env.control)) == 1
    assert (await _row(rec.id)).remote_control is None   # nothing was changed
    # Turning it off is always allowed.
    env.control.replies = [{}]
    assert (await env.orch.set_remote_control(rec.id, False))["enabled"] is False


@pytest.mark.asyncio
async def test_a_live_toggle_refusal_records_the_reason_and_the_account(env) -> None:
    await _set_default(False)
    rec = await env.make()
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    with pytest.raises(rc.RemoteControlError) as ei:
        await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_REFUSED
    assert str(ei.value) == rc.LOGIN_REJECTED_MESSAGE
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED
    other = await env.make("other")
    with pytest.raises(rc.RemoteControlError):
        await env.orch.set_remote_control(other.id, True)
    assert len(_rc_sent(env.control)) == 1


@pytest.mark.asyncio
async def test_default_on_skips_a_rejected_account(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    await env.make()
    assert await env.orch.apply_remote_control_default(True) == {"sessions": 0, "failed": 0}
    assert len(_rc_sent(env.control)) == 1


@pytest.mark.asyncio
async def test_a_spawn_on_another_account_clears_a_stale_reason(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    rec = await env.make()
    env.orch._remote_control_refused_accounts().pop("alpha")   # e.g. a daemon restart
    env.control.replies = [ON_REPLY]
    env.orch._schedule_remote_control(rec.id)
    await env.settle(rec.id)
    assert rec.remote_control_unavailable_reason is None
    assert rec.remote_control_url == URL


def test_the_reason_rides_the_daemon_to_edge_copy_and_forces_off() -> None:
    rec: Any = SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                             engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    rec.remote_control_available = True
    rec.remote_control = True
    rec.remote_control_unavailable_reason = rc.REASON_LOGIN_REJECTED
    back = SessionRecord.from_dict(rec.to_dict())
    assert back.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED
    with patch.dict(os.environ, _ENV):
        p = _payload(rec)
    assert p["remoteControl"] is False
    assert p["remoteControlUnavailableReason"] == rc.REASON_LOGIN_REJECTED


# ── Task B: launch on the account's claude.ai login, rotate its token live ──────────────

_ORCH = "server.sessions.orchestrator"


def _capture_spawn_env(orch) -> list[dict]:
    """Record the ``extra_env`` of every spawn the orchestrator's factory makes."""
    seen: list[dict] = []
    base = orch._sdk_factory

    @functools.wraps(base)
    def wrapped(*a, extra_env=None, **kw):
        seen.append(dict(extra_env or {}))
        return base(*a, extra_env=extra_env, **kw)
    orch._sdk_factory = wrapped
    return seen


@pytest.mark.asyncio
async def test_a_remote_control_spawn_launches_on_the_slot_token_and_org(env) -> None:
    seen = _capture_spawn_env(env.orch)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    assert seen[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == SLOT_TOKEN
    assert seen[-1]["CLAUDE_CODE_ORGANIZATION_UUID"] == "org-1"
    assert rec.launch_credential == "slot"
    assert env.orch._session_uses_setup_token(rec) is False
    assert rec.remote_control_url == URL


@pytest.mark.asyncio
async def test_with_remote_control_off_the_spawn_stays_on_the_setup_token(env) -> None:
    await _set_default(False)
    seen = _capture_spawn_env(env.orch)
    with patch("server.sessions.account_auth.crypto") as crypto:
        crypto.read_oauth_token.return_value = "sk-ant-oat01-SETUP"
        rec = await env.make()
    assert seen[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-SETUP"
    assert "CLAUDE_CODE_ORGANIZATION_UUID" not in seen[-1]
    assert rec.launch_credential == "setup"
    assert rec.remote_control_unavailable_reason is None


@pytest.mark.asyncio
async def test_no_usable_login_launches_on_the_setup_token_as_login_needed(env) -> None:
    with patch(f"{_ORCH}.remote_control_env_for", return_value=None), \
            patch(f"{_ORCH}.remote_control_readiness", return_value="login_needed"):
        rec = await env.make()
        assert rec.launch_credential != "slot"
        assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_NEEDED
        assert _rc_sent(env.control) == []           # no doomed enable
        p = _payload(rec)
        assert p["remoteControl"] is False
        assert p["remoteControlUnavailableReason"] == rc.REASON_LOGIN_NEEDED
        with pytest.raises(rc.RemoteControlError) as ei:
            await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_REFUSED
    assert str(ei.value) == rc.LOGIN_NEEDED_MESSAGE
    assert (await _row(rec.id)).remote_control is None    # nothing was changed
    assert _rc_sent(env.control) == []


@pytest.mark.asyncio
async def test_login_needed_lifts_once_the_account_signs_in_again(env) -> None:
    with patch(f"{_ORCH}.remote_control_env_for", return_value=None), \
            patch(f"{_ORCH}.remote_control_readiness", return_value="login_needed"):
        rec = await env.make()
        await env.orch._recheck_remote_control_logins()
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_NEEDED
    await env.orch._recheck_remote_control_logins()          # the slot is ready now
    assert rec.remote_control_unavailable_reason is None
    # ...and the toggle respawns the session onto the login and opens the bridge.
    env.control.replies = [ON_REPLY]
    assert await env.orch.set_remote_control(rec.id, True) == {"enabled": True, "url": URL}
    assert rec.launch_credential == "slot"


@pytest.mark.asyncio
async def test_toggle_on_respawns_a_setup_token_session_onto_the_login(env) -> None:
    await _set_default(False)
    rec = await env.make()
    assert rec.launch_credential != "slot"
    first = env.orch._drivers[rec.id]
    env.control.replies = [ON_REPLY]
    assert await env.orch.set_remote_control(rec.id, True) == {"enabled": True, "url": URL}
    assert env.orch._drivers[rec.id] is not first           # respawned
    assert rec.launch_credential == "slot"
    assert _rc_sent(env.control) == [{"subtype": "remote_control", "enabled": True}]


@pytest.mark.asyncio
async def test_toggle_on_mid_turn_is_busy_and_does_not_respawn(env) -> None:
    await _set_default(False)
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    rec.status = "working"
    with pytest.raises(rc.RemoteControlError) as ei:
        await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_BUSY
    assert env.orch._drivers[rec.id] is first
    assert _rc_sent(env.control) == []
    # Nothing was persisted: the choice applies only once the session runs on the login.
    assert rec.remote_control is None
    assert (await _row(rec.id)).remote_control is None


@pytest.mark.asyncio
async def test_toggle_on_while_the_login_is_pinned_is_login_busy_without_a_respawn(env) -> None:
    """The login is fine but its token is stale and a live session owns its family: the
    launch could not use it, so the toggle refuses up front instead of respawning the
    session onto the setup-token and then saying "Sign in"."""
    await _set_default(False)
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    with patch(f"{_ORCH}.remote_control_readiness", return_value="login_busy"):
        with pytest.raises(rc.RemoteControlError) as ei:
            await env.orch.set_remote_control(rec.id, True)
    assert ei.value.code == rc.REMOTE_CONTROL_REFUSED
    assert str(ei.value) == rc.LOGIN_BUSY_MESSAGE
    assert env.orch._drivers[rec.id] is first
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_BUSY
    assert (await _row(rec.id)).remote_control is None


@pytest.mark.asyncio
async def test_a_spawn_pinned_out_of_the_login_reports_login_busy(env) -> None:
    with patch(f"{_ORCH}.remote_control_env_for", return_value=None), \
            patch(f"{_ORCH}.remote_control_readiness", return_value="login_busy"):
        rec = await env.make()
    assert rec.launch_credential != "slot"
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_BUSY
    await env.orch._recheck_remote_control_logins()       # the owner went away
    assert rec.remote_control_unavailable_reason is None


def _plant_cfg_owner(orch, account: str = "alpha") -> None:
    owner = SessionRecord(id="owner", name="owner", account=account,
                          engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80,
                          rows=24, cwd="/tmp")
    owner.launch_credential = "cfg"
    orch._records["owner"] = owner
    orch._drivers["owner"] = SimpleNamespace(account=account)


@pytest.mark.asyncio
async def test_401_recovery_never_refreshes_a_family_a_live_session_owns(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    refreshed: list[str] = []

    async def _refresh(account: str) -> None:
        refreshed.append(account)
    env.orch._refresh_stored_token = _refresh

    async def _no_reassign(*a, **k):
        return "ok"
    env.orch._reassign = _no_reassign
    _plant_cfg_owner(env.orch)
    try:
        with patch(f"{_ORCH}.crypto") as crypto:
            crypto.read_oauth_token.return_value = "sk-ant-oat01-SETUP"
            assert await env.orch._stored_credentials_valid(rec) is True
        await env.orch._recompose_same_account(rec)
        assert refreshed == []                          # the owner's family is untouched
    finally:
        env.orch._drivers.pop("owner")
        env.orch._records.pop("owner")
    await env.orch._recompose_same_account(rec)
    assert refreshed == ["alpha"]                       # no owner: the authority refreshes


@pytest.mark.asyncio
async def test_every_slot_launch_echoes_prompts_even_with_rewind_off(env) -> None:
    env.orch._rewind_spawn = lambda session_id: False
    base = env.orch._sdk_factory
    seen: list[dict] = []

    def spy(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *,
            on_pre_compact=None, options_overlay=None, flag_settings=None, extra_args=None,
            **kw):
        seen.append(dict(extra_args or {}))
        return base(cwd, config_dir, can_use_tool, mode, resume, extra_env,
                    on_pre_compact=on_pre_compact, options_overlay=options_overlay,
                    flag_settings=flag_settings, **kw)
    env.orch._sdk_factory = spy
    env.control.replies = [ON_REPLY]
    await env.make()
    assert "replay-user-messages" in seen[-1]
    await _set_default(False)
    await env.make("setup")
    assert "replay-user-messages" not in seen[-1]


# ── revocation: Remote Control off moves a slot session back onto the setup-token ───


@pytest.mark.asyncio
async def test_toggle_off_respawns_an_idle_slot_session_onto_the_setup_token(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    env.control.replies = [{}]
    await env.orch.set_remote_control(rec.id, False)
    assert env.orch._drivers[rec.id] is not first
    assert rec.launch_credential != "slot"
    assert rec.id not in env.orch._rc_token_fps()


@pytest.mark.asyncio
async def test_toggle_off_mid_turn_revokes_at_the_next_idle_sweep(env) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    rec.status = "working"
    env.control.replies = [{}]
    await env.orch.set_remote_control(rec.id, False)
    assert env.orch._drivers[rec.id] is first and rec.launch_credential == "slot"
    pushed: list[str] = []

    async def _push(token: str) -> None:
        pushed.append(token)
    first.push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-ROTATED"):
        await env.orch._maintain_remote_control_logins()
    assert pushed == []                                 # a revoked CLI is never renewed
    assert env.orch._drivers[rec.id] is first           # still busy: not yet
    rec.status = "idle"
    await env.orch._maintain_remote_control_logins()
    assert env.orch._drivers[rec.id] is not first
    assert rec.launch_credential != "slot"


@pytest.mark.asyncio
async def test_the_kill_switch_revokes_slot_sessions_at_the_sweep(env, monkeypatch) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    monkeypatch.setenv("VIBERPIT_REMOTE_CONTROL_ENABLED", "false")
    await env.orch._maintain_remote_control_logins()
    assert rec.launch_credential != "slot"


@pytest.mark.asyncio
async def test_a_runner_session_is_unavailable_as_runner(env) -> None:
    with patch(f"{_ORCH}.is_refresh_authority", return_value=False):
        rec = await env.make()
        assert rec.launch_credential != "slot"
        assert rec.remote_control_unavailable_reason == rc.REASON_RUNNER
        with pytest.raises(rc.RemoteControlError) as ei:
            await env.orch.set_remote_control(rec.id, True)
    assert str(ei.value) == rc.RUNNER_MESSAGE
    assert _rc_sent(env.control) == []


# ── broker runners: the authority's Remote Control envelope is the runner's login ─────


async def _persisted_launch(orch) -> None:
    """Wait for the background ``launch_credential`` row writes."""
    tasks = [t for t in list(getattr(orch, "_bg_tasks", ()))
             if (t.get_name() or "").startswith("launch-credential-")]
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def on_runner():
    """This daemon is a broker runner (not the refresh authority) holding envelopes for the
    account. Whether it holds a usable Remote Control envelope is what the env fixture's
    ``remote_control_env_for`` / ``remote_control_readiness`` patches answer."""
    with patch(f"{_ORCH}.is_refresh_authority", return_value=False), \
            patch(f"{_ORCH}._runner_uses_envelope", return_value=True):
        yield


@pytest.mark.asyncio
async def test_a_runner_session_launches_on_the_rc_envelope(env, on_runner) -> None:
    seen = _capture_spawn_env(env.orch)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    assert seen[-1]["CLAUDE_CODE_OAUTH_TOKEN"] == SLOT_TOKEN
    assert seen[-1]["CLAUDE_CODE_ORGANIZATION_UUID"] == "org-1"
    assert rec.launch_credential == "slot"
    assert rec.remote_control_unavailable_reason is None       # no longer "runner"
    assert rec.remote_control_url == URL                       # the spawn-time enable ran
    await _persisted_launch(env.orch)
    assert (await _row(rec.id)).launch_credential == "slot"    # visible to the primary


@pytest.mark.asyncio
async def test_a_runner_without_an_rc_envelope_is_login_needed(env, on_runner) -> None:
    seen = _capture_spawn_env(env.orch)
    with patch(f"{_ORCH}.remote_control_env_for", return_value=None), \
            patch(f"{_ORCH}.remote_control_readiness", return_value="login_needed"):
        rec = await env.make()
        assert "CLAUDE_CODE_ORGANIZATION_UUID" not in seen[-1]
        assert rec.launch_credential != "slot"
        assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_NEEDED
        assert _rc_sent(env.control) == []
        with pytest.raises(rc.RemoteControlError) as ei:
            await env.orch.set_remote_control(rec.id, True)
    assert str(ei.value) == rc.LOGIN_NEEDED_MESSAGE


@pytest.mark.asyncio
async def test_a_creds_sync_pushes_a_new_rc_token_into_live_runner_sessions(
        env, on_runner, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers[rec.id]
    pushed: list[str] = []

    async def _push(token: str) -> None:
        pushed.append(token)
    driver.push_oauth_token = _push
    fresh = "sk-ant-oat01-NEXT-GENERATION"
    with patch(f"{_ORCH}.slot_access_token", return_value=fresh):
        await env.orch.remote_control_credentials_synced()
        await env.orch.remote_control_credentials_synced()
    assert pushed == [fresh]                                  # once per new token
    assert env.orch._drivers[rec.id] is driver
    assert fresh not in caplog.text and SLOT_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_runner_push_is_retried_on_the_next_sync(env, on_runner) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers[rec.id]
    calls: list[str] = []

    async def _push(token: str) -> None:
        calls.append(token)
        if len(calls) == 1:
            raise rc.RemoteControlError(rc.REMOTE_CONTROL_FAILED, "no reply")
    driver.push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-NEW"):
        await env.orch.remote_control_credentials_synced()
        await env.orch.remote_control_credentials_synced()
        await env.orch.remote_control_credentials_synced()
    assert len(calls) == 2 and env.orch._drivers[rec.id] is driver


@pytest.mark.asyncio
async def test_the_authority_ignores_creds_syncs(env) -> None:
    upkeep = []

    async def _upkeep() -> None:
        upkeep.append(1)
    env.orch._maintain_remote_control_logins = _upkeep
    assert env.orch.remote_control_credentials_synced() is None
    assert upkeep == []


@pytest.mark.asyncio
async def test_runner_toggle_off_respawns_onto_the_inference_envelope(env, on_runner) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    env.control.replies = [{}]
    await env.orch.set_remote_control(rec.id, False)
    assert env.orch._drivers[rec.id] is not first
    assert rec.launch_credential != "slot"
    assert rec.id not in env.orch._rc_token_fps()


@pytest.mark.asyncio
async def test_runner_toggle_off_mid_turn_revokes_at_a_later_creds_sync(env, on_runner) -> None:
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    first = env.orch._drivers[rec.id]
    rec.status = "working"
    env.control.replies = [{}]
    await env.orch.set_remote_control(rec.id, False)
    assert env.orch._drivers[rec.id] is first and rec.launch_credential == "slot"
    pushed: list[str] = []

    async def _push(token: str) -> None:
        pushed.append(token)
    first.push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-ROTATED"):
        await env.orch.remote_control_credentials_synced()
    assert pushed == []                                 # a revoked CLI is never renewed
    rec.status = "idle"
    await env.orch.remote_control_credentials_synced()
    assert env.orch._drivers[rec.id] is not first
    assert rec.launch_credential != "slot"


@pytest.mark.asyncio
async def test_the_sweep_pushes_a_refreshed_token_to_live_slot_sessions(env, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers[rec.id]
    pushed: list[str] = []

    async def _push(token: str) -> None:
        pushed.append(token)
    driver.push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value=SLOT_TOKEN):
        await env.orch._push_slot_tokens()
    assert pushed == []                                      # the CLI already holds it
    fresh = "sk-ant-oat01-REFRESHED"
    with patch(f"{_ORCH}.slot_access_token", return_value=fresh):
        await env.orch._push_slot_tokens()
        await env.orch._push_slot_tokens()
    assert pushed == [fresh]                                 # once per new token
    assert fresh not in caplog.text and SLOT_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_a_failed_push_is_logged_retried_and_never_kills_the_session(env, caplog) -> None:
    caplog.set_level(logging.INFO)
    env.control.replies = [ON_REPLY]
    rec = await env.make()
    driver = env.orch._drivers[rec.id]
    calls = 0

    async def _push(token: str) -> None:
        nonlocal calls
        calls += 1
        raise rc.RemoteControlError(rc.REMOTE_CONTROL_FAILED, "no reply")
    driver.push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-NEW"):
        await env.orch._push_slot_tokens()
        await env.orch._push_slot_tokens()
    assert calls == 2
    assert env.orch._drivers[rec.id] is driver
    assert any("token_push_failed" in r.getMessage() for r in caplog.records)
    assert "sk-ant-oat01-NEW" not in caplog.text


@pytest.mark.asyncio
async def test_setup_token_sessions_are_never_pushed_a_slot_token(env) -> None:
    await _set_default(False)
    rec = await env.make()
    pushed: list[str] = []

    async def _push(token: str) -> None:
        pushed.append(token)
    env.orch._drivers[rec.id].push_oauth_token = _push
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-NEW"):
        await env.orch._push_slot_tokens()
    assert pushed == []


@pytest.mark.asyncio
async def test_the_refresh_sweep_runs_the_remote_control_upkeep(env) -> None:
    upkeep = []

    async def _upkeep() -> None:
        upkeep.append(1)
    env.orch._maintain_remote_control_logins = _upkeep
    with patch("viberpit.accounts.list_accounts", return_value=[]):
        await env.orch._refresh_idle_tokens_once()
    assert upkeep == [1]


@pytest.mark.asyncio
async def test_a_rejection_memo_lifts_when_the_slot_token_changes(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    rec = await env.make()
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED
    with patch(f"{_ORCH}.slot_access_token", return_value=SLOT_TOKEN):
        await env.orch._recheck_remote_control_logins()      # same token: still refused
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_REJECTED
    with patch(f"{_ORCH}.slot_access_token", return_value="sk-ant-oat01-RELOGIN"):
        await env.orch._recheck_remote_control_logins()      # a fresh browser login
    assert "alpha" not in env.orch._remote_control_refused_accounts()
    assert rec.remote_control_unavailable_reason is None


@pytest.mark.asyncio
async def test_a_spawn_on_a_new_slot_token_drops_the_memo(env) -> None:
    env.control.replies = [_control_error(LOGIN_REJECTED)]
    await env.make()
    relogin = {**SLOT_ENV, "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-RELOGIN"}
    env.control.replies = [ON_REPLY]
    with patch(f"{_ORCH}.remote_control_env_for", return_value=relogin):
        second = await env.make("second")
    assert second.remote_control_unavailable_reason is None
    assert second.remote_control_url == URL


@pytest.mark.asyncio
async def test_slot_session_credential_checks_follow_the_launch(env) -> None:
    from viberpit import oauth as real_oauth
    env.control.replies = [ON_REPLY]
    rec = await env.make()

    async def _dead(account: str) -> None:
        raise real_oauth.AuthError("slot rejected")
    env.orch._refresh_stored_token = _dead
    with patch(f"{_ORCH}.crypto") as crypto:
        crypto.read_oauth_token.return_value = "sk-ant-oat01-SETUP"
        assert env.orch._session_uses_setup_token(rec) is False
        # Only the claude.ai login died; the setup-token still carries the account.
        assert await env.orch._stored_credentials_valid(rec) is True
        crypto.read_oauth_token.return_value = None
        assert await env.orch._stored_credentials_valid(rec) is False
    rec.launch_credential = "setup"
    assert env.orch._session_uses_setup_token(rec) is True


# ── driver: the live token rotation message ────────────────────────────────────────────


class _Transport:
    """The SDK transport plus the reply its reader would route: a ``control_response``
    for the line's ``request_id``, resolved through the query's pending table."""

    def __init__(self, query: Any, reply: Any) -> None:
        self.lines: list[str] = []
        self.query = query
        self.reply = reply

    async def write(self, data: str) -> None:
        self.lines.append(data)
        if self.reply is None:
            return                          # the CLI never answers
        if isinstance(self.reply, BaseException):
            raise self.reply                # the write itself failed
        rid = json.loads(data)["request_id"]
        # An ("error", text) reply is the CLI's error control_response, which the SDK's
        # reader stores as an Exception.
        self.query.pending_control_results[rid] = (
            Exception(self.reply[1]) if isinstance(self.reply, tuple)
            else {"subtype": "success", "request_id": rid, "response": self.reply})
        self.query.pending_control_responses[rid].set()


async def _driver_with_transport(reply: Any):
    control = FakeControl()
    control.pending_control_responses = {}  # type: ignore[attr-defined]
    control.pending_control_results = {}    # type: ignore[attr-defined]
    d = _driver(control)
    await d.start()
    transport = _Transport(control, reply)
    client: Any = d._client
    client._transport = transport
    return d, control, transport


@pytest.mark.asyncio
async def test_push_oauth_token_writes_the_top_level_update(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    d, control, transport = await _driver_with_transport(
        {"applied": ["CLAUDE_CODE_OAUTH_TOKEN"]})
    try:
        await d.push_oauth_token("sk-ant-oat01-NEW")
    finally:
        await d.aclose()
    assert len(transport.lines) == 1 and transport.lines[0].endswith("\n")
    msg = json.loads(transport.lines[0])
    assert msg["type"] == "update_environment_variables"   # top level, not control_request
    assert msg["variables"] == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-NEW"}
    assert isinstance(msg["request_id"], str) and msg["request_id"]
    assert control.pending_control_responses == {}          # cleaned up
    assert control.sent == []                               # not a control request
    assert "sk-ant-oat01-NEW" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,code", [
    ({"applied": []}, rc.REMOTE_CONTROL_REFUSED),
    (("error", "variables must be an object of string values"), rc.REMOTE_CONTROL_REFUSED),
    (CLIConnectionError("closed"), rc.REMOTE_CONTROL_NOT_LIVE),
    (RuntimeError("boom"), rc.REMOTE_CONTROL_FAILED),
])
async def test_push_oauth_token_failures_map_to_one_code(reply, code, caplog) -> None:
    d, _, _ = await _driver_with_transport(reply)
    try:
        with pytest.raises(rc.RemoteControlError) as ei:
            await d.push_oauth_token("sk-ant-oat01-NEW")
    finally:
        await d.aclose()
    assert ei.value.code == code
    assert "sk-ant-oat01-NEW" not in caplog.text and "sk-ant-oat01-NEW" not in str(ei.value)


@pytest.mark.asyncio
async def test_push_oauth_token_without_a_reply_times_out(monkeypatch) -> None:
    monkeypatch.setattr(rc, "TOKEN_PUSH_TIMEOUT_S", 0.05)
    d, control, _ = await _driver_with_transport(None)
    try:
        with pytest.raises(rc.RemoteControlError) as ei:
            await d.push_oauth_token("tok")
    finally:
        await d.aclose()
    assert ei.value.code == rc.REMOTE_CONTROL_FAILED
    assert control.pending_control_responses == {}


@pytest.mark.asyncio
async def test_push_oauth_token_needs_a_live_client() -> None:
    d = _driver(FakeControl())
    with pytest.raises(rc.RemoteControlError) as ei:
        await d.push_oauth_token("tok")
    assert ei.value.code == rc.REMOTE_CONTROL_NOT_LIVE


# ── upgrade: a session launched before the login was usable moves onto it ──────────────


async def _make_on_setup_token(env, name: str = "RC") -> SessionRecord:
    """A session that launched (Remote Control on) while its account had no usable
    claude.ai login: on the setup-token, ``login_needed``."""
    with patch(f"{_ORCH}.remote_control_env_for", return_value=None), \
            patch(f"{_ORCH}.remote_control_readiness", return_value="login_needed"), \
            patch("server.sessions.account_auth.crypto") as crypto:
        crypto.read_oauth_token.return_value = "sk-ant-oat01-SETUP"
        rec = await env.make(name)
    assert rec.launch_credential == "setup"
    assert rec.remote_control_unavailable_reason == rc.REASON_LOGIN_NEEDED
    return rec


@pytest.mark.asyncio
async def test_an_idle_session_is_upgraded_onto_the_login_once_it_is_ready(env) -> None:
    rec = await _make_on_setup_token(env)
    first = env.orch._drivers[rec.id]
    assert _payload(rec)["remoteControlState"] == "unavailable"
    env.control.replies = [ON_REPLY]
    await env.orch._maintain_remote_control_logins()     # the sign-in completed
    await env.settle(rec.id)
    assert env.orch._drivers[rec.id] is not first          # respawned, resumed
    assert rec.launch_credential == "slot"
    assert rec.remote_control_unavailable_reason is None
    assert rec.remote_control_url == URL
    assert _rc_sent(env.control) == [{"subtype": "remote_control", "enabled": True}]
    assert _payload(rec)["remoteControlState"] == "on"


@pytest.mark.asyncio
async def test_a_runner_session_on_the_inference_envelope_is_upgraded_too(env) -> None:
    # Regression (prod, rocky 2026-11-25): a runner session that started before its first
    # Remote Control envelope arrived is stamped "envelope", not "setup", and the upgrade
    # skipped it, so it never reached the Claude apps.
    rec = await _make_on_setup_token(env)
    rec.launch_credential = "envelope"
    first = env.orch._drivers[rec.id]
    env.control.replies = [ON_REPLY]
    await env.orch._maintain_remote_control_logins()
    await env.settle(rec.id)
    assert env.orch._drivers[rec.id] is not first
    assert rec.launch_credential == "slot" and rec.remote_control_url == URL


@pytest.mark.asyncio
async def test_a_busy_session_is_upgraded_only_once_idle(env) -> None:
    rec = await _make_on_setup_token(env)
    first = env.orch._drivers[rec.id]
    rec.status = "working"
    await env.orch._maintain_remote_control_logins()
    assert env.orch._drivers[rec.id] is first and rec.launch_credential == "setup"
    p = _payload(rec)
    assert p["remoteControl"] is True and p["remoteControlState"] == "connecting"
    rec.status = "idle"
    env.control.replies = [ON_REPLY]
    await env.orch._maintain_remote_control_logins()
    await env.settle(rec.id)
    assert env.orch._drivers[rec.id] is not first and rec.launch_credential == "slot"


@pytest.mark.asyncio
async def test_a_session_with_remote_control_off_is_not_upgraded(env) -> None:
    rec = await _make_on_setup_token(env)
    await env.orch.set_remote_control(rec.id, False)
    first = env.orch._drivers[rec.id]
    await env.orch._maintain_remote_control_logins()
    assert env.orch._drivers[rec.id] is first and rec.launch_credential == "setup"
    assert _payload(rec)["remoteControlState"] == "off"


@pytest.mark.asyncio
async def test_a_pinned_login_is_not_upgraded(env) -> None:
    rec = await _make_on_setup_token(env)
    first = env.orch._drivers[rec.id]
    with patch(f"{_ORCH}.remote_control_readiness", return_value="login_busy"):
        await env.orch._maintain_remote_control_logins()
    assert env.orch._drivers[rec.id] is first and rec.launch_credential == "setup"


@pytest.mark.parametrize("available,reason,on,url,state", [
    (False, None, False, None, "unavailable"),
    (True, "login_needed", False, None, "unavailable"),
    (True, None, False, None, "off"),
    (True, None, False, URL, "off"),
    (True, None, True, None, "connecting"),
    (True, None, True, URL, "on"),
])
def test_display_state_matrix(available, reason, on, url, state) -> None:
    assert rc.display_state(available=available, reason=reason, effective_on=on,
                            url=url) == state


@pytest.mark.parametrize("on,url,state", [
    (True, None, "standby"),     # on, no CLI process: connects when the session runs
    (True, URL, "standby"),      # a stored URL from the last run is dead once detached
    (False, None, "off"),
])
def test_a_detached_session_is_standby_not_connecting(on, url, state) -> None:
    assert rc.display_state(available=True, reason=None, effective_on=on, url=url,
                            live=False) == state


def test_payload_reports_standby_for_a_detached_session() -> None:
    rec: Any = SessionRecord(id=SID, name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                             engine_session_id="c", cols=80, rows=24, cwd="/tmp")
    rec.remote_control_available = True
    rec.remote_control = True
    rec.remote_control_url = URL
    rec.detached = True
    with patch.dict(os.environ, _ENV):
        p = _payload(rec)
    assert p["remoteControlState"] == "standby"
    rec.detached = False
    with patch.dict(os.environ, _ENV):
        assert _payload(rec)["remoteControlState"] == "on"

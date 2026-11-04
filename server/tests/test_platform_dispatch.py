"""Routed dispatch for env-gated issues (host-platforms spec §3, designer calls 5 and 7).

An ``env: windows`` issue may only ever run on a runner that advertises ``windows``:
it rides the batch as ``Batch.platform``, is dispatched alone, goes to ``pick`` (and
the capacity queue) as a hard filter, is refused rather than spawned locally when
there is no routed path, runs on Claude only, and is stamped on the session row so
forks, children and respawns keep it.
"""
from __future__ import annotations

import asyncio
import os
import types
from datetime import datetime, timedelta, timezone
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select as sa_select, update as sa_update

from server.automations.backlog_grouping import family_groups, plan_to_batch
from server.automations.backlog_selection import Batch
from server.db import db_session, init_db
from server.db.models import Automation, Host, Session as DbSession
from server.pm.queue import QueueRow, rank, to_candidate
from server.sessions.engine import AgentEngine
from server.sessions.fleet_orchestrator import FleetOrchestrator
from server.sessions.fleet_router import FleetRouter, NoHostAvailable
from server.sessions.host_platforms import PlatformDispatchRefused, platform_launch_engine
from server.sessions.internal_dispatch import EdgeUnreachable
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.test_session_fork import env  # noqa: F401 — the fork fixture

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)
WIN_BATCH = Batch(primary="o/r#7", issues=["o/r#7"], repo="o/r",
                  titles={"o/r#7": "fix the desktop click"}, product="widget",
                  platform="windows")


def _cand(ref: str, platform: str | None = None, **kw) -> dict:
    return {"ref": ref, "repo": "o/r", "title": ref, "product": "widget",
            "platform": platform, **kw}


# --- selection: a gated issue is a batch of one ------------------------------------

def test_gated_issue_is_singleton_batch():
    # The ranked pool carries the gated platform onto the candidate.
    row = QueueRow(ref="o/r#1", repo="o/r", number=1, product="widget", title="t", body="",
                   url="u", status="Ready", score=1.0, gh_created_at=NOW - timedelta(days=1),
                   gh_updated_at=NOW, assignees=(), author="tony",
                   labels=("env: windows",), open_children=0, open_blockers=(),
                   live_session=False, claimed=False, author_id=7)
    plain = QueueRow(**{**row.__dict__, "ref": "o/r#2", "number": 2, "labels": ("bug",)})
    entries, _ = rank([row, plain], now=NOW, trusted_authors=frozenset({"user:7"}),
                      platforms=frozenset({"linux", "windows"}))
    by_ref = {e.ref: to_candidate(e) for e in entries}
    assert by_ref["o/r#1"]["platform"] == "windows"
    assert by_ref["o/r#2"]["platform"] is None

    pool = [_cand("o/r#1", "windows"), _cand("o/r#2"), _cand("o/r#3")]
    # A gated primary takes nobody with it...
    batch, _ = plan_to_batch([{"id": 1, "refs": ["o/r#1", "o/r#2", "o/r#3"]}], pool)
    assert batch is not None
    assert (batch.primary, batch.issues, batch.platform) == ("o/r#1", ["o/r#1"], "windows")
    # ...and an ungated primary never carries a gated sibling.
    batch, _ = plan_to_batch([{"id": 2, "refs": ["o/r#2", "o/r#1", "o/r#3"]}], pool)
    assert batch is not None
    assert (batch.issues, batch.platform) == (["o/r#2", "o/r#3"], None)
    # The grouping model never sees a gated candidate: it is planned alone up front.
    alone, rest = family_groups(pool, max_k=3)
    assert [g["refs"] for g in alone] == [["o/r#1"]]
    assert [c["ref"] for c in rest] == ["o/r#2", "o/r#3"]


# --- fleet: the platform is a hard filter, including the capacity queue ------------

class _Daemon:
    def __init__(self, name: str) -> None:
        self.name = name
        self.creates: list[dict] = []

    async def create_session(self, **kwargs):
        self.creates.append(dict(kwargs))
        return types.SimpleNamespace(id=f"s-{self.name}-{len(self.creates)}", host=None,
                                     status="queued" if kwargs.get("queue_only") else "idle")


async def _fleet(tmp_path, dbname: str, hosts) -> tuple[FleetOrchestrator, dict]:
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / dbname}")
    daemons = {}
    router = FleetRouter()
    async with db_session() as db:
        for name, platforms, active, cap in hosts:
            db.add(Host(name=name, endpoint=f"https://{name}:9443", enabled=True,
                        healthy=True, weight=100, active_sessions=active,
                        max_sessions=cap, platforms=platforms))
            daemons[name] = _Daemon(name)
            router.add(name, daemons[name])
    fo = FleetOrchestrator(cast(FleetRouter, router), primary="dobby")

    async def _stamp(session_id: str, host: str) -> None:
        return None

    fo._stamp_host = _stamp  # type: ignore[method-assign]
    return fo, daemons


@pytest.mark.asyncio
async def test_dispatch_lands_only_on_platform_host(tmp_path):
    # dobby is preferred and has room; only rocky advertises windows.
    fo, d = await _fleet(tmp_path, "route.db", [
        ("dobby", ["linux"], 0, 10), ("rocky", ["linux", "windows"], 0, 10)])
    rec = await fo.create_session(name="x", prefer="dobby", platform="windows")
    assert rec.id.startswith("s-rocky")
    assert d["dobby"].creates == []
    assert d["rocky"].creates == [{"name": "x", "platform": "windows"}]
    # An ungated launch never sends the key, so a daemon predating it is unaffected.
    await fo.create_session(name="y", prefer="dobby")
    assert d["dobby"].creates == [{"name": "y"}]
    # No host advertises the platform: refused, never placed elsewhere.
    fo2, d2 = await _fleet(tmp_path, "route2.db", [("dobby", ["linux"], 0, 10)])
    with pytest.raises(NoHostAvailable, match="windows"):
        await fo2.create_session(name="z", platform="windows")
    assert d2["dobby"].creates == []


@pytest.mark.asyncio
async def test_capacity_queue_never_drains_gated_session_to_linux(tmp_path):
    # Edge half: rocky (windows) is full while dobby has room. The session is queued
    # on rocky, never placed or queued on dobby.
    fo, d = await _fleet(tmp_path, "cap.db", [
        ("dobby", ["linux"], 0, 10), ("rocky", ["linux", "windows"], 6, 6)])
    rec = await fo.create_session(name="x", platform="windows")
    assert rec.status == "queued"
    assert d["dobby"].creates == []
    assert d["rocky"].creates == [{"name": "x", "platform": "windows", "queue_only": True}]
    # The windows host has dropped the platform (VM down) and every host is full: no
    # platform host can even persist it, so the fan-out fallback must not queue it on
    # a Linux host either.
    fo2, d2 = await _fleet(tmp_path, "cap2.db", [
        ("dobby", ["linux"], 4, 4), ("rocky", ["linux"], 6, 6)])
    with pytest.raises(NoHostAvailable):
        await fo2.create_session(name="x", platform="windows")
    assert d2["dobby"].creates == [] and d2["rocky"].creates == []


def test_queued_gated_session_drains_only_on_a_platform_host(tmp_path, monkeypatch):
    """Daemon half: a queued windows row is never started by a host whose registry
    row does not advertise windows (VM down, or a Linux host adopting the entry), and
    starts — keeping its platform stamp — once it does."""
    from server.tests.test_session_queue_drain import _make_orch, _stop_orch
    from server.sessions import orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_OWNERSHIP_HOSTS_TTL_S", 0.0)
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'drain.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            async with db_session() as db:
                db.add(Host(name="dobby", endpoint="https://dobby:9443", enabled=True,
                            healthy=True, platforms=["linux"]))
            rec = await orch.create_session("gated", queue_only=True, platform="windows")
            assert rec.status == "queued"
            assert await orch._maybe_start_queued() == 0
            assert await orch._maybe_start_queued(force=rec.id) == 0
            async with db_session() as db:
                row = await db.get(DbSession, rec.id)
                assert row is not None and row.status == "queued"
                await db.execute(sa_update(Host).where(Host.name == "dobby")
                                 .values(platforms=["linux", "windows"]))
            assert await orch._maybe_start_queued() == 1
            async with db_session() as db:
                row = await db.get(DbSession, rec.id)
                assert row is not None and row.status != "queued"
                assert row.platform == "windows"
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


# --- refusals: no routed path, or a Codex engine -----------------------------------

def _rec(id_: str) -> SessionRecord:
    return SessionRecord(id=id_, name="x", account="a", engine=AgentEngine.CLAUDE_CODE,
                         engine_session_id="e", cols=80, rows=24, cwd="/tmp")


class _Me:
    def __init__(self) -> None:
        self.create_session = AsyncMock(return_value=_rec("local"))


@pytest.mark.asyncio
async def test_gated_dispatch_refuses_when_host_routing_disabled(tmp_path):
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "false"}):
        me = _Me()
        with pytest.raises(PlatformDispatchRefused, match="routing"):
            await SessionOrchestrator._automation_create_session(
                me, name="x", platform="windows")
        me.create_session.assert_not_awaited()
        with pytest.raises(PlatformDispatchRefused):
            platform_launch_engine("windows", AgentEngine.CLAUDE_CODE)
    # An unreachable edge is not a reason to spawn a gated session here either. It
    # propagates as itself: a transport error can follow a launch the edge started,
    # and the runner's ownership-phase check is what decides whether to release.
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}), \
         patch("server.sessions.internal_dispatch.create_via_edge",
               new=AsyncMock(side_effect=EdgeUnreachable("connection refused"))):
        me = _Me()
        with pytest.raises(EdgeUnreachable):
            await SessionOrchestrator._automation_create_session(
                me, name="x", platform="windows")
        me.create_session.assert_not_awaited()

    # The automation context records it as a refused spawn, and creates nothing.
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'ctx.db'}")
    async with db_session() as db:
        db.add(Automation(id="backlog-runner"))
    from server.automations.context import AutomationContext
    create = AsyncMock(return_value=_rec("never"))
    ctx = AutomationContext("backlog-runner", create_session=create)
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "false"}):
        rec, created = await ctx.spawn_or_join_session(
            origin_ref="o/r#7", name="backlog o/r#7", platform="windows")
    assert (rec, created) == (None, False)
    create.assert_not_awaited()
    kinds = [e["kind"] for e in await ctx.list_events()]
    assert "platform_dispatch_refused" in kinds


@pytest.mark.asyncio
async def test_gated_dispatch_refuses_codex_pinned_product(tmp_path, monkeypatch):
    from server.tests.test_automation_spawn_model import (
        _build_backlog_orch, _capture_spawn, _patch_git)
    orch = _build_backlog_orch({"engine": "codex"})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]

    async def codex_plan():
        return AgentEngine.CODEX, {"engine": "codex"}
    orch._backlog_ctx.launch_plan = codex_plan  # type: ignore[method-assign]
    events: list[tuple] = []

    async def emit(level, summary, **kw):
        events.append((level, summary, kw))
    orch._backlog_ctx.emit = emit  # type: ignore[method-assign]
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}):
        rec = await orch._backlog_spawn(WIN_BATCH, "acct", default_model="sonnet",
                                        ownership_token="tok")
    assert rec is None
    assert captured == {}
    assert [kw["kind"] for _l, _s, kw in events] == ["platform_dispatch_refused"]
    assert "codex" in events[0][1].lower()
    monkeypatch.setenv("VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED", "true")
    with pytest.raises(PlatformDispatchRefused, match="codex"):
        platform_launch_engine("windows", AgentEngine.CODEX)


@pytest.mark.asyncio
async def test_gated_dispatch_forces_claude_engine(tmp_path, monkeypatch):
    from server.tests.test_automation_spawn_model import (
        _build_backlog_orch, _capture_spawn, _patch_git)
    orch = _build_backlog_orch({})
    _patch_git(monkeypatch, tmp_path)
    captured: dict = {}
    orch._backlog_ctx.spawn_session = _capture_spawn(captured)  # type: ignore[method-assign]
    with patch.dict(os.environ, {"VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED": "true"}):
        rec = await orch._backlog_spawn(WIN_BATCH, "acct", default_model="sonnet",
                                        ownership_token="tok")
    assert rec is not None
    assert captured["engine"] is AgentEngine.CLAUDE_CODE
    assert captured["platform"] == "windows"
    monkeypatch.setenv("VIBERPIT_AUTOMATION_HOST_ROUTING_ENABLED", "true")
    assert platform_launch_engine(
        "windows", AgentEngine.CLAUDE_CODE) is AgentEngine.CLAUDE_CODE
    # An ungated dispatch carries no platform at all.
    captured.clear()
    await orch._backlog_spawn(Batch(primary="o/r#8", issues=["o/r#8"], repo="o/r"), "acct",
                              default_model="sonnet", ownership_token="tok")
    assert captured["platform"] is None


# --- the session row ----------------------------------------------------------------

def test_session_row_stamped_with_platform(tmp_path):
    from server.tests.test_session_queue_drain import _make_orch, _stop_orch

    async def run():
        orch = await _make_orch(db_url=f"sqlite+aiosqlite:///{tmp_path / 'stamp.db'}",
                                tmp_path=tmp_path, max_active_sessions=10)
        try:
            live = await orch.create_session("live", cwd=str(tmp_path), platform="windows")
            plain = await orch.create_session("plain", cwd=str(tmp_path))
            queued = await orch.create_session("queued", queue_only=True, platform="windows")
            async with db_session() as db:
                got = {r.id: r.platform for r in (await db.execute(
                    sa_select(DbSession))).scalars()}
            assert got[live.id] == "windows"
            assert got[plain.id] is None
            assert got[queued.id] == "windows"
            with pytest.raises(ValueError, match="beos"):
                await orch.create_session("bad", cwd=str(tmp_path), platform="beos")
        finally:
            await _stop_orch(orch)

    asyncio.run(run())


@pytest.mark.asyncio
async def test_fork_and_respawn_inherit_platform(env) -> None:  # noqa: F811
    """A fork of a windows session, and a child it dispatches, are stamped windows
    too (design call 7). A respawn (resume, model switch, queue drain, approval)
    reuses the row, whose stamp nothing overwrites — the drain case is asserted in
    ``test_queued_gated_session_drains_only_on_a_platform_host``."""
    from server.tests.test_session_fork import _fork, _rows_with_parent
    orch, _spawns, src, _root, _clone = env
    async with db_session() as db:
        await db.execute(sa_update(DbSession).where(DbSession.id == src.id)
                         .values(platform="windows"))
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    child = await orch.dispatch_child_session(src.id, name="Child", prompt="do X",
                                              model=None, permission_mode=None,
                                              identity="u@x")
    rows = {r.id: r.platform for r in await _rows_with_parent(src.id)}
    assert rows == {out["sessionId"]: "windows", child["id"]: "windows"}


# --- the internal create route ------------------------------------------------------

def test_internal_create_rejects_unknown_platform():
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.auth.internal_token import mint_token
    env_vars = {"VIBERPIT_DB_URL": "sqlite+aiosqlite:///:memory:", "VIBERPIT_DEV": "1",
                "VIBERPIT_TAILNET_ALLOWLIST": "", "VIBERPIT_SESSION_SECRET": "x" * 32}
    with patch.dict(os.environ, env_vars):
        app = create_app()
        tok = mint_token()
        c = TestClient(app, client=("127.0.0.1", 5000))
        calls: list[dict] = []

        class _Orch:
            async def create_session(self, **kw):
                calls.append(kw)
                return _rec("s1")

        c.app.state.sessions = _Orch()
        h = {"Authorization": f"Bearer {tok}"}
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "x", "platform": "beos"}, headers=h)
        assert r.status_code == 422, r.text
        assert calls == []
        r = c.post("/api/internal/automation-sessions",
                   json={"name": "x", "platform": "windows"}, headers=h)
        assert r.status_code == 201, r.text
        assert calls == [{"prefer": None, "name": "x", "platform": "windows"}]

"""The engine decision as it reaches a real spawn (Phase 7, Task 7.1).

``test_automation_engine_policy.py`` covers the pure policy. This covers the
wiring: what ``AutomationContext.spawn_session`` actually hands ``create_session``
for an audited origin, an unaudited one, and one whose only audited engine cannot
run — plus the API surface that must refuse an out-of-policy config write.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from ccswitch.engines import AgentEngine
from server.automations.engine_policy import EnginePolicy
from server.db import db_session, init_db


async def _fake_create_session(*, origin=None, origin_ref=None, **kw):
    from server.db.models import Session
    async with db_session() as db:
        s = Session(id=f"sess-{origin_ref}", name="auto", account_name="a", cwd="/tmp",
                    status="working", origin=origin, origin_ref=origin_ref)
        db.add(s)
    s.launch_kwargs = kw  # type: ignore[attr-defined]
    return s


async def _spawn_with(tmp_path, db_name, *, policy, config=None, automations_enabled=False,
                      monkeypatch=None, **spawn_kwargs):
    """Spawn once under *policy*/*config* and return (record, ctx)."""
    from server.automations.context import AutomationContext
    from server.automations.registry import registry
    from server.db.models import Automation

    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / db_name}")
    async with db_session() as db:
        db.add(Automation(id="pr-doctor", enabled=True, mode="fix", config=config or {}))
    monkeypatch.setattr(registry.get("pr-doctor"), "engine_policy", policy,
                        raising=False)
    monkeypatch.setenv("CCSWITCH_CODEX_AUTOMATIONS_ENABLED",
                       "true" if automations_enabled else "false")
    ctx = AutomationContext("pr-doctor", create_session=_fake_create_session)
    rec = await ctx.spawn_session(origin_ref="a/b#1", name="auto", **spawn_kwargs)
    return rec, ctx


def test_a_claude_only_origin_launches_claude_explicitly(tmp_path, monkeypatch):
    """The engine is PASSED, not left to create_session's default: an automation
    launch must state its engine so a later default change cannot move it."""

    async def run():
        rec, _ctx = await _spawn_with(
            tmp_path, "eng_a.db", policy=EnginePolicy.CLAUDE_ONLY,
            monkeypatch=monkeypatch)
        assert rec.launch_kwargs["engine"] is AgentEngine.CLAUDE_CODE

    asyncio.run(run())


def test_a_forged_codex_config_on_an_unaudited_origin_still_launches_claude(
        tmp_path, monkeypatch):
    """Both layers hold here: schema validation drops the unknown `engine` key on
    the way in, and `resolve_engine` would clamp it even if it survived (that half
    is asserted directly in test_automation_engine_policy.py). Either way the
    forged row produces a Claude launch, not an unsupervised Codex run."""

    async def run():
        rec, _ctx = await _spawn_with(
            tmp_path, "eng_b.db", policy=EnginePolicy.CLAUDE_ONLY,
            config={"engine": "codex"}, automations_enabled=True,
            monkeypatch=monkeypatch)
        assert rec.launch_kwargs["engine"] is AgentEngine.CLAUDE_CODE

    asyncio.run(run())


def test_a_codex_only_origin_refuses_and_records_why_when_the_flag_is_off(
        tmp_path, monkeypatch):
    async def run():
        rec, ctx = await _spawn_with(
            tmp_path, "eng_c.db", policy=EnginePolicy.CODEX_ONLY,
            automations_enabled=False, monkeypatch=monkeypatch)
        assert rec is None                      # no session, not a Claude fallback
        events = await ctx.list_events()
        assert any(e["kind"] == "engine_policy_refused" for e in events)

    asyncio.run(run())


def test_a_codex_spawn_drops_the_claude_account_and_model_and_says_so(
        tmp_path, monkeypatch):
    """The spawn sites resolve a Claude account and a Claude model alias before
    the engine is known; both must be dropped, visibly."""

    async def run():
        rec, ctx = await _spawn_with(
            tmp_path, "eng_d.db", policy=EnginePolicy.CODEX_ALLOWED,
            config={"engine": "codex"}, automations_enabled=True,
            monkeypatch=monkeypatch, account="tony_cairnea.com", model="opus")
        assert rec.launch_kwargs["engine"] is AgentEngine.CODEX
        assert rec.launch_kwargs["account"] is None
        assert "model" not in rec.launch_kwargs
        adjusted = [e for e in await ctx.list_events()
                    if e["kind"] == "engine_launch_adjusted"]
        assert adjusted, "dropping launch inputs must be recorded, not silent"

    asyncio.run(run())


def test_an_unregistered_origin_is_unaudited_and_gets_claude(tmp_path, monkeypatch):
    """An ad-hoc origin has no declared ceiling; it must not fail open."""

    async def run():
        from server.automations.context import AutomationContext
        from server.db.models import Automation

        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'eng_e.db'}")
        async with db_session() as db:
            db.add(Automation(id="ad-hoc-origin", enabled=True))
        monkeypatch.setenv("CCSWITCH_CODEX_AUTOMATIONS_ENABLED", "true")
        ctx = AutomationContext("ad-hoc-origin", create_session=_fake_create_session)
        rec = await ctx.spawn_session(origin_ref="x#1", name="auto")
        assert rec.launch_kwargs["engine"] is AgentEngine.CLAUDE_CODE

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# API: the config/schema half of the same rule.
# --------------------------------------------------------------------------- #

H = {"Tailscale-User-Login": "testuser@example.com"}


@contextmanager
def _client():
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from server.tests.fakes.fake_factory import fake_sdk_factory
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {
                "CCSWITCH_DB_URL": f"sqlite+aiosqlite:///{tmp}/test.db",
                "CCSWITCH_DEV": "1", "CCSWITCH_TAILNET_ALLOWLIST": ""}):
            app = create_app(orchestrator=SessionOrchestrator(
                sdk_factory=fake_sdk_factory()))
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c


def test_the_api_publishes_no_engine_selector_for_an_unaudited_origin():
    with _client() as c:
        rows = c.get("/api/automations", headers=H).json()
        assert rows, "expected the registered automations"
        for row in rows:
            assert row["enginePolicy"] == "claude_only"
            assert not any(f["key"] == "engine" for f in row["configSchema"])


def test_the_api_rejects_an_engine_key_on_an_unaudited_origin():
    with _client() as c:
        resp = c.patch("/api/automations/backlog-runner",
                       json={"config": {"engine": "codex"}}, headers=H)
        assert resp.status_code == 422
        assert "engine" in resp.json()["detail"]


def test_the_api_accepts_and_validates_the_engine_key_once_an_origin_is_audited(
        monkeypatch):
    from server.automations.registry import registry
    monkeypatch.setattr(registry.get("backlog-runner"), "engine_policy",
                        EnginePolicy.CODEX_ALLOWED, raising=False)
    with _client() as c:
        ok = c.patch("/api/automations/backlog-runner",
                     json={"config": {"engine": "codex"}}, headers=H)
        assert ok.status_code == 200
        assert ok.json()["config"]["engine"] == "codex"

        # Still an enum: a value outside the ceiling is refused.
        bad = c.patch("/api/automations/backlog-runner",
                      json={"config": {"engine": "gpt-9"}}, headers=H)
        assert bad.status_code == 422


@pytest.mark.parametrize("policy,expected", [
    (EnginePolicy.CLAUDE_ONLY, []),
    (EnginePolicy.CODEX_ALLOWED, ["claude_code", "codex"]),
    (EnginePolicy.CODEX_ONLY, ["codex"]),
])
def test_registry_schema_for_reflects_the_ceiling(policy, expected, monkeypatch):
    from server.automations.registry import registry
    monkeypatch.setattr(registry.get("pr-doctor"), "engine_policy", policy,
                        raising=False)
    engine_fields = [f for f in registry.schema_for("pr-doctor") if f.key == "engine"]
    if not expected:
        assert engine_fields == []
    else:
        assert engine_fields[0].choices == expected

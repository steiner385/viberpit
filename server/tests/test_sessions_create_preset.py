"""POST /api/sessions with presetId / permissionProfileId (Task 7.4, #1228).

A preset fills any field the caller left unset on the request body (never
overriding an explicit value); a permission profile's `patterns` are written
directly to the created session's `tool_allowlist` column via a DB UPDATE —
NOT threaded through SessionOrchestrator.create_session (see the binding
ruling in the task-7.4 brief: `tool_allowlist` has zero in-memory consumers
today, so there's nothing to keep in sync by writing it out-of-band).

Harness pattern: same as test_sessions_create_host_prefer.py — FakeOrch
injected into app.state.sessions, TestClient(raise_server_exceptions=False),
a per-test sqlite file. Preset/profile/session rows are seeded directly via
db_session() before the request.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import types
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.db import db_session, init_db
from server.db.models import PermissionProfile, Session as DbSession, SessionPreset
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}


def _make_env(db_url: str) -> dict:
    return {
        "CCSWITCH_DB_URL": db_url,
        "CCSWITCH_DEV": "1",
        "CCSWITCH_TAILNET_ALLOWLIST": "",
    }


def _make_record(sid: str, **overrides) -> types.SimpleNamespace:
    base = dict(
        id=sid,
        name="Session",
        account="acct1",
        repo=None,
        host=None,
        status="idle",
        name_is_auto=True,
        detached=False,
        adopted_in_place=False,
        sub_agents=None,
        created_at=None,
        last_turn_at=None,
        account_pinned=False,
        model=None,
        active_goal=None,
        usage_unverified=False,
        updated_at_iso=None,
        rev_at=None,
        cwd=None,
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        reasoning_effort=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


class FakeOrch:
    """Fake orchestrator that records the most recent create_session kwargs
    and echoes model/repo back into the returned record."""

    def __init__(self):
        self._records: dict[str, types.SimpleNamespace] = {}
        self.last_create_kwargs: dict = {}

    async def create_session(self, **kwargs):
        self.last_create_kwargs = kwargs
        rec = _make_record(
            "sess-1",
            model=kwargs.get("model"),
            repo=kwargs.get("repo"),
        )
        self._records[rec.id] = rec
        return rec

    async def list_sessions(self):
        return list(self._records.values())

    async def get_session(self, session_id: str):
        return self._records.get(session_id)

    async def latest_suggestions(self, sid):
        return []

    async def prs_for(self, sid):
        return []

    async def latest_summary(self, sid, *, viewing=False):
        return ""

    async def session_queue_positions(self) -> dict[str, int]:
        return {}

    async def all_prs(self):
        return []


def _build_client(fake_orch: FakeOrch, db_url: str) -> TestClient:
    env = _make_env(db_url)
    with patch.dict(os.environ, env):
        real_orch = SessionOrchestrator(
            sdk_factory=fake_sdk_factory(), poll_interval=9999.0, usage_fn=lambda n: None
        )
        app = create_app(orchestrator=real_orch)
    app.state.sessions = fake_orch
    return TestClient(app, raise_server_exceptions=False)


def _new_db_url() -> str:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        return f"sqlite+aiosqlite:///{tf.name}"


def _seed_preset(db_url: str, **fields) -> None:
    async def _seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(SessionPreset(**fields))

    asyncio.run(_seed())


def _seed_permission_profile(db_url: str, **fields) -> None:
    async def _seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(PermissionProfile(**fields))

    asyncio.run(_seed())


def _seed_session_row(db_url: str, **fields) -> None:
    async def _seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(**fields))

    asyncio.run(_seed())


def _read_tool_allowlist(db_url: str, session_id: str) -> list:
    async def _read():
        await init_db(db_url=db_url)
        async with db_session() as db:
            row = await db.get(DbSession, session_id)
            return row.tool_allowlist

    return asyncio.run(_read())


# ---------------------------------------------------------------------------
# 1. Preset fills unset fields
# ---------------------------------------------------------------------------


def test_preset_fills_unset_fields():
    db_url = _new_db_url()
    try:
        _seed_preset(
            db_url,
            id="p1", name="p", model="opus", permission_mode="auto",
            repo="acme/billing",
        )
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "x", "presetId": "p1"},
                headers=_H,
            )
        assert resp.status_code == 201, resp.text
        assert fake.last_create_kwargs["model"] == "opus"
        assert fake.last_create_kwargs["repo"] == "acme/billing"
        assert fake.last_create_kwargs["permission_mode"] == "auto"
    finally:
        os.unlink(db_url.split("///")[1])


# ---------------------------------------------------------------------------
# 2. Unknown preset id -> 404
# ---------------------------------------------------------------------------


def test_unknown_preset_returns_404():
    db_url = _new_db_url()
    try:
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "x", "presetId": "does-not-exist"},
                headers=_H,
            )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "not_found"
    finally:
        os.unlink(db_url.split("///")[1])


# ---------------------------------------------------------------------------
# 3. Explicit fields are never overwritten by the preset
# ---------------------------------------------------------------------------


def test_preset_does_not_override_explicit_fields():
    db_url = _new_db_url()
    try:
        _seed_preset(
            db_url,
            id="p1", name="p", model="opus", permission_mode="auto",
            repo="acme/billing",
        )
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "x", "presetId": "p1", "model": "sonnet"},
                headers=_H,
            )
        assert resp.status_code == 201, resp.text
        assert fake.last_create_kwargs["model"] == "sonnet"
        # repo was left unset by the caller, so the preset still fills it.
        assert fake.last_create_kwargs["repo"] == "acme/billing"
    finally:
        os.unlink(db_url.split("///")[1])


# ---------------------------------------------------------------------------
# 4. Permission profile sets tool_allowlist on the real DB row
# ---------------------------------------------------------------------------


def test_permission_profile_sets_tool_allowlist():
    db_url = _new_db_url()
    try:
        _seed_permission_profile(
            db_url, id="pp1", name="p", patterns=["Bash(git *)", "Read"],
        )
        _seed_session_row(
            db_url, id="sess-1", name="x", account_name="a", cwd="/tmp",
        )
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "x", "permissionProfileId": "pp1"},
                headers=_H,
            )
        assert resp.status_code == 201, resp.text
        assert _read_tool_allowlist(db_url, "sess-1") == ["Bash(git *)", "Read"]
    finally:
        os.unlink(db_url.split("///")[1])


# ---------------------------------------------------------------------------
# 5. Unknown permission profile id -> 404
# ---------------------------------------------------------------------------


def test_unknown_permission_profile_returns_404():
    db_url = _new_db_url()
    try:
        fake = FakeOrch()
        client = _build_client(fake, db_url)
        with patch.dict(os.environ, _make_env(db_url)), client:
            resp = client.post(
                "/api/sessions",
                json={"initialPrompt": "x", "permissionProfileId": "does-not-exist"},
                headers=_H,
            )
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["code"] == "not_found"
    finally:
        os.unlink(db_url.split("///")[1])

"""On-demand suggestion generation (the sparkle ✨ "always ask the LLM" action).

Covers:
  - ClaudeAgentDriver.generate_suggestions_now: awaits the suggester, caches + returns
  - ClaudeAgentDriver.generate_suggestions_now: [] when no suggester configured
  - POST /api/sessions/{id}/suggest: 200 {"suggestions": [...]}; 404 unknown

Auth/TestClient setup mirrors test_send_prompt_route.py.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from server.app import create_app
from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_sdk import FakeSDKClient
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_factory import fake_sdk_factory

_H = {"Tailscale-User-Login": "testuser@example.com"}
_ENV = {
    "CCSWITCH_DB_URL": "sqlite+aiosqlite:///:memory:",
    "CCSWITCH_DEV": "1",
    "CCSWITCH_TAILNET_ALLOWLIST": "",
}


def _factory(msgs):
    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool)
    return make


class _StubSuggester:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    async def generate(self, cwd, config_dir, context, account=None):
        self.calls += 1
        return self.items


def test_driver_generate_now_returns_and_caches():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]
    sug = _StubSuggester(["Run the tests", "Open a PR"])

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=sug)
        await d.start()
        items = await d.generate_suggestions_now()
        cached = list(d._last_suggestions)
        await d.aclose()
        return items, cached

    items, cached = asyncio.run(run())
    assert items == ["Run the tests", "Open a PR"]
    assert cached == ["Run the tests", "Open a PR"]
    assert sug.calls == 1


def test_driver_generate_now_no_suggester_returns_empty():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("done", uuid="t1"), f.result(uuid="t1")]

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=_factory(msgs), registry=PermissionRegistry(),
                          identity="me", suggester=None)
        await d.start()
        items = await d.generate_suggestions_now()
        await d.aclose()
        return items

    assert asyncio.run(run()) == []


class _FakeSessions:
    """Minimal sessions facade for the suggest route."""

    def __init__(self, record, items):
        self._record = record
        self._items = items
        self.generated: list[str] = []

    async def get_session(self, sid):
        return self._record if (self._record and self._record.id == sid) else None

    async def generate_suggestions_now(self, sid):
        self.generated.append(sid)
        return list(self._items)


def _rec():
    from server.sessions.types import SessionRecord
    return SessionRecord(id="s1", name="n", account="a", claude_session_id="c",
                         cols=80, rows=24, cwd="/x")


def _make_client(record, items):
    with patch.dict(os.environ, _ENV):
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0,
                                   usage_fn=lambda name: None)
        app = create_app(orchestrator=orch)
    fake = _FakeSessions(record, items)
    app.state.sessions = fake
    return TestClient(app, raise_server_exceptions=False), fake


def test_suggest_route_200_returns_items():
    client, fake = _make_client(_rec(), ["Run the tests", "Commit it"])
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/suggest", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"suggestions": ["Run the tests", "Commit it"]}
    assert fake.generated == ["s1"]


def test_suggest_route_200_empty_when_model_silent():
    client, fake = _make_client(_rec(), [])
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/s1/suggest", headers=_H)
    assert r.status_code == 200
    assert r.json() == {"suggestions": []}


def test_suggest_route_404_unknown():
    client, fake = _make_client(None, [])
    with patch.dict(os.environ, _ENV), client:
        r = client.post("/api/sessions/missing/suggest", headers=_H)
    assert r.status_code == 404
    assert fake.generated == []  # never tried to generate for an unknown session

"""Persist + replay the EXACT context-window occupancy so a reloaded / idle session
shows real remaining context instead of falling back to the per-call approximation.

Background: the `contextUsage` event (carrying the SDK's real `maxTokens`) was
live-only — never replayed. On a fresh attach the frontend fell back to
`remainingContextPct(usage, model)`, which divides by a window guessed from the
model string. For a 1M-context opus session the API strips the `[1m]` marker, so
the guess is 200k and any session past 200k clamps to 0% remaining. Persisting the
exact reading and re-emitting it on attach makes the exact value the source of truth
across reloads.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord
from server.sessions.orchestrator import SessionOrchestrator


# ── serde round-trip (RPC daemon↔edge path) ─────────────────────────────────

def _rec(**kw) -> SessionRecord:
    base: dict[str, Any] = dict(id="s1", name="S1", account="acct", engine=AgentEngine.CLAUDE_CODE, engine_session_id="cs",
                                cols=80, rows=24, cwd="/tmp")
    base.update(kw)
    return SessionRecord(**base)


def test_to_dict_carries_context_usage():
    r = _rec(context_used_tokens=566286, context_max_tokens=1_000_000)
    d = r.to_dict()
    assert d["contextUsedTokens"] == 566286
    assert d["contextMaxTokens"] == 1_000_000


def test_from_dict_restores_context_usage():
    r = _rec(context_used_tokens=566286, context_max_tokens=1_000_000)
    assert SessionRecord.from_dict(r.to_dict()) == r


def test_context_usage_defaults_to_none():
    r = _rec()
    assert r.context_used_tokens is None and r.context_max_tokens is None
    assert SessionRecord.from_dict(r.to_dict()) == r


# ── rehydrate restores from the DB row ──────────────────────────────────────

class _FakeResult:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return self._rows
    def first(self): return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows): self._rows = rows
    async def execute(self, stmt): return _FakeResult(self._rows)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass


def _make_row(**kwargs):
    defaults = dict(
        id="s1", name="test", account_name="acct-a", engine_session_id="cid1",
        cwd="/tmp", repo=None, status="idle", name_is_auto=True,
        permission_mode="interactive", created_at=datetime(2025, 1, 1),
        adopted_in_place=False, model=None, account_pinned=False,
        context_used_tokens=566286, context_max_tokens=1_000_000,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.mark.asyncio
async def test_rehydrate_restores_context_usage(monkeypatch):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([_make_row()]))

    await orch.rehydrate()
    rec = orch._records["s1"]
    assert rec.context_used_tokens == 566286
    assert rec.context_max_tokens == 1_000_000


@pytest.mark.asyncio
async def test_rehydrate_defaults_when_context_columns_absent(monkeypatch):
    row = SimpleNamespace(
        id="s1", name="test", account_name="acct-a", engine_session_id="cid1",
        cwd="/tmp", repo=None, status="idle", name_is_auto=True,
        permission_mode="interactive", created_at=datetime(2025, 1, 1),
        # deliberately omit context columns
    )
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([row]))

    await orch.rehydrate()
    rec = orch._records["s1"]
    assert rec.context_used_tokens is None
    assert rec.context_max_tokens is None


# ── _persist_context_usage updates the in-memory record ─────────────────────

@pytest.mark.asyncio
async def test_persist_context_usage_updates_record(monkeypatch):
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    rec = _rec()
    orch._records = {"s1": rec}
    import server.sessions.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "db_session", lambda: _FakeDB([]))

    await orch._persist_context_usage("s1", 566286, 1_000_000)
    assert rec.context_used_tokens == 566286
    assert rec.context_max_tokens == 1_000_000


# ── driver invokes the on_context_usage callback ────────────────────────────

def test_driver_calls_on_context_usage_callback():
    from claude_agent_sdk import TextBlock
    from server.sessions.claude_driver import ClaudeAgentDriver
    from server.sessions.permissions import PermissionRegistry
    from server.sessions import events as ev
    from server.tests.fakes.fake_sdk import FakeSDKClient
    from server.tests.fakes import fake_sdk as f

    cu = {"totalTokens": 65729, "maxTokens": 200000, "percentage": 32.9,
          "model": "claude-opus-4-8"}
    msgs = [f.sys_init("sess-1"), f.stream_start(uuid="t1"),
            f.stream_text("hi", uuid="t1"),
            f.assistant(TextBlock(text="hi"), uuid="t1"),
            f.result(session_id="sess-1", uuid="t1")]
    captured: list[tuple] = []

    def make(cwd, config_dir, can_use_tool, mode, resume, extra_env=None, *, on_pre_compact=None):
        return FakeSDKClient(msgs, on_can_use_tool=can_use_tool, context_usage=cu)

    async def on_ctx(sid, used, mx):
        captured.append((sid, used, mx))

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/tmp/r", "/tmp/cfg", mode="interactive",
                          sdk_factory=make, registry=PermissionRegistry(),
                          identity="me", on_context_usage=on_ctx)
        await d.start()
        await d.send_prompt("hi")
        agen = d.events()
        while True:
            try:
                e = await asyncio.wait_for(agen.__anext__(), timeout=0.5)
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
            if isinstance(e, ev.ContextUsage):
                break
        await d.aclose()

    asyncio.run(run())
    assert captured == [("s1", 65729, 200000)], captured

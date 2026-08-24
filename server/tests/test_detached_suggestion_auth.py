"""Tests for the detached-suggestion auth gate in
server.sessions.orchestrator._generate_detached_suggestions.

Covers issue #749: an account whose browser-OAuth refresh token is dead
(`acquire_valid_token` raises `AuthError`/`FileNotFoundError`) but which still holds a live
setup-token is model-capable — the same way session launch treats it (`oauth_env_for`) — and must
not be silently and stickily blocked from suggestion generation.
"""
import asyncio
import os
import tempfile
import types
from unittest.mock import AsyncMock

import pytest

from ccswitch.oauth import AuthError
import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


def _orch_with_record(*, suggester_items=("Run tests", "Commit")):
    orch = SessionOrchestrator()
    orch._suggester = types.SimpleNamespace(
        generate=AsyncMock(return_value=list(suggester_items))
    )
    rec = types.SimpleNamespace(status="done", cwd="/x", account="tonystein85_gmail.com", repo=None)
    orch._records["s1"] = rec
    return orch, rec


def _temp_transcript():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.write(fd, b"{}\n")
    os.close(fd)
    return path


@pytest.fixture(autouse=True)
def _stub_replay(monkeypatch):
    async def fake_replay(path):
        return ["evt"]
    monkeypatch.setattr(orch_mod, "replay_events", fake_replay, raising=False)
    monkeypatch.setattr(orch_mod, "last_turn_context", lambda events: ("hi", "done"), raising=False)
    monkeypatch.setattr(orch_mod, "compose", lambda account: "/cfg/" + account, raising=False)


async def _await_until(coro_factory, predicate, tries=50):
    for _ in range(tries):
        res = await coro_factory()
        if predicate(res):
            return res
        await asyncio.sleep(0.01)
    return await coro_factory()


def test_gate_falls_back_to_setup_token_when_refresh_dead(monkeypatch):
    """AuthError from acquire_valid_token + a live setup-token -> generation proceeds."""
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-tok"})

        await orch.latest_suggestions("s1")
        got = await _await_until(lambda: orch.latest_suggestions("s1"), lambda r: bool(r))
        assert got == ["Run tests", "Commit"]
        assert orch._suggester.generate.call_count == 1
    asyncio.run(run())


def test_gate_gives_up_when_no_setup_token_either(monkeypatch, caplog):
    """AuthError + no setup-token -> give up, log a warning naming the account, never the token."""
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        with caplog.at_level("WARNING"):
            await orch.latest_suggestions("s1")
            # Let the spawned background task run to completion.
            for _ in range(20):
                if "s1" not in orch._suggest_inflight:
                    break
                await asyncio.sleep(0.01)

        assert orch._suggester.generate.call_count == 0
        assert "tonystein85_gmail.com" in caplog.text
        assert "sk-ant-oat01" not in caplog.text
    asyncio.run(run())


def test_failed_gate_attempt_is_not_cached_and_retries(monkeypatch):
    """A give-up (no auth available) must NOT be cached — the next call re-attempts generation
    rather than serving a stuck cached empty, even though the transcript signature is unchanged.
    """
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())

        state = {"has_token": False}
        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(
            orch_mod, "oauth_env_for",
            lambda account: {"CLAUDE_CODE_OAUTH_TOKEN": "tok"} if state["has_token"] else {},
        )

        # First attempt: no setup-token -> gives up.
        await orch.latest_suggestions("s1")
        for _ in range(20):
            if "s1" not in orch._suggest_inflight:
                break
            await asyncio.sleep(0.01)
        assert "s1" not in orch._detached_suggestions, "a failed attempt must not populate the cache"
        assert orch._suggester.generate.call_count == 0

        # Cause is fixed (setup-token now present); same transcript signature.
        state["has_token"] = True
        await orch.latest_suggestions("s1")   # must re-attempt, not serve a cached empty
        got = await _await_until(lambda: orch.latest_suggestions("s1"), lambda r: bool(r))
        assert got == ["Run tests", "Commit"]
        assert orch._suggester.generate.call_count == 1
    asyncio.run(run())


def test_no_setup_token_and_no_valid_token_returns_empty_from_latest_suggestions():
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        res = await orch.latest_suggestions("s1")
        assert res == []
    asyncio.run(run())

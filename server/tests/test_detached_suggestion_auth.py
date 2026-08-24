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
from unittest.mock import AsyncMock, MagicMock

import pytest

from ccswitch.oauth import AuthError
import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


def _orch_with_record(*, suggester_items=("Run tests", "Commit"), account="tonystein85_gmail.com"):
    orch = SessionOrchestrator()
    orch._suggester = types.SimpleNamespace(
        generate=AsyncMock(return_value=list(suggester_items))
    )
    rec = types.SimpleNamespace(status="done", cwd="/x", account=account, repo=None)
    orch._records["s1"] = rec
    return orch, rec


def _add_record(orch, session_id, *, account="tonystein85_gmail.com"):
    rec = types.SimpleNamespace(status="done", cwd="/x", account=account, repo=None)
    orch._records[session_id] = rec
    return rec


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


async def _run_one_failed_attempt(orch, session_id="s1"):
    """Drive one latest_suggestions() poll to completion (spawned task finished)."""
    await orch.latest_suggestions(session_id)
    for _ in range(20):
        if session_id not in orch._suggest_inflight:
            break
        await asyncio.sleep(0.01)


def test_failed_gate_attempt_is_not_cached_and_retries_after_backoff(monkeypatch):
    """A give-up (no auth available) must NOT be cached — a later call re-attempts generation
    rather than serving a stuck cached empty, even though the transcript signature is unchanged.
    "Later" means after the backoff window (see the poll-storm tests below) has elapsed; that
    part is simulated here by forcing `_suggest_auth_failed_until` into the past.
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
        await _run_one_failed_attempt(orch)
        assert "s1" not in orch._detached_suggestions, "a failed attempt must not populate the cache"
        assert orch._suggester.generate.call_count == 0
        assert "tonystein85_gmail.com" in orch._suggest_auth_failed_until, (
            "a failed attempt must arm the backoff, keyed by account"
        )

        # Cause is fixed (setup-token now present) and the backoff window has elapsed.
        state["has_token"] = True
        orch._suggest_auth_failed_until["tonystein85_gmail.com"] = orch_mod.time.monotonic() - 1
        await orch.latest_suggestions("s1")   # must re-attempt, not serve a cached empty
        got = await _await_until(lambda: orch.latest_suggestions("s1"), lambda r: bool(r))
        assert got == ["Run tests", "Commit"]
        assert orch._suggester.generate.call_count == 1
    asyncio.run(run())


def test_repeated_polls_within_backoff_do_not_respawn_generation(monkeypatch, caplog):
    """Issue #749 finding 1 (poll-storm regression): since a failed attempt is not cached, a
    naive re-check on every /sessions poll (every 5s while a dashboard is open) would re-invoke
    acquire_valid_token — a real refresh_access_token network POST plus sync_back — and log a
    WARNING on every single poll, for as long as the account has no usable auth. Two rapid
    consecutive failed attempts must produce exactly ONE acquire_valid_token call (and one
    WARNING), not two.
    """
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())

        acquire = MagicMock(side_effect=AuthError("token refresh failed with 400"))
        monkeypatch.setattr(orch_mod.ccswitch_accounts, "acquire_valid_token", acquire)
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        with caplog.at_level("WARNING"):
            # First poll: spawns generation, which gives up and arms the backoff.
            await _run_one_failed_attempt(orch)
            assert acquire.call_count == 1

            # Second poll immediately after: backoff window has not elapsed, must not re-spawn.
            await orch.latest_suggestions("s1")
            await asyncio.sleep(0.02)
            assert acquire.call_count == 1, "a backed-off poll must not re-invoke acquire_valid_token"

        assert caplog.text.count("detached suggestions: giving up") == 1
    asyncio.run(run())


def test_retries_once_backoff_window_elapses(monkeypatch):
    """The other direction of the poll-storm fix: backoff must not be permanent — once the
    window elapses, a poll retries generation. (Sticky-blank regression guard.)
    """
    async def run():
        orch, _ = _orch_with_record()
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())

        acquire = MagicMock(side_effect=AuthError("token refresh failed with 400"))
        monkeypatch.setattr(orch_mod.ccswitch_accounts, "acquire_valid_token", acquire)
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        await _run_one_failed_attempt(orch)
        assert acquire.call_count == 1

        # Simulate the backoff window having elapsed (no real 60s sleep in a unit test).
        orch._suggest_auth_failed_until["tonystein85_gmail.com"] = orch_mod.time.monotonic() - 1
        await _run_one_failed_attempt(orch)
        assert acquire.call_count == 2, "a poll after the backoff window must retry"
    asyncio.run(run())


def test_same_account_cooldown_shared_across_sessions(monkeypatch, caplog):
    """Issue #749 finding 2 (re-keying): the cooldown backs off an ACCOUNT, not a session. Two
    different sessions on the SAME account must share one cooldown — after session A's failed
    attempt arms the backoff, session B's poll within the window must not re-invoke
    acquire_valid_token or re-spawn generation, even though B has never attempted before.
    """
    async def run():
        orch, _ = _orch_with_record()
        _add_record(orch, "s2", account="tonystein85_gmail.com")
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())

        acquire = MagicMock(side_effect=AuthError("token refresh failed with 400"))
        monkeypatch.setattr(orch_mod.ccswitch_accounts, "acquire_valid_token", acquire)
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        with caplog.at_level("WARNING"):
            # Session A fails first, arming the account-level backoff.
            await _run_one_failed_attempt(orch, "s1")
            assert acquire.call_count == 1

            # Session B, same account, polls within the window: must not spawn generation.
            await orch.latest_suggestions("s2")
            await asyncio.sleep(0.02)
            assert acquire.call_count == 1, (
                "a second session on the same, already-backed-off account must not "
                "re-invoke acquire_valid_token"
            )
            assert "s2" not in orch._suggest_inflight

        assert caplog.text.count("detached suggestions: giving up") == 1
    asyncio.run(run())


def test_no_setup_token_and_no_valid_token_returns_empty_from_latest_suggestions(monkeypatch):
    """Cache-miss path with no usable auth returns [] without touching real credentials.

    Uses a dummy account name that cannot exist and monkeypatches both auth entry points so
    the spawned background task never performs a real OAuth refresh or sync_back write.
    """
    async def run():
        orch, _ = _orch_with_record(account="does-not-exist_example.invalid")
        orch.resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        monkeypatch.setattr(
            orch_mod.ccswitch_accounts, "acquire_valid_token",
            lambda name: (_ for _ in ()).throw(AuthError("token refresh failed with 400")),
        )
        monkeypatch.setattr(orch_mod, "oauth_env_for", lambda account: {})

        res = await orch.latest_suggestions("s1")
        assert res == []
        await _run_one_failed_attempt(orch)
        assert orch._suggester.generate.call_count == 0
    asyncio.run(run())

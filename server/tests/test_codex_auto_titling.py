"""Codex sessions get a real auto-title, not a first-prompt snapshot frozen forever.

Covers #1279: ``_maybe_apply_auto_name`` used to call ``transcript_path``
unconditionally, which returns ``None`` for every Codex session by design (Codex
has no Claude-shaped transcript file — see ``transcript_path``'s own docstring),
so the function returned immediately and a Codex session's name never moved past
whatever ``CodexAgentDriver.send_prompt`` set on the very first prompt. These
tests cover the routing fix (Codex sessions take a different path, never touch
``transcript_path``) and the new path's own guards (dedup, graceful skip with no
Codex runner).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from server.db import init_db
from server.sessions.engine import AgentEngine
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory


def _new_orch():
    return SessionOrchestrator(sdk_factory=fake_sdk_factory(), poll_interval=9999.0)


def _codex_record(sid="s1", name="s1", name_is_auto=True):
    return SessionRecord(
        id=sid, name=name, account="acct", engine=AgentEngine.CODEX,
        engine_session_id="thread-1", cols=80, rows=24, cwd="/repo/s1",
        status="idle", name_is_auto=name_is_auto,
    )


class _FakeReplay:
    def __init__(self, prompts: list[str]) -> None:
        self._prompts = prompts

    async def user_prompts(self) -> list[str]:
        return self._prompts


class _FakeCodexManager:
    """Answers ``client_for`` with a live sentinel for every account — enough to
    clear the cost guard without a real app-server runner."""

    def client_for(self, account: str) -> object | None:
        return object()


def _with_live_codex(orch):
    orch._codex = _FakeCodexManager()
    return orch


async def _init_test_db(tmp_path):
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path/'c.db'}")


def test_maybe_apply_auto_name_never_touches_transcript_path_for_codex(tmp_path):
    """The routing fix: a Codex session must not even call transcript_path (which
    would just return None and bail before this fix's fallback ever ran)."""
    async def run():
        await _init_test_db(tmp_path)
        orch = _new_orch()
        rec = _codex_record()
        orch._records = {rec.id: rec}
        orch._generate_codex_fallback_title = AsyncMock(return_value="Fix the login flow")

        def fail_if_called(session_id):
            raise AssertionError("transcript_path must not be consulted for a Codex session")

        orch.transcript_path = fail_if_called
        await orch._maybe_apply_auto_name(rec.id)
        return orch._records[rec.id].name

    assert asyncio.run(run()) == "Fix the login flow"


def test_maybe_apply_auto_name_is_a_noop_when_codex_fallback_yields_nothing(tmp_path):
    async def run():
        await _init_test_db(tmp_path)
        orch = _new_orch()
        rec = _codex_record(name="original")
        orch._records = {rec.id: rec}
        orch._generate_codex_fallback_title = AsyncMock(return_value=None)
        await orch._maybe_apply_auto_name(rec.id)
        return orch._records[rec.id].name

    assert asyncio.run(run()) == "original"


def test_maybe_apply_auto_name_respects_a_manual_rename_for_codex_too(tmp_path):
    """A manual rename (name_is_auto=False) must block Codex auto-titling exactly
    like it already blocks Claude's — checked before any engine dispatch happens."""
    async def run():
        await _init_test_db(tmp_path)
        orch = _new_orch()
        rec = _codex_record(name="my custom name", name_is_auto=False)
        orch._records = {rec.id: rec}
        orch._generate_codex_fallback_title = AsyncMock(
            side_effect=AssertionError("must not run for a manually renamed session"))
        await orch._maybe_apply_auto_name(rec.id)
        return orch._records[rec.id].name

    assert asyncio.run(run()) == "my custom name"


def test_generate_codex_fallback_title_returns_none_with_no_codex_manager():
    """Codex integration entirely disabled (self._codex is None) -> fail open,
    no title — and no attempt to reach the (nonexistent) app-server."""
    async def run():
        orch = _new_orch()
        orch._codex_replay = AsyncMock(
            side_effect=AssertionError("must not be reached with no Codex manager"))
        rec = _codex_record()
        return await orch._generate_codex_fallback_title(rec.id, rec)

    assert asyncio.run(run()) is None


def test_generate_codex_fallback_title_skips_when_no_client_is_live():
    """Cost guard: a Codex manager exists but has no live runner for this
    account -> skip rather than start one (`_codex_replay` would happily call
    `codex.ensure()`, which is the wrong thing for an unattended sweep)."""
    class _NoRunnerCodexManager:
        def client_for(self, account: str) -> object | None:
            return None

    async def run():
        orch = _new_orch()
        orch._codex = _NoRunnerCodexManager()
        orch._codex_replay = AsyncMock(
            side_effect=AssertionError("must not call _codex_replay (would ensure() a runner)"))
        rec = _codex_record()
        return await orch._generate_codex_fallback_title(rec.id, rec)

    assert asyncio.run(run()) is None


def test_generate_codex_fallback_title_returns_none_when_replay_is_unavailable():
    """A live client exists but _codex_replay still can't resolve one (e.g. no
    thread id yet) -> fail open, no title."""
    async def run():
        orch = _with_live_codex(_new_orch())
        orch._codex_replay = AsyncMock(return_value=None)
        rec = _codex_record()
        return await orch._generate_codex_fallback_title(rec.id, rec)

    assert asyncio.run(run()) is None


def test_generate_codex_fallback_title_returns_none_when_thread_has_no_prompts():
    async def run():
        orch = _with_live_codex(_new_orch())
        orch._codex_replay = AsyncMock(return_value=_FakeReplay([]))
        rec = _codex_record()
        return await orch._generate_codex_fallback_title(rec.id, rec)

    assert asyncio.run(run()) is None


def test_generate_codex_fallback_title_uses_the_thread_prompts():
    async def run():
        orch = _with_live_codex(_new_orch())
        orch._codex_replay = AsyncMock(
            return_value=_FakeReplay(["ok", "refactor the payment retry loop"]))
        rec = _codex_record()
        return await orch._generate_codex_fallback_title(rec.id, rec)

    assert asyncio.run(run()) == "refactor the payment retry loop"


def test_generate_codex_fallback_title_dedups_unchanged_prompts():
    """Mirrors the Claude path's _last_title_sig gate: an idle Codex session's
    thread doesn't grow between sweeps, so a second call with byte-identical
    prompts must skip recomputation (return None) rather than recompute the
    same title forever."""
    async def run():
        orch = _with_live_codex(_new_orch())
        prompts = ["refactor the payment retry loop"]
        orch._codex_replay = AsyncMock(return_value=_FakeReplay(prompts))
        rec = _codex_record()

        first = await orch._generate_codex_fallback_title(rec.id, rec)
        second = await orch._generate_codex_fallback_title(rec.id, rec)
        return first, second

    first, second = asyncio.run(run())
    assert first == "refactor the payment retry loop"
    assert second is None


def test_generate_codex_fallback_title_recomputes_after_new_prompts():
    async def run():
        orch = _with_live_codex(_new_orch())
        rec = _codex_record()
        orch._codex_replay = AsyncMock(return_value=_FakeReplay(["first ask"]))
        first = await orch._generate_codex_fallback_title(rec.id, rec)

        orch._codex_replay = AsyncMock(return_value=_FakeReplay(["first ask", "second ask"]))
        second = await orch._generate_codex_fallback_title(rec.id, rec)
        return first, second

    first, second = asyncio.run(run())
    assert first == "first ask"
    assert second == "first ask"  # codex_fallback_title still picks the earliest substantive prompt

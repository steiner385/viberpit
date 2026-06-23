import asyncio
import os
import types
import tempfile
from unittest.mock import AsyncMock

import pytest

import server.sessions.orchestrator as orch_mod
from server.sessions.orchestrator import SessionOrchestrator


def _orch_with_record(status="done", *, suggester_items=("Run tests", "Commit")):
    orch = SessionOrchestrator()
    orch._suggester = types.SimpleNamespace(
        generate=AsyncMock(return_value=list(suggester_items))
    )
    rec = types.SimpleNamespace(status=status, cwd="/x", account="acct")
    orch._records["s1"] = rec
    return orch, rec


def _temp_transcript():
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.write(fd, b"{}\n")
    os.close(fd)
    return path


@pytest.fixture(autouse=True)
def _stub_replay(monkeypatch):
    # Generator calls replay_events(path) then last_turn_context(events).
    async def fake_replay(path):
        return ["evt"]
    monkeypatch.setattr(orch_mod, "replay_events", fake_replay, raising=False)
    monkeypatch.setattr(orch_mod, "last_turn_context", lambda events: ("hi", "done"), raising=False)
    monkeypatch.setattr(orch_mod, "compose", lambda account: "/cfg/" + account, raising=False)


async def _await_until_cached(orch, sid, tries=50):
    # latest_suggestions schedules a background task; poll until it lands.
    for _ in range(tries):
        res = await orch.latest_suggestions(sid)
        if res:
            return res
        await asyncio.sleep(0.01)
    return await orch.latest_suggestions(sid)


def test_detached_done_generates_and_caches():
    async def run():
        orch, _ = _orch_with_record("done")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        first = await orch.latest_suggestions("s1")     # kicks generation
        assert first == []                               # nothing cached yet
        got = await _await_until_cached(orch, "s1")
        assert got == ["Run tests", "Commit"]
        # Second call, same mtime → no second generate call.
        calls_before = orch._suggester.generate.call_count
        again = await orch.latest_suggestions("s1")
        assert again == ["Run tests", "Commit"]
        assert orch._suggester.generate.call_count == calls_before
    asyncio.run(run())


def test_detached_idle_status_is_eligible():
    async def run():
        orch, _ = _orch_with_record("idle")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        await orch.latest_suggestions("s1")
        got = await _await_until_cached(orch, "s1")
        assert got == ["Run tests", "Commit"]
    asyncio.run(run())


def test_non_done_idle_status_skipped():
    async def run():
        orch, _ = _orch_with_record("in_progress")
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        res = await orch.latest_suggestions("s1")
        assert res == []
        assert orch._suggester.generate.call_count == 0
    asyncio.run(run())


def test_no_suggester_returns_empty():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._suggester = None
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        assert await orch.latest_suggestions("s1") == []
    asyncio.run(run())


def test_empty_result_is_cached_not_retried():
    async def run():
        orch, _ = _orch_with_record("done", suggester_items=())
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        await orch.latest_suggestions("s1")          # kicks generation -> []
        for _ in range(50):
            await orch.latest_suggestions("s1")
            await asyncio.sleep(0.005)
        # Generated at most once for this transcript signature.
        assert orch._suggester.generate.call_count <= 1
    asyncio.run(run())


def test_signature_change_retriggers():
    async def run():
        orch, _ = _orch_with_record("done")
        path = _temp_transcript()
        orch._resolve_transcript_path = AsyncMock(return_value=path)
        await orch.latest_suggestions("s1")
        await _await_until_cached(orch, "s1")
        first_calls = orch._suggester.generate.call_count
        # Bump mtime to simulate a new turn on the transcript.
        st = os.stat(path)
        os.utime(path, (st.st_atime + 10, st.st_mtime + 10))
        await orch.latest_suggestions("s1")          # stale -> re-kick
        await _await_until_cached(orch, "s1")
        assert orch._suggester.generate.call_count > first_calls
    asyncio.run(run())


def test_inflight_dedup_single_generation():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._resolve_transcript_path = AsyncMock(return_value=_temp_transcript())
        # Fire many concurrent awaits before the first generation completes.
        await asyncio.gather(*(orch.latest_suggestions("s1") for _ in range(8)))
        await _await_until_cached(orch, "s1")
        assert orch._suggester.generate.call_count == 1
    asyncio.run(run())


def test_live_driver_path_unchanged():
    async def run():
        orch, _ = _orch_with_record("done")
        orch._drivers["s1"] = types.SimpleNamespace(_last_suggestions=["X"])
        # Driver present → never touches the detached generator.
        assert await orch.latest_suggestions("s1") == ["X"]
        assert orch._suggester.generate.call_count == 0
    asyncio.run(run())

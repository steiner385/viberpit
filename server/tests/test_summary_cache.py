import asyncio
import types
from unittest.mock import AsyncMock

from server.sessions.orchestrator import SessionOrchestrator


def _orch_with_record(status="in_progress"):
    orch = SessionOrchestrator()
    rec = types.SimpleNamespace(id="s1", cwd="/x", account="acct", status=status)
    orch._records["s1"] = rec
    return orch, rec


def test_latest_summary_serves_cache_without_regen_when_signature_matches():
    orch, _ = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="new"))
    orch._summaries["s1"] = (5.0, 1_000.0, "cached text")

    async def fake_path(_sid):
        return "/t.jsonl"

    orch.resolve_transcript_path = fake_path  # type: ignore[assignment]
    import server.sessions.orchestrator as om
    orig = om.os.path.getmtime
    om.os.path.getmtime = lambda _p: 5.0  # signature unchanged
    try:
        out = asyncio.run(orch.latest_summary("s1", viewing=True))
    finally:
        om.os.path.getmtime = orig
    assert out == "cached text"
    orch._summarizer.generate.assert_not_called()


def test_latest_summary_no_regen_when_not_viewing():
    orch, _ = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="new"))

    async def fake_path(_sid):
        return "/t.jsonl"

    orch.resolve_transcript_path = fake_path  # type: ignore[assignment]
    import server.sessions.orchestrator as om
    orig = om.os.path.getmtime
    om.os.path.getmtime = lambda _p: 9.0
    try:
        out = asyncio.run(orch.latest_summary("s1", viewing=False))
    finally:
        om.os.path.getmtime = orig
    assert out == ""  # no cache, no generation scheduled
    orch._summarizer.generate.assert_not_called()


def test_latest_summary_missing_session_returns_empty():
    orch = SessionOrchestrator()
    assert asyncio.run(orch.latest_summary("nope", viewing=True)) == ""


def test_generate_summary_caches_text_and_clears_inflight():
    orch, rec = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="Doing X"))
    orch._summary_inflight.add("s1")

    import server.sessions.orchestrator as om
    orig_replay = om.replay_events
    orig_ctx = om.last_turn_context
    orig_compose = om.compose
    async def fake_replay(_p):
        return []
    om.replay_events = fake_replay
    om.last_turn_context = lambda _e: ("hi", "done")
    om.compose = lambda account: "/cfg/" + account  # type: ignore[assignment]
    try:
        asyncio.run(orch._generate_summary("s1", rec, "/t.jsonl", 7.0))
    finally:
        om.replay_events = orig_replay
        om.last_turn_context = orig_ctx
        om.compose = orig_compose
    sig, _at, text = orch._summaries["s1"]
    assert (sig, text) == (7.0, "Doing X")
    assert "s1" not in orch._summary_inflight

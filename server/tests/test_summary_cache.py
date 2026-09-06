"""One-line status summaries (#1282): Codex parity for latest_summary/_generate_summary.

Before this, `latest_summary` gated everything on `resolve_transcript_path`, which
answers None for every Codex session by construction (Codex has no on-disk JSONL —
see `server/codex/replay.py`). That silently meant a Codex session never got a
summary at all, not even best-effort empty text: the regen check itself never fired.

The fix routes the freshness signature and the transcript read through the same
engine-neutral seams `has_transcript`/`transcript_events` already use, and the
one-shot Haiku helper call through `_autopilot_helper_config_dir` (an Anthropic
account is required to MAKE the call, even when the session being summarized is
not one) so a Codex account with no Claude credentials of its own still gets a
meaningful summary rather than silently failing to authenticate.
"""
import asyncio
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.sessions.orchestrator import SessionOrchestrator

from server.tests.test_codex_orchestrator import (
    _FakeCodexManager,
    _FakeThreadClient,
    _record,
    _turn,
)


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
    """Not viewing must short-circuit BEFORE the (possibly Codex-RPC-costed)
    signature probe — should_regenerate would refuse regardless, so nothing about
    the transcript needs to be read at all."""
    orch, _ = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="new"))

    async def fail_signature(_sid):
        raise AssertionError("signature must not be probed when not viewing")

    orch._summary_signature = fail_signature  # type: ignore[assignment]
    out = asyncio.run(orch.latest_summary("s1", viewing=False))
    assert out == ""  # no cache, no generation scheduled
    orch._summarizer.generate.assert_not_called()


def test_latest_summary_missing_session_returns_empty():
    orch = SessionOrchestrator()
    assert asyncio.run(orch.latest_summary("nope", viewing=True)) == ""


def test_generate_summary_caches_text_and_clears_inflight():
    orch, rec = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="Doing X"))
    orch._summary_inflight.add("s1")

    async def fake_events(_sid, *, max_turns=20):
        return []

    orch.transcript_events = fake_events  # type: ignore[assignment]
    orch._autopilot_helper_config_dir = lambda account: "/cfg/" + account  # type: ignore[assignment]

    import server.sessions.orchestrator as om
    orig_ctx = om.last_turn_context
    om.last_turn_context = lambda _e: ("hi", "done")
    try:
        asyncio.run(orch._generate_summary("s1", rec, 7.0))
    finally:
        om.last_turn_context = orig_ctx
    sig, _at, text = orch._summaries["s1"]
    assert (sig, text) == (7.0, "Doing X")
    assert "s1" not in orch._summary_inflight
    orch._summarizer.generate.assert_awaited_once_with(rec.cwd, "/cfg/acct", "User: hi\n\nAssistant: done")


def test_generate_summary_degrades_to_empty_when_no_helper_account_available():
    """A fleet with no live Anthropic account can't make the throwaway Haiku call
    for ANY session — this must degrade to a cached empty summary, not raise, and
    must never fall back to composing the (possibly non-Anthropic) session's own
    account, which would hand the wrong provider's credentials to the SDK."""
    orch, rec = _orch_with_record()
    orch._summarizer = types.SimpleNamespace(generate=AsyncMock(return_value="unused"))
    orch._summary_inflight.add("s1")

    async def fake_events(_sid, *, max_turns=20):
        return []

    orch.transcript_events = fake_events  # type: ignore[assignment]
    orch._autopilot_helper_config_dir = lambda account: None  # type: ignore[assignment]

    asyncio.run(orch._generate_summary("s1", rec, 3.0))
    sig, _at, text = orch._summaries["s1"]
    assert (sig, text) == (3.0, "")
    assert "s1" not in orch._summary_inflight
    orch._summarizer.generate.assert_not_called()


# --------------------------------------------------------------------------- #
# _summary_signature — the engine-neutral freshness key                        #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_signature_uses_mtime_on_the_claude_path():
    orch, _ = _orch_with_record()

    async def fake_path(_sid):
        return "/t.jsonl"

    orch.resolve_transcript_path = fake_path  # type: ignore[assignment]
    import server.sessions.orchestrator as om
    orig = om.os.path.getmtime
    om.os.path.getmtime = lambda _p: 42.0
    try:
        sig = await orch._summary_signature("s1")
    finally:
        om.os.path.getmtime = orig
    assert sig == 42.0


@pytest.mark.asyncio
async def test_summary_signature_none_when_neither_path_nor_replay_exist():
    orch, _ = _orch_with_record()

    async def no_path(_sid):
        return None

    async def no_replay(_sid):
        return None

    orch.resolve_transcript_path = no_path  # type: ignore[assignment]
    orch._engine_replay = no_replay  # type: ignore[assignment]
    assert await orch._summary_signature("s1") is None


@pytest.mark.asyncio
async def test_summary_signature_falls_back_to_turn_count_for_codex(tmp_path: Path, isolated_db):
    """The bug this issue fixes: resolve_transcript_path answers None for Codex by
    design, so latest_summary must not treat that as "no history" — it must ask
    the engine-neutral replay, exactly as has_transcript already does."""
    orch = SessionOrchestrator(
        sdk_factory=lambda *a, **k: None,
        codex=_FakeCodexManager(client=_FakeThreadClient([_turn("t1"), _turn("t2")])),
        repo_root=str(tmp_path / "repos"),
    )
    orch._records["s1"] = _record("s1", tmp_path)
    assert await orch.resolve_transcript_path("s1") is None  # the Claude-only path is silent
    sig = await orch._summary_signature("s1")
    assert sig == 2.0


# --------------------------------------------------------------------------- #
# End-to-end: latest_summary actually schedules a Codex generation             #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_latest_summary_schedules_generation_for_a_codex_session(
        tmp_path: Path, monkeypatch, isolated_db):
    """Regression guard for #1282: before this fix, a Codex session's `path` was
    always None, so the `if path and ...` gate meant this method never spawned a
    generation no matter how stale the cache was."""
    orch = SessionOrchestrator(
        sdk_factory=lambda *a, **k: None,
        codex=_FakeCodexManager(client=_FakeThreadClient([_turn("t1")])),
        repo_root=str(tmp_path / "repos"),
    )
    orch._records["s1"] = _record("s1", tmp_path)
    generate = AsyncMock(return_value="Fixing the widget")
    orch._summarizer = types.SimpleNamespace(generate=generate)  # type: ignore[assignment]
    monkeypatch.setattr("ccswitch.accounts.provider_or_none", lambda name: None)
    monkeypatch.setattr("server.sessions.helper_call.pick_helper_config_dir",
                        lambda: "/fallback/claude-acct")

    out = await orch.latest_summary("s1", viewing=True)
    assert out == ""  # nothing cached yet — the spawn is fire-and-forget
    await asyncio.sleep(0)  # let the spawned task run
    for _ in range(50):
        if "s1" not in orch._summary_inflight:
            break
        await asyncio.sleep(0.01)
    assert orch._summaries["s1"][2] == "Fixing the widget"
    generate.assert_awaited_once()
    call = generate.await_args
    assert call is not None and call.args[1] == "/fallback/claude-acct"  # never the Codex acct's dir

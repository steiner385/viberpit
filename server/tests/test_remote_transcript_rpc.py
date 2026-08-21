"""Cross-host transcript replay/search: the daemon resolves + reads the transcript
file, the edge never touches a path string itself.

Regression: RemoteOrchestrator.transcript_window used to resolve a path via RPC and
then read that path LOCALLY (replay_window(path, ...)) — correct only when the daemon
and edge share a filesystem. Once the daemon runs on another host, the path string is
valid on both machines but points at DIFFERENT files, so the edge read silently
returned an empty/blank transcript. The fix moves the read behind a `transcript_window`
RPC (and a `transcript_search_text` RPC for search) so the read always happens on the
daemon's own host.
"""
import types
from unittest.mock import AsyncMock

import pytest

from server.ipc.orchestrator_rpc import register_orchestrator_rpc
from server.ipc.client import DaemonClient
from server.ipc.daemon import create_daemon_app
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.remote import RemoteOrchestrator
from server.sessions.replay import ReplayWindow
from server.tests.ipc_harness import serve_daemon_uds
from unittest import mock


def _stub_remote(result):
    ro = RemoteOrchestrator.__new__(RemoteOrchestrator)
    ro._client = types.SimpleNamespace(call=AsyncMock(return_value=result))
    return ro


# ---------------------------------------------------------------------------
# Part A: RemoteOrchestrator.transcript_window
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcript_window_builds_replay_window_from_stubbed_rpc(monkeypatch):
    """RemoteOrchestrator.transcript_window must round-trip events/earliestTurn/
    totalTurns from the RPC response into a ReplayWindow, and must NEVER touch the
    local filesystem to do it (no path is even present in the stubbed response)."""
    opened: list[str] = []
    real_open = open

    def _tripwire_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _tripwire_open)

    fake_events = [{"type": "textDelta", "turnId": "t1", "text": "hi"},
                   {"type": "turnEnd", "turnId": "t1"}]
    ro = _stub_remote({"events": fake_events, "earliestTurn": 3, "totalTurns": 9})

    window = await ro.transcript_window("s1", before=10, limit=5)

    assert isinstance(window, ReplayWindow)
    assert window.events == fake_events
    assert window.earliest_turn == 3
    assert window.total_turns == 9
    ro._client.call.assert_awaited_once_with(
        "transcript_window", session_id="s1", before=10, limit=5)
    assert opened == []  # no local file was ever opened


@pytest.mark.asyncio
async def test_transcript_window_returns_none_when_daemon_reports_none():
    ro = _stub_remote(None)
    window = await ro.transcript_window("s1")
    assert window is None


@pytest.mark.asyncio
async def test_transcript_window_over_real_daemon_uds(tmp_path):
    """End-to-end: the daemon-side orchestrator resolves the path AND replays the
    transcript in-process; the edge (RemoteOrchestrator) only ever sees the RPC's
    already-serialized dict — proving the whole cross-host-safe path works, not just
    the stubbed wiring."""
    jsonl = tmp_path / "cs1.jsonl"
    jsonl.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"hello"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","model":"m1",'
        '"content":[{"type":"text","text":"hi there"}]}}\n'
    )

    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    orch.resolve_transcript_path = AsyncMock(return_value=str(jsonl))
    register_orchestrator_rpc(app, orch)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            window = await ro.transcript_window("s1")
        finally:
            await ro.aclose()

    assert window is not None
    assert window.total_turns == 1
    types_seen = [e.get("type") for e in window.events]
    assert "userMessage" in types_seen or any("hi there" in str(e) for e in window.events)


# ---------------------------------------------------------------------------
# Part B: RemoteOrchestrator.transcript_search_text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transcript_search_text_calls_rpc_not_a_path(monkeypatch):
    """The remote search-text method must go through the daemon RPC — it must never
    resolve a path and read it locally."""
    opened: list[str] = []
    real_open = open

    def _tripwire_open(path, *a, **k):
        opened.append(str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _tripwire_open)

    ro = _stub_remote("fix the auth bug")
    text = await ro.transcript_search_text("s1")

    assert text == "fix the auth bug"
    ro._client.call.assert_awaited_once_with("transcript_search_text", session_id="s1")
    assert opened == []


@pytest.mark.asyncio
async def test_transcript_search_text_non_str_rpc_result_degrades_to_empty():
    ro = _stub_remote(None)
    assert await ro.transcript_search_text("s1") == ""


@pytest.mark.asyncio
async def test_transcript_search_text_over_real_daemon_uds(tmp_path):
    jsonl = tmp_path / "cs2.jsonl"
    jsonl.write_text('{"type":"user","message":{"role":"user","content":"needle phrase"}}\n')

    app = create_daemon_app()
    orch = SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)
    orch.resolve_transcript_path = AsyncMock(return_value=str(jsonl))
    register_orchestrator_rpc(app, orch)

    async with serve_daemon_uds(app) as sock:
        ro = RemoteOrchestrator(DaemonClient(sock))
        try:
            text = await ro.transcript_search_text("s1")
        finally:
            await ro.aclose()

    assert "needle phrase" in text


# ---------------------------------------------------------------------------
# _search_ids: fetch-text callable contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_ids_matches_via_fetch_text_callable():
    from server.api.sessions import _search_ids

    texts = {"s1": "nothing relevant", "s2": "we fixed the auth token refresh"}

    async def fetch_text(sid):
        return texts[sid]

    items = [("s1", "Session One"), ("s2", "Session Two")]
    ids = await _search_ids(items, fetch_text, ["auth"])
    assert ids == ["s2"]


@pytest.mark.asyncio
async def test_search_ids_degrades_to_name_only_when_fetch_text_raises():
    from server.api.sessions import _search_ids

    async def fetch_text(sid):
        if sid == "s1":
            raise RuntimeError("daemon hiccup")
        return "irrelevant text"

    items = [("s1", "Auth cleanup"), ("s2", "Billing")]
    # 'auth' matches s1 by NAME despite the fetch failure, and does not match s2.
    ids = await _search_ids(items, fetch_text, ["auth"])
    assert ids == ["s1"]


@pytest.mark.asyncio
async def test_search_ids_skips_fetch_entirely_when_terms_blank():
    from server.api.sessions import _search_ids

    calls = []

    async def fetch_text(sid):
        calls.append(sid)
        return "x"

    items = [("s1", "A"), ("s2", "B")]
    ids = await _search_ids(items, fetch_text, [])
    assert sorted(ids) == ["s1", "s2"]
    assert calls == []  # blank terms must skip the fetch entirely

"""Review Fix 3: the WS attach replay must be bounded by the same ceiling.

`_run_replay_and_complete` called `replay_window` directly, so the transcript
ceiling added for the RPC path did not cover the WebSocket attach — arguably
*the* session-open path. A slow transcript there wedges the attach: no
replayComplete, so the client's spinner never clears and the live stream never
takes over.
"""
import asyncio

import pytest

from server.bridge import ws as ws_mod


class _FakeOrch:
    async def transcript_path(self, session_id):
        return "/tmp/does-not-matter.jsonl"


@pytest.mark.asyncio
async def test_slow_attach_replay_degrades_instead_of_hanging(monkeypatch):
    async def never_finishes(*a, **k):
        await asyncio.sleep(3600)

    from server.sessions import orchestrator as orch_mod

    # ws.py imports replay_window at module scope → patch it there.
    monkeypatch.setattr(ws_mod, "replay_window", never_finishes)
    # ...and it reads the ceiling through the orchestrator module, so there is
    # exactly ONE constant to patch (no duplicated ceiling).
    monkeypatch.setattr(orch_mod, "_TRANSCRIPT_WINDOW_CEILING_S", 0.3)

    frames: list[dict] = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        events = await asyncio.wait_for(
            ws_mod._run_replay_and_complete(_FakeOrch(), frames.append, "sess-1"),
            timeout=5.0,
        )
    except asyncio.TimeoutError:
        pytest.fail("ws attach replay did not return within 5s — the ceiling is "
                    "not applied on the WS path and this test's safety timeout fired")
    elapsed = loop.time() - started

    assert elapsed < 2.0, f"ws attach replay blocked for {elapsed:.1f}s"
    assert events == [], "a timed-out replay degrades to no history, not a raise"
    # The client must still be released: replayComplete always rides the queue.
    assert [f["type"] for f in frames] == ["replayComplete"]
    assert frames[0]["sessionId"] == "sess-1"


@pytest.mark.asyncio
async def test_fast_attach_replay_still_replays(monkeypatch):
    """Guard against 'fixing' the hang by never replaying anything."""
    from server.sessions.replay import ReplayWindow

    class _Ev:
        def to_wire(self):
            return {"type": "textDelta", "text": "hi"}

    async def quick(*a, **k):
        return ReplayWindow(events=[_Ev()], earliest_turn=2, total_turns=7)

    monkeypatch.setattr(ws_mod, "replay_window", quick)

    frames: list[dict] = []
    await ws_mod._run_replay_and_complete(_FakeOrch(), frames.append, "sess-2")

    assert [f["type"] for f in frames] == ["replayReset", "textDelta", "replayComplete"]
    assert frames[-1]["earliestTurn"] == 2
    assert frames[-1]["totalTurns"] == 7

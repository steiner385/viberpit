"""The WS attach falls back to the engine replay when there is no transcript FILE.

`_run_replay_and_complete` reads the Claude transcript directly, because only
`replay_window` takes `drop_incomplete_turn` / `turn_started_at` — and in remote mode
the edge reads the shared file, so routing it through an RPC would change both the
latency and the ring/gap behaviour. That path must stay byte-identical. A Codex
session has no file at all, so `transcript_path` answers None and the attach takes the
`engine_replay_window` fallback; everything after it — replayReset, the per-frame
sessionId tag, replayComplete — is shared and unchanged.
"""
from __future__ import annotations

import pytest

from server.bridge import ws as ws_mod
from server.sessions.replay import ReplayWindow


class _Ev:
    def __init__(self, text: str) -> None:
        self.text = text

    def to_wire(self) -> dict:
        return {"type": "textDelta", "text": self.text}


class _EngineOrch:
    """No transcript file; history comes back through engine_replay_window."""

    def __init__(self, window: ReplayWindow | None, *, wire: bool = False) -> None:
        self._window = window
        self._wire = wire
        self.calls: list[dict] = []

    async def transcript_path(self, session_id):
        return None

    async def engine_replay_window(self, session_id, *, drop_incomplete_turn=False,
                                   turn_started_at=None):
        self.calls.append({"session_id": session_id,
                           "drop_incomplete_turn": drop_incomplete_turn,
                           "turn_started_at": turn_started_at})
        if self._window is None or not self._wire:
            return self._window
        return ReplayWindow(
            events=[e.to_wire() for e in self._window.events],
            earliest_turn=self._window.earliest_turn,
            total_turns=self._window.total_turns,
        )


@pytest.mark.asyncio
async def test_attach_replays_a_session_with_no_transcript_file() -> None:
    orch = _EngineOrch(ReplayWindow(events=[_Ev("hello")], earliest_turn=3,
                                    total_turns=9))
    frames: list[dict] = []

    events = await ws_mod._run_replay_and_complete(
        orch, frames.append, "codex-1", drop_incomplete=True,
        turn_started_at=1751710123.5)

    assert orch.calls == [{"session_id": "codex-1", "drop_incomplete_turn": True,
                           "turn_started_at": 1751710123.5}]
    assert [f["type"] for f in frames] == ["replayReset", "textDelta", "replayComplete"]
    assert all(f["sessionId"] == "codex-1" for f in frames)
    assert (frames[-1]["earliestTurn"], frames[-1]["totalTurns"]) == (3, 9)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_already_serialized_events_are_forwarded_unchanged() -> None:
    """In remote mode the daemon RPC hands back wire dicts, not ev.* objects."""
    orch = _EngineOrch(ReplayWindow(events=[_Ev("hi")], total_turns=1), wire=True)
    frames: list[dict] = []

    await ws_mod._run_replay_and_complete(orch, frames.append, "codex-2")

    assert frames[1] == {"type": "textDelta", "text": "hi", "sessionId": "codex-2"}


@pytest.mark.asyncio
async def test_an_unreadable_engine_history_still_releases_the_client() -> None:
    """None means "could not read" — the attach degrades to no history but must
    still send replayComplete, or the client's spinner never clears."""
    orch = _EngineOrch(None)
    frames: list[dict] = []

    events = await ws_mod._run_replay_and_complete(orch, frames.append, "codex-3")

    assert events == []
    assert [f["type"] for f in frames] == ["replayComplete"]


@pytest.mark.asyncio
async def test_the_claude_disk_path_is_untouched(monkeypatch) -> None:
    """When a transcript file exists the attach still reads it directly — the
    fallback must not be reachable, or remote mode loses its shared-file read."""
    class _DiskOrch(_EngineOrch):
        async def transcript_path(self, session_id):
            return "/tmp/whatever.jsonl"

    orch = _DiskOrch(None)

    async def _from_disk(path, **kw):
        assert kw["drop_incomplete_turn"] is True
        return ReplayWindow(events=[_Ev("disk")], total_turns=1)

    monkeypatch.setattr(ws_mod, "replay_window", _from_disk)
    frames: list[dict] = []
    await ws_mod._run_replay_and_complete(orch, frames.append, "claude-1",
                                          drop_incomplete=True)

    assert orch.calls == [], "the engine fallback ran for a session that has a file"
    assert [f["type"] for f in frames] == ["replayReset", "textDelta", "replayComplete"]


@pytest.mark.asyncio
async def test_a_failing_engine_replay_still_sends_the_reset() -> None:
    """A fallback failure must lose HISTORY, never the `replayReset`.

    This branch also runs for a brand-new Claude session whose transcript does not
    exist yet, and the call can legitimately fail on a mixed-version fleet — a
    newer edge attaching to a session owned by an older daemon that never
    registered the RPC. If that reached the outer handler it would skip the reset,
    which is the #390 regression: the reducer keeps its stale transcript and
    double-appends the live turn on every reconnect.
    """
    class _BrokenOrch(_EngineOrch):
        async def engine_replay_window(self, session_id, **kw):
            raise RuntimeError("older daemon does not know this RPC")

    frames: list[dict] = []
    events = await ws_mod._run_replay_and_complete(
        _BrokenOrch(None), frames.append, "codex-4", drop_incomplete=True)

    assert events == []
    assert [f["type"] for f in frames] == ["replayReset", "replayComplete"]


@pytest.mark.asyncio
async def test_an_orchestrator_without_the_rpc_still_attaches() -> None:
    """The same guarantee for a peer that lacks the method entirely."""
    class _OldOrch:
        async def transcript_path(self, session_id):
            return None

    frames: list[dict] = []
    events = await ws_mod._run_replay_and_complete(
        _OldOrch(), frames.append, "codex-5", drop_incomplete=True)

    assert events == []
    assert [f["type"] for f in frames] == ["replayReset", "replayComplete"]

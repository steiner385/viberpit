from __future__ import annotations
import json
import pytest
from server.sessions.replay import replay_window, replay_events


def _write_transcript(tmp_path, n_turns: int):
    """Write a JSONL transcript with n_turns user+assistant turn pairs."""
    p = tmp_path / "t.jsonl"
    lines = []
    for i in range(n_turns):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _turn_ids(window):
    from server.sessions import events as ev
    return [e.turn_id for e in window.events if isinstance(e, ev.TurnStart)]


@pytest.mark.asyncio
async def test_window_default_returns_last_n(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, limit=20)
    assert w.total_turns == 50
    assert w.earliest_turn == 30           # turns 30..49
    assert _turn_ids(w) == [f"a{i}" for i in range(30, 50)]


@pytest.mark.asyncio
async def test_window_before_cursor(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=30, limit=20)
    assert w.earliest_turn == 10           # turns 10..29
    assert _turn_ids(w) == [f"a{i}" for i in range(10, 30)]


@pytest.mark.asyncio
async def test_window_reaches_start(tmp_path):
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=10, limit=20)
    assert w.earliest_turn == 0            # clamped at start: turns 0..9
    assert _turn_ids(w) == [f"a{i}" for i in range(0, 10)]


@pytest.mark.asyncio
async def test_window_before_past_end_is_clamped(tmp_path):
    path = _write_transcript(tmp_path, 5)
    w = await replay_window(path, before=999, limit=20)
    assert w.total_turns == 5
    assert w.earliest_turn == 0
    assert _turn_ids(w) == [f"a{i}" for i in range(0, 5)]


@pytest.mark.asyncio
async def test_window_empty_transcript(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    w = await replay_window(str(p))
    assert w.total_turns == 0
    assert w.earliest_turn == 0
    assert w.events == []


@pytest.mark.asyncio
async def test_session_meta_present_in_every_window(tmp_path):
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=20, limit=10)
    assert any(isinstance(e, ev.SessionMeta) for e in w.events)


@pytest.mark.asyncio
async def test_replay_events_wrapper_unchanged(tmp_path):
    path = _write_transcript(tmp_path, 30)
    events = await replay_events(path, max_turns=20)
    from server.sessions import events as ev
    starts = [e for e in events if isinstance(e, ev.TurnStart)]
    assert len(starts) == 20

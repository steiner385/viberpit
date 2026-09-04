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
async def test_window_limit_none_returns_all_turns(tmp_path):
    """limit=None means NO cap: the full transcript from turn 0 (B1)."""
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 25)
    w = await replay_window(path, limit=None)
    assert w.total_turns == 25
    assert w.earliest_turn == 0
    assert _turn_ids(w) == [f"a{i}" for i in range(25)]
    # The very first turn's content is present, not sliced away.
    texts = [e.text for e in w.events if isinstance(e, ev.TextDelta)]
    assert "r0" in texts


def _write_megaturn_transcript(tmp_path, n_pre_turns: int, n_asst_msgs: int):
    """n_pre_turns completed user+assistant pairs, then ONE user prompt ("big")
    followed by n_asst_msgs assistant messages — the shape of a tool-heavy
    logical turn, where every SDK step is its own assistant JSONL entry."""
    p = tmp_path / "mega.jsonl"
    lines = []
    for i in range(n_pre_turns):
        lines.append({"type": "user", "uuid": f"u{i}",
                      "message": {"content": f"q{i}"}})
        lines.append({"type": "assistant", "uuid": f"a{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"r{i}"}],
                                  "usage": {"input_tokens": 1}}})
    lines.append({"type": "user", "uuid": "u-big", "message": {"content": "big"}})
    for i in range(n_asst_msgs):
        lines.append({"type": "assistant", "uuid": f"m{i}",
                      "message": {"model": "claude-x",
                                  "content": [{"type": "text", "text": f"step{i}"}],
                                  "usage": {"input_tokens": 1}}})
    p.write_text("\n".join(json.dumps(o) for o in lines))
    return str(p)


def _user_texts(window):
    from server.sessions import events as ev
    return [e.text for e in window.events if isinstance(e, ev.UserMessage)]


@pytest.mark.asyncio
async def test_midturn_tail_window_includes_owning_user_prompt(tmp_path):
    """A logical turn longer than the window limit must not lose its own prompt:
    the tail window slices into the middle of the mega-turn (turn counting is per
    assistant message), so without the prepend the replay carries the turn's
    content with NO UserMessage at all. Client-side that leaves the sent-prompt
    buffer unconsumed, and every reconnect re-asserts the prompt at the very
    bottom of the transcript — the "last prompt stuck at the bottom" bug."""
    from server.sessions import events as ev
    path = _write_megaturn_transcript(tmp_path, 3, 30)   # 33 assistant messages total
    w = await replay_window(path, limit=20)
    assert w.total_turns == 33
    assert w.earliest_turn == 13                         # window = m13..m29 region
    assert _user_texts(w) == ["big"]
    # The owning prompt renders ABOVE the turn content: it precedes every TurnStart.
    first_ts = next(i for i, e in enumerate(w.events) if isinstance(e, ev.TurnStart))
    user_at = next(i for i, e in enumerate(w.events) if isinstance(e, ev.UserMessage))
    assert user_at < first_ts


@pytest.mark.asyncio
async def test_midturn_pagination_window_includes_owning_user_prompt(tmp_path):
    """Scrolling back within a mega-turn pages in windows that also start
    mid-turn — they get the same owning-prompt prepend (the client dedupes the
    repeated turn id across pages)."""
    path = _write_megaturn_transcript(tmp_path, 3, 30)
    w = await replay_window(path, before=23, limit=10)   # m10..m19: mid-mega-turn
    assert _user_texts(w) == ["big"]


@pytest.mark.asyncio
async def test_window_starting_at_turn_boundary_gains_no_extra_prompt(tmp_path):
    """A window whose head already sits on a user boundary is unchanged."""
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, limit=20)              # turns 30..49, u30 included
    from server.sessions import events as ev
    users = [e.text for e in w.events if isinstance(e, ev.UserMessage)]
    assert users == [f"q{i}" for i in range(30, 50)]     # exactly the window's own


@pytest.mark.asyncio
async def test_trimmed_open_megaturn_prompt_stays_out(tmp_path):
    """drop_incomplete_turn trims the whole open turn — its prompt must NOT be
    re-added by the mid-turn prepend (the live resupply owns that turn)."""
    path = _write_megaturn_transcript(tmp_path, 3, 30)
    w = await replay_window(path, limit=20, drop_incomplete_turn=True)
    assert "big" not in _user_texts(w)


@pytest.mark.asyncio
async def test_replay_events_wrapper_unchanged(tmp_path):
    path = _write_transcript(tmp_path, 30)
    events = await replay_events(path, max_turns=20)
    from server.sessions import events as ev
    starts = [e for e in events if isinstance(e, ev.TurnStart)]
    assert len(starts) == 20


@pytest.mark.asyncio
async def test_replay_events_max_turns_none_returns_all(tmp_path):
    """max_turns=None → the FULL transcript. The sub-agent summary persister uses
    this: with the default 20-turn tail, agents spawned earlier in a long session
    silently vanished from the dashboard chips (sibling of the B1 cap)."""
    path = _write_transcript(tmp_path, 30)
    events = await replay_events(path, max_turns=None)
    from server.sessions import events as ev
    starts = [e for e in events if isinstance(e, ev.TurnStart)]
    assert len(starts) == 30


@pytest.mark.asyncio
async def test_window_turnstarts_carry_turn_index_in_cursor_space(tmp_path):
    """Each in-window TurnStart is stamped with its ordinal in the same space the
    `?before=` cursor / `earliest_turn` index, so the client can verify that page
    merges keep turn order (the page-boundary ordering bug, session 888ad2bf)."""
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 50)
    w = await replay_window(path, before=30, limit=20)
    idx = [e.turn_index for e in w.events if isinstance(e, ev.TurnStart)]
    assert idx == list(range(10, 30))
    assert idx[0] == w.earliest_turn


@pytest.mark.asyncio
async def test_window_turn_index_survives_owner_prefix_and_meta(tmp_path):
    """The owning-prompt prepend and the SessionMeta prepend shift list positions
    but must not shift ordinals."""
    from server.sessions import events as ev
    path = _write_megaturn_transcript(tmp_path, 3, 30)     # 33 assistant messages
    w = await replay_window(path, before=23, limit=10)     # m10..m19, prefixed with "big"
    starts = [e for e in w.events if isinstance(e, ev.TurnStart)]
    # Cursor space counts ALL turns (the 3 pre-turns too), so m10 (the 14th assistant
    # entry, 0-indexed 13) carries turn_index 13, matching w.earliest_turn below —
    # not its bare "m" suffix.
    assert [e.turn_index for e in starts] == list(range(13, 23))
    assert starts[0].turn_index == w.earliest_turn
    assert isinstance(w.events[0], ev.SessionMeta)
    assert isinstance(w.events[1], ev.UserMessage)


def test_live_turnstart_has_no_turn_index():
    """Live-translated turns are not positioned in the disk turn space."""
    from server.sessions import events as ev
    assert ev.TurnStart("a").to_wire()["turnIndex"] is None


@pytest.mark.asyncio
async def test_replay_events_turn_index_shares_cursor_space(tmp_path):
    """`_parse` is `_window(before=None, …)`, so replay_events consumers see the
    same untrimmed ordinals a `?before=` page would — never a different numbering."""
    from server.sessions import events as ev
    path = _write_transcript(tmp_path, 5)
    events = await replay_events(path, max_turns=2)
    assert [e.turn_index for e in events if isinstance(e, ev.TurnStart)] == [3, 4]

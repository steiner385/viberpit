import json
import asyncio
from server.sessions.replay import replay_window
from server.sessions import events as ev


def _write(tmp_path, rows):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return str(p)


def _turn(uuid_u, uuid_a, text):
    return [
        {"type": "user", "uuid": uuid_u, "message": {"role": "user", "content": text}},
        {"type": "assistant", "uuid": uuid_a,
         "message": {"role": "assistant", "model": "claude-x",
                     "content": [{"type": "text", "text": text + "-reply"}]}},
    ]


def test_drop_incomplete_turn_removes_trailing_user_onward(tmp_path):
    rows = _turn("u1", "a1", "first") + _turn("u2", "a2", "second")
    # trailing in-progress turn: real user + one completed assistant text block
    rows += [{"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
             {"type": "assistant", "uuid": "a3",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)

    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    users = [e for e in kept if isinstance(e, ev.UserMessage)]
    assert [u.text for u in users] == ["first", "second"]     # "third" dropped
    assert not any(getattr(e, "text", None) == "partial" for e in kept)

    full = asyncio.run(replay_window(path, drop_incomplete_turn=False)).events
    assert [u.text for u in full if isinstance(u, ev.UserMessage)] == ["first", "second", "third"]


def test_compact_summary_is_not_a_user_turn(tmp_path):
    """The CLI's auto-compaction summary is a ``type:"user"`` entry flagged
    ``isCompactSummary`` (NOT ``isMeta``). It is harness plumbing, not a real user
    prompt, so it must not render as a user bubble — otherwise a reloaded transcript
    shows a giant "This session is being continued…" message the live view never had."""
    rows = _turn("u1", "a1", "first")
    rows += [{"type": "user", "uuid": "cs", "isCompactSummary": True,
              "isVisibleInTranscriptOnly": True,
              "message": {"role": "user",
                          "content": "This session is being continued from a previous conversation…"}}]
    rows += _turn("u2", "a2", "second")
    path = _write(tmp_path, rows)
    full = asyncio.run(replay_window(path, drop_incomplete_turn=False)).events
    assert [u.text for u in full if isinstance(u, ev.UserMessage)] == ["first", "second"]


def test_drop_incomplete_turn_boundary_ignores_compact_summary(tmp_path):
    """Regression: a mid-session auto-compaction writes a ``isCompactSummary`` user
    entry. ``drop_incomplete_turn`` must anchor on the last REAL user prompt, not on
    the compaction summary — otherwise every completed post-compaction turn (which
    lives only on disk, not in the live in-progress buffer) is dropped on reattach and
    vanishes from the reloaded transcript."""
    rows = _turn("u1", "a1", "first")
    # auto-compaction boundary, then a COMPLETED post-compaction turn …
    rows += [{"type": "user", "uuid": "cs", "isCompactSummary": True,
              "isVisibleInTranscriptOnly": True,
              "message": {"role": "user", "content": "This session is being continued…"}}]
    rows += _turn("u2", "a2", "second")
    # … then the trailing in-progress turn owned by the live buffer.
    rows += [{"type": "user", "uuid": "u3", "message": {"role": "user", "content": "third"}},
             {"type": "assistant", "uuid": "a3",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "text", "text": "partial"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    # Only "third" (the real in-progress turn) is dropped; the completed post-compaction
    # "second" turn is preserved.
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first", "second"]
    assert any(getattr(e, "text", "") == "second-reply" for e in kept)
    assert not any(getattr(e, "text", "") == "partial" for e in kept)


def test_drop_incomplete_turn_ignores_tool_result_user_lines(tmp_path):
    rows = _turn("u1", "a1", "first")
    # in-progress turn: real user, assistant tool_use, then a tool_result "user" line
    rows += [{"type": "user", "uuid": "u2", "message": {"role": "user", "content": "second"}},
             {"type": "assistant", "uuid": "a2",
              "message": {"role": "assistant", "model": "claude-x",
                          "content": [{"type": "tool_use", "id": "tool1", "name": "Bash", "input": {}}]}},
             {"type": "user", "uuid": "ur",
              "message": {"role": "user",
                          "content": [{"type": "tool_result", "tool_use_id": "tool1", "content": "ok"}]}}]
    path = _write(tmp_path, rows)
    kept = asyncio.run(replay_window(path, drop_incomplete_turn=True)).events
    # The trim boundary is the last REAL user ("second"), not the tool_result line.
    assert [u.text for u in kept if isinstance(u, ev.UserMessage)] == ["first"]

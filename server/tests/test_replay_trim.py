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

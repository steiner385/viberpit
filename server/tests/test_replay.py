import asyncio
from pathlib import Path
from server.sessions.replay import replay_events
from server.sessions.events import TurnStart, TextDelta, ToolCall, ToolResult, TurnEnd, UserMessage

FIX = Path(__file__).parent / "fixtures" / "transcript.jsonl"

def test_replay_emits_summary_events():
    out = asyncio.run(replay_events(str(FIX)))
    wires = [e.to_wire()["type"] for e in out]
    assert wires[0] == "user"
    assert "turnStart" in wires and "toolCall" in wires and "toolResult" in wires and "turnEnd" in wires
    tc = next(e for e in out if isinstance(e, ToolCall))
    assert tc.name == "Read"
    res = next(e for e in out if isinstance(e, ToolResult))
    assert res.payload["content"] == "print(1)\n"


def test_replay_skips_malformed_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n'
        'this is not json\n'
        '{"type":"assistant","uuid":"u2","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    types = [e.to_wire()["type"] for e in out]
    assert "user" in types and "turnStart" in types and "turnEnd" in types


def test_replay_emits_session_meta_and_usage(tmp_path):
    """Re-attach must reconstruct the model (from the assistant message) and the
    per-result usage so the status strip can render context/quota after replay."""
    p = tmp_path / "m.jsonl"
    p.write_text(
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","model":"claude-opus-4-1",'
        '"content":[{"type":"text","text":"ok"}],'
        '"usage":{"input_tokens":10,"output_tokens":2,"cache_read_input_tokens":3}}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    metas = [e for e in out if e.to_wire()["type"] == "sessionMeta"]
    assert metas and metas[0].to_wire()["model"] == "claude-opus-4-1"
    te = next(e for e in out if e.to_wire()["type"] == "turnEnd")
    assert te.to_wire()["usage"] == {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 3}


def test_replay_handles_string_content(tmp_path):
    """Regression: a user message whose `content` is a plain STRING (not a list of
    blocks) must parse — otherwise iterating it yields chars and b.get() raises
    AttributeError, crashing replay → blank history."""
    p = tmp_path / "s.jsonl"
    p.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"tell me a lullaby"}}\n'
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":[{"type":"text","text":"ok"}]}}\n'
    )
    out = asyncio.run(replay_events(str(p)))
    user_evs = [e for e in out if e.to_wire()["type"] == "user"]
    assert user_evs, "string-content user message should produce a user event"
    assert user_evs[0].to_wire()["text"] == "tell me a lullaby"

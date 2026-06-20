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

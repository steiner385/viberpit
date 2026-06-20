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

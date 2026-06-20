from server.tests.fakes import fake_sdk as f
from server.sessions.translator import Translator
from server.sessions.events import (TurnStart, TextDelta, ThinkingDelta, ToolCall, ToolResult,
                                     TurnEnd, ToolKind)
from claude_agent_sdk import TextBlock

def _run(msgs):
    tr = Translator(); out = []
    for m in msgs:
        out += tr.feed(m)
    return tr, out

def test_simple_turn():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"),
            f.stream_text("Hello ", uuid="t1"), f.stream_text("there", uuid="t1"),
            f.assistant(TextBlock(text="Hello there"), uuid="t1"), f.result(session_id="s1", uuid="t1")]
    tr, out = _run(msgs)
    assert tr.session_id == "s1"
    assert [type(e).__name__ for e in out] == ["TurnStart", "TextDelta", "TextDelta", "TurnEnd"]
    assert out[1].text == "Hello " and out[1].turn_id == out[0].turn_id == "t1"

def test_thinking_delta():
    msgs = [f.stream_start(uuid="t1"), f.stream_thinking("hmm", uuid="t1"), f.result(uuid="t1")]
    _, out = _run(msgs)
    assert any(isinstance(e, ThinkingDelta) and e.text == "hmm" for e in out)

def test_tool_turn():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"), f.stream_text("Reading.", uuid="t1"),
            f.tool_use("tc1", "Read", {"file_path": "a.py"}, uuid="t1"),
            f.user_tool_result("tc1", "print(1)\n"), f.result(session_id="s1", uuid="t1")]
    _, out = _run(msgs)
    call = next(e for e in out if isinstance(e, ToolCall))
    res = next(e for e in out if isinstance(e, ToolResult))
    assert call.kind is ToolKind.READ and call.tool_id == "tc1" and call.turn_id == "t1"
    assert res.tool_id == "tc1" and res.ok is True and res.payload["content"] == "print(1)\n"

def test_tool_call_deduped():
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("tc1", "Bash", {"command": "ls"}, uuid="t1"),
            f.tool_use("tc1", "Bash", {"command": "ls"}, uuid="t1"),  # repeat (consolidated msg seen twice)
            f.result(uuid="t1")]
    _, out = _run(msgs)
    assert sum(1 for e in out if isinstance(e, ToolCall)) == 1

def test_turnstart_without_message_start_falls_back():
    # no stream_start: first AssistantMessage opens the turn
    msgs = [f.assistant(TextBlock(text="hi"), uuid="t9"), f.result(uuid="t9")]
    _, out = _run(msgs)
    assert isinstance(out[0], TurnStart) and out[0].turn_id == "t9"

def test_second_turn_resets_state():
    tr = Translator()
    out1 = []
    for m in [f.stream_start(uuid="t1"), f.tool_use("tc1", "Bash", {"command": "ls"}, uuid="t1"), f.result(uuid="t1")]:
        out1 += tr.feed(m)
    out2 = []
    for m in [f.stream_start(uuid="t2"), f.tool_use("tc1", "Bash", {"command": "ls"}, uuid="t2"), f.result(uuid="t2")]:
        out2 += tr.feed(m)
    assert isinstance(out2[0], TurnStart) and out2[0].turn_id == "t2"
    assert sum(1 for e in out2 if isinstance(e, ToolCall)) == 1  # not deduped across turns

from server.tests.fakes import fake_sdk as f
from server.sessions.translator import Translator
from server.sessions.events import (TurnStart, ThinkingDelta, ToolCall, ToolResult,
                                     TurnEnd, ToolKind, SessionMeta)
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

def test_init_emits_session_meta_with_model():
    msgs = [f.sys_init("s1", model="claude-sonnet-4-5"), f.stream_start(uuid="t1"),
            f.result(session_id="s1", uuid="t1")]
    _, out = _run(msgs)
    metas = [e for e in out if isinstance(e, SessionMeta)]
    assert len(metas) == 1
    assert metas[0].model == "claude-sonnet-4-5"
    assert metas[0].session_id == "s1"

def test_init_without_model_emits_no_session_meta():
    msgs = [f.sys_init("s1"), f.stream_start(uuid="t1"), f.result(session_id="s1", uuid="t1")]
    _, out = _run(msgs)
    assert not any(isinstance(e, SessionMeta) for e in out)

def test_turn_end_carries_usage_and_cost():
    msgs = [f.stream_start(uuid="t1"),
            f.result(session_id="s1", usage={"input_tokens": 5}, cost=0.25, uuid="t1")]
    _, out = _run(msgs)
    te = next(e for e in out if isinstance(e, TurnEnd))
    assert te.usage == {"input_tokens": 5}
    assert te.cost_usd == 0.25

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


def test_askuserquestion_tool_use_emits_no_toolcall():
    """AskUserQuestion tool_use blocks must NOT produce a ToolCall event.
    The driver emits ev.Question instead; a raw ToolCall alongside it would make
    the frontend render a generic JSON card on top of the QuestionCard."""
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("ask1", "AskUserQuestion",
                       {"questions": [{"question": "Q?", "options": []}]}, uuid="t1"),
            f.result(uuid="t1")]
    _, out = _run(msgs)
    tool_calls = [e for e in out if isinstance(e, ToolCall)]
    assert tool_calls == [], (
        f"expected no ToolCall for AskUserQuestion, got {tool_calls}"
    )


# ---- outcome classification tests ----
from server.sessions.completion import COMPLETION_MARKER


def _drive(msgs):
    tr = Translator()
    out = []
    for m in msgs:
        out.extend(tr.feed(m))
    return out


def _turn_end(events):
    from server.sessions.events import TurnEnd
    return next(e for e in events if isinstance(e, TurnEnd))


def test_turn_end_outcome_success_without_marker():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="all set"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "success"


def test_turn_end_outcome_complete_with_marker():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"done\n{COMPLETION_MARKER}"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "complete"


def test_turn_end_outcome_failed_on_error_subtype():
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="boom"), uuid="t1"),
            f.result(session_id="s", uuid="t1", subtype="error_during_execution", is_error=True)]
    assert _turn_end(_drive(msgs)).outcome == "failed"


def test_marker_on_an_errored_turn_is_still_failed():
    # subtype precedence: an errored turn is never 'complete' even if text has the marker.
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"{COMPLETION_MARKER}"), uuid="t1"),
            f.result(session_id="s", uuid="t1", subtype="error_max_turns", is_error=True)]
    assert _turn_end(_drive(msgs)).outcome == "failed"

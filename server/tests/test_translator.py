from server.tests.fakes import fake_sdk as f
from server.sessions.translator import Translator
from server.sessions.events import (TurnStart, ThinkingDelta, ToolCall, ToolResult,
                                     TurnEnd, ToolKind, SessionMeta, TextDelta, ErrorEvent)
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


def test_success_result_emits_no_error_event():
    msgs = [f.stream_start(uuid="t1"), f.stream_text("hi", uuid="t1"), f.result(uuid="t1")]
    _, out = _run(msgs)
    assert not any(isinstance(e, ErrorEvent) for e in out)
    assert any(isinstance(e, TurnEnd) for e in out)


def test_max_turns_error_emits_error_event_before_turn_end():
    msgs = [f.stream_start(uuid="t1"),
            f.result(uuid="t1", subtype="error_max_turns", is_error=True,
                     result="Reached the maximum number of turns")]
    _, out = _run(msgs)
    err = next(e for e in out if isinstance(e, ErrorEvent))
    end = next(e for e in out if isinstance(e, TurnEnd))
    assert err.code == "max_turns"
    assert "maximum number of turns" in err.message
    assert err.turn_id == "t1"
    # error precedes the turnEnd so the UI shows it inline before the turn closes
    assert out.index(err) < out.index(end)


def test_execution_error_maps_to_execution_error_code():
    msgs = [f.stream_start(uuid="t1"),
            f.result(uuid="t1", subtype="error_during_execution", is_error=True,
                     errors=["boom", "kapow"])]
    _, out = _run(msgs)
    err = next(e for e in out if isinstance(e, ErrorEvent))
    assert err.code == "execution_error"
    assert "boom" in err.message and "kapow" in err.message


def test_api_error_status_maps_to_api_code():
    msgs = [f.stream_start(uuid="t1"),
            f.result(uuid="t1", subtype="success", is_error=True, api_error_status=429)]
    _, out = _run(msgs)
    err = next(e for e in out if isinstance(e, ErrorEvent))
    assert err.code == "api_429"
    assert "429" in err.message


def test_error_result_without_text_has_default_message():
    msgs = [f.stream_start(uuid="t1"),
            f.result(uuid="t1", subtype="error_during_execution", is_error=True)]
    _, out = _run(msgs)
    err = next(e for e in out if isinstance(e, ErrorEvent))
    assert err.message  # non-empty fallback


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

def test_subagent_events_tagged_and_separately_turned():
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("task1", "Task", {"subagent_type": "Explore", "description": "x"}, uuid="t1"),
            f.stream_start(uuid="sa1", pid="task1"),
            f.stream_text("sub-text", uuid="sa1", pid="task1"),
            f.tool_use("g1", "Grep", {"pattern": "foo"}, uuid="sa1", pid="task1"),
            f.result(uuid="t1")]
    _, out = _run(msgs)
    task = next(e for e in out if isinstance(e, ToolCall) and e.name == "Task")
    grep = next(e for e in out if isinstance(e, ToolCall) and e.name == "Grep")
    assert task.parent_tool_use_id is None
    assert grep.parent_tool_use_id == "task1"
    sub_text = next(e for e in out if isinstance(e, TextDelta) and e.parent_tool_use_id == "task1")
    main_start = next(e for e in out if isinstance(e, TurnStart) and e.parent_tool_use_id is None)
    assert sub_text.turn_id != main_start.turn_id

def test_parallel_subagents_get_distinct_turns():
    msgs = [f.stream_start(uuid="t1"),
            f.tool_use("taskA", "Task", {}, uuid="t1"),
            f.tool_use("taskB", "Task", {}, uuid="t1"),
            f.stream_start(uuid="saA", pid="taskA"), f.stream_text("A", uuid="saA", pid="taskA"),
            f.stream_start(uuid="saB", pid="taskB"), f.stream_text("B", uuid="saB", pid="taskB"),
            f.result(uuid="t1")]
    _, out = _run(msgs)
    starts = {e.parent_tool_use_id: e.turn_id for e in out if isinstance(e, TurnStart)}
    assert starts["taskA"] != starts["taskB"]
    assert starts[None] not in (starts["taskA"], starts["taskB"])


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


def test_sub_agent_marker_does_not_complete_parent_turn():
    # A Task sub-agent emits the marker for its own sub-objective; the top-level
    # final message has no marker. The parent turn must NOT be 'complete'.
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"sub done {COMPLETION_MARKER}"), uuid="sub", parent_tool_use_id="task-1"),
            f.assistant(TextBlock(text="top-level still working"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "success"


def test_marker_in_final_top_level_message_completes():
    # Sub-agent runs, then the TOP-LEVEL agent's final message carries the marker.
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text="delegating"), uuid="sub", parent_tool_use_id="task-1"),
            f.assistant(TextBlock(text=f"all objectives met {COMPLETION_MARKER}"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "complete"


def test_marker_only_in_interim_top_level_message_is_not_complete():
    # Reset-per-message: a non-final top-level message mentions the marker, but the
    # FINAL top-level message does not. Per spec ("final message"), not complete.
    msgs = [f.sys_init("s"), f.stream_start(uuid="t1"),
            f.assistant(TextBlock(text=f"I will end with {COMPLETION_MARKER} when done"), uuid="t1"),
            f.assistant(TextBlock(text="actually not done yet"), uuid="t1"),
            f.result(session_id="s", uuid="t1")]
    assert _turn_end(_drive(msgs)).outcome == "success"

"""Replay must reconstruct AskUserQuestion as Question events, not raw ToolCalls.

The live translator skips emitting a ToolCall for AskUserQuestion (the driver
emits ev.Question, rendered as the selectable QuestionCard). Replay rebuilds the
transcript from the CLI's JSONL, where AskUserQuestion exists only as a tool_use
block — so without this special-casing a detached/reloaded session shows the raw
input JSON instead of the question UI.
"""
import json

from server.sessions.replay import _parse
from server.sessions import events as ev


def test_ask_user_question_replays_as_question_not_toolcall(tmp_path):
    line = {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_x", "name": "AskUserQuestion", "input": {"questions": [
            {"header": "Layout", "question": "Two-pane?", "multiSelect": False, "options": [
                {"label": "Yes", "description": "master-detail"},
                {"label": "No", "description": "single column"},
            ]},
        ]}},
    ]}}
    p = tmp_path / "q.jsonl"
    p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)

    # No raw AskUserQuestion ToolCall (which would render as JSON).
    assert not any(isinstance(e, ev.ToolCall) and e.name == "AskUserQuestion" for e in out)
    # A Question event reconstructed from the tool_use input.
    qs = [e for e in out if isinstance(e, ev.Question)]
    assert len(qs) == 1
    q = qs[0]
    assert q.header == "Layout"
    assert q.question == "Two-pane?"
    assert q.multi_select is False
    assert [o["label"] for o in q.options] == ["Yes", "No"]
    assert q.turn_id == "a1"


def test_ask_user_question_multiple_questions_each_replay(tmp_path):
    line = {"type": "assistant", "uuid": "a2", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_y", "name": "AskUserQuestion", "input": {"questions": [
            {"header": "A", "question": "Q1?", "options": [{"label": "x"}]},
            {"header": "B", "question": "Q2?", "multiSelect": True, "options": [{"label": "y"}]},
        ]}},
    ]}}
    p = tmp_path / "q2.jsonl"
    p.write_text(json.dumps(line) + "\n")
    qs = [e for e in _parse(str(p), max_turns=20) if isinstance(e, ev.Question)]
    assert [q.question for q in qs] == ["Q1?", "Q2?"]
    assert qs[1].multi_select is True
    # Distinct synthesized ids so the frontend's idempotency keys don't collide.
    assert qs[0].question_id != qs[1].question_id


def test_non_askuserquestion_tool_still_renders_as_toolcall(tmp_path):
    line = {"type": "assistant", "uuid": "a3", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_z", "name": "Bash", "input": {"command": "ls"}},
    ]}}
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(line) + "\n")
    out = _parse(str(p), max_turns=20)
    assert any(isinstance(e, ev.ToolCall) and e.name == "Bash" for e in out)
    assert not any(isinstance(e, ev.Question) for e in out)

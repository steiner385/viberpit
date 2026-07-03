import server.sessions.events as ev
from server.sessions.events import (ToolKind, tool_kind, TextDelta, ToolCall, Status, SessionMeta,
                                     TurnEnd, TurnStart, ThinkingDelta, ToolResult, QueueUpdate)

def test_tool_kind_maps_known_names():
    assert tool_kind("Edit") is ToolKind.EDIT
    assert tool_kind("Bash") is ToolKind.BASH
    assert tool_kind("Grep") is ToolKind.SEARCH
    assert tool_kind("SomethingNew") is ToolKind.UNKNOWN

def test_tool_kind_maps_web_task_and_mcp():
    assert tool_kind("WebFetch") is ToolKind.WEB
    assert tool_kind("WebSearch") is ToolKind.WEB
    assert tool_kind("Task") is ToolKind.TASK
    # Any mcp__<server>__<tool> name is the MCP kind, regardless of server/tool.
    assert tool_kind("mcp__github__list_prs") is ToolKind.MCP
    assert tool_kind("mcp__plugin_railway_railway__deploy") is ToolKind.MCP

def test_textdelta_to_wire():
    e = TextDelta(turn_id="t1", text="hi", src_uuid="u1")
    w = e.to_wire()
    assert w["type"] == "textDelta" and w["turnId"] == "t1" and w["text"] == "hi"
    assert w["srcUuid"] == "u1" and w["parentToolUseId"] is None

def test_status_to_wire_is_structured():
    assert Status(state="queued", count=2).to_wire() == {"type": "status", "state": "queued", "count": 2}

def test_toolcall_carries_kind_and_turn():
    e = ToolCall(turn_id="t1", tool_id="x", name="Edit", kind=ToolKind.EDIT, input={"file_path": "a"}, src_uuid="u2")
    w = e.to_wire()
    assert w["type"] == "toolCall" and w["toolKind"] == "edit" and w["turnId"] == "t1"

def test_session_meta_to_wire():
    e = SessionMeta(model="claude-sonnet-4-5", session_id="s1")
    assert e.to_wire() == {"type": "sessionMeta", "model": "claude-sonnet-4-5", "sessionId": "s1"}

def test_turn_end_to_wire_includes_cost():
    e = TurnEnd(turn_id="t1", usage={"input_tokens": 5}, cost_usd=0.25, src_uuid="u1")
    w = e.to_wire()
    assert w["type"] == "turnEnd" and w["usage"] == {"input_tokens": 5}
    assert w["costUsd"] == 0.25 and w["turnId"] == "t1"

def test_block_events_carry_parent_tool_use_id_on_wire():
    tc = ToolCall("t1", "g1", "Grep", ToolKind.SEARCH, {}, src_uuid="u1",
                  parent_tool_use_id="task1")
    assert tc.to_wire()["parentToolUseId"] == "task1"

    td = TextDelta("t1", "hi", src_uuid="u1", parent_tool_use_id="task1")
    assert td.to_wire()["parentToolUseId"] == "task1"

    tr = ToolResult("g1", True, {"content": "x"}, src_uuid="u1", parent_tool_use_id="task1")
    assert tr.to_wire()["parentToolUseId"] == "task1"

def test_block_events_default_parent_to_none_on_wire():
    for e in (TurnStart("t1"), TurnEnd("t1"), ThinkingDelta("t1", "z")):
        assert e.to_wire()["parentToolUseId"] is None

def test_queue_update_wire_carries_text_and_images():
    qu = QueueUpdate([
        {"text": "hello", "images": []},
        {"text": "look", "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]},
    ])
    assert qu.to_wire() == {
        "type": "queueUpdate",
        "items": [
            {"text": "hello", "images": []},
            {"text": "look", "images": [{"url": "/api/attachments/a", "mime": "image/png", "name": "s.png"}]},
        ],
    }


def test_block_events_carry_ts_on_wire():
    for e in [
        ev.UserMessage(turn_id="t", text="hi"),
        ev.TextDelta("t", "hi"),
        ev.ThinkingDelta("t", "mm"),
        ev.ToolCall("t", "id", "Bash", ev.tool_kind("Bash"), {}),
        ev.ToolResult("id", True, {}),
        ev.PermissionRequest("p", "t", "Bash", ev.tool_kind("Bash"), {}),
        ev.Question("q", "t", "h", "q?", []),
        ev.ErrorEvent("code", "msg"),
    ]:
        wire = e.to_wire()
        assert "ts" in wire, f"{type(e).__name__} missing ts"
        assert isinstance(wire["ts"], float) and wire["ts"] > 0


def test_replay_can_override_ts():
    # Replay passes an explicit ts (may be None when the line lacks a timestamp).
    assert ev.TextDelta("t", "hi", ts=None).to_wire()["ts"] is None
    assert ev.TextDelta("t", "hi", ts=123.0).to_wire()["ts"] == 123.0


def test_recovering_to_wire():
    e = ev.Recovering(turn_id="t1", message="Recovering the dropped turn…")
    w = e.to_wire()
    assert w["type"] == "recovering"
    assert w["turnId"] == "t1"
    assert w["message"] == "Recovering the dropped turn…"
    assert "ts" in w

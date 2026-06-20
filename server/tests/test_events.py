from server.sessions.events import ToolKind, tool_kind, TextDelta, ToolCall, Status

def test_tool_kind_maps_known_names():
    assert tool_kind("Edit") is ToolKind.EDIT
    assert tool_kind("Bash") is ToolKind.BASH
    assert tool_kind("Grep") is ToolKind.SEARCH
    assert tool_kind("SomethingNew") is ToolKind.UNKNOWN

def test_textdelta_to_wire():
    e = TextDelta(turn_id="t1", text="hi", src_uuid="u1")
    assert e.to_wire() == {"type": "textDelta", "turnId": "t1", "text": "hi", "srcUuid": "u1"}

def test_status_to_wire_is_structured():
    assert Status(state="queued", count=2).to_wire() == {"type": "status", "state": "queued", "count": 2}

def test_toolcall_carries_kind_and_turn():
    e = ToolCall(turn_id="t1", tool_id="x", name="Edit", kind=ToolKind.EDIT, input={"file_path": "a"}, src_uuid="u2")
    w = e.to_wire()
    assert w["type"] == "toolCall" and w["toolKind"] == "edit" and w["turnId"] == "t1"

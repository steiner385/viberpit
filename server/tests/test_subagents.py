from server.sessions import events as ev
from server.sessions.subagents import summarize_transcript


def _task_call(tool_id, atype="Explore", desc="find X"):
    return ev.ToolCall("turn", tool_id, "Task", ev.tool_kind("Task"),
                       {"subagent_type": atype, "description": desc}, "u")


def test_running_when_no_result():
    out = summarize_transcript([_task_call("toolu_A")])
    assert out == [{"agentId": "toolu_A", "parentAgentId": None,
                    "agentType": "Explore", "description": "find X", "status": "running"}]


def test_done_and_failed_from_results():
    evs = [_task_call("toolu_A"), _task_call("toolu_B"),
           ev.ToolResult("toolu_A", True, {}, "u"),
           ev.ToolResult("toolu_B", False, {}, "u")]
    by = {s["agentId"]: s["status"] for s in summarize_transcript(evs)}
    assert by == {"toolu_A": "done", "toolu_B": "failed"}


def test_agent_tool_name_also_counted_and_order_preserved():
    evs = [_task_call("toolu_A"),
           ev.ToolCall("turn", "toolu_B", "Agent", ev.tool_kind("Agent"), {}, "u")]
    out = summarize_transcript(evs)
    assert [s["agentId"] for s in out] == ["toolu_A", "toolu_B"]
    assert out[1]["agentType"] == "agent"  # missing subagent_type falls back


def test_ignores_non_subagent_tools():
    evs = [ev.ToolCall("turn", "t1", "Read", ev.tool_kind("Read"), {}, "u")]
    assert summarize_transcript(evs) == []


def test_nested_task_call_names_its_parent_thread():
    """A Task issued INSIDE a sub-agent's thread carries that thread's key as its
    parent_tool_use_id (replay tags sidecar tool calls with the spawning Task id), so
    the summary nests it instead of flattening every agent to top level."""
    evs = [_task_call("toolu_A"),
           ev.ToolCall("turn-sub", "toolu_B", "Task", ev.tool_kind("Task"),
                       {"subagent_type": "Explore", "description": "deeper"}, "u",
                       parent_tool_use_id="toolu_A")]
    by = {s["agentId"]: s["parentAgentId"] for s in summarize_transcript(evs)}
    assert by == {"toolu_A": None, "toolu_B": "toolu_A"}

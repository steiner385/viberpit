from server.sessions import events as ev

def test_content_events_carry_agent_id_in_wire():
    assert ev.TextDelta("t1", "hi", "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"
    assert ev.ToolCall("t1", "tc1", "Read", ev.tool_kind("Read"), {}, "u1").to_wire()["agentId"] is None
    assert ev.TurnStart("t1", "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"
    assert ev.ToolResult("tc1", True, {}, "u1", agent_id="toolu_A").to_wire()["agentId"] == "toolu_A"

def test_subagent_update_wire():
    e = ev.SubAgentUpdate(agent_id="toolu_A", parent_agent_id=None,
                          agent_type="Explore", description="find X", status="running")
    assert e.to_wire() == {"type": "subAgentUpdate", "agentId": "toolu_A",
                           "parentAgentId": None, "agentType": "Explore",
                           "description": "find X", "status": "running"}

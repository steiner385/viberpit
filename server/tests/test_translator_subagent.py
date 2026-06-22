import dataclasses as dc
from claude_agent_sdk import (SystemMessage, AssistantMessage, UserMessage, ResultMessage,
                              ToolUseBlock, ToolResultBlock, TextBlock)
from server.sessions.translator import Translator
from server.sessions import events as ev

def _types(out): return [type(e).__name__ for e in out]

def test_task_call_emits_subagent_update_and_tags_main_null():
    tr = Translator()
    # Main agent issues a Task tool call (parent_tool_use_id is None).
    am = AssistantMessage(content=[ToolUseBlock(id="toolu_A", name="Task",
                                                input={"subagent_type": "Explore", "description": "find X"})],
                          model="m", parent_tool_use_id=None, uuid="u-main")
    out = tr.feed(am)
    sub = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    calls = [e for e in out if isinstance(e, ev.ToolCall)]
    assert calls and calls[0].agent_id is None and calls[0].tool_id == "toolu_A"
    assert sub and sub[0].agent_id == "toolu_A" and sub[0].parent_agent_id is None
    assert sub[0].agent_type == "Explore" and sub[0].status == "running"

def test_subagent_text_tagged_with_agent_id():
    tr = Translator()
    am = AssistantMessage(content=[TextBlock(text="working")], model="m",
                          parent_tool_use_id="toolu_A", uuid="u-sub")
    out = tr.feed(am)
    # Sub-agent tool/text events must carry agent_id = "toolu_A"
    starts = [e for e in out if isinstance(e, ev.TurnStart)]
    assert starts and starts[0].agent_id == "toolu_A"

def test_task_result_emits_done():
    tr = Translator()
    tr.feed(AssistantMessage(content=[ToolUseBlock(id="toolu_A", name="Task",
                                                   input={"subagent_type": "Explore", "description": "d"})],
                             model="m", parent_tool_use_id=None, uuid="u-main"))
    out = tr.feed(UserMessage(content=[ToolResultBlock(tool_use_id="toolu_A", content="ok", is_error=False)],
                              parent_tool_use_id=None, uuid="u-res"))
    sub = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    assert sub and sub[0].agent_id == "toolu_A" and sub[0].status == "done"

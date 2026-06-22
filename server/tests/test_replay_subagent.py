import asyncio, os
from server.sessions.replay import replay_events
from server.sessions import events as ev

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_subagent.jsonl")
FIX_PTUI = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_subagent_ptui.jsonl")
FIX_PLAIN = os.path.join(os.path.dirname(__file__), "fixtures", "transcript.jsonl")

def _run(coro): return asyncio.run(coro)

def test_replay_tags_sidechain_with_agent_id_and_excludes_from_main():
    out = _run(replay_events(FIX))
    # There is at least one SubAgentUpdate and at least one event tagged with an agent_id.
    subs = [e for e in out if isinstance(e, ev.SubAgentUpdate)]
    assert subs, "expected a SubAgentUpdate from the Task in the fixture"
    sub_id = subs[0].agent_id
    tagged = [e for e in out if getattr(e, "agent_id", None) == sub_id]
    assert tagged, "sidechain events must be tagged with the sub-agent's agent_id"
    # The main thread (agent_id is None) must NOT contain the sub-agent's text.
    main_text = "".join(getattr(e, "text", "") for e in out
                        if isinstance(e, ev.TextDelta) and getattr(e, "agent_id", None) is None)
    sub_text = "".join(getattr(e, "text", "") for e in tagged if isinstance(e, ev.TextDelta))
    if sub_text:
        assert sub_text not in main_text


def test_replay_ptui_top_level_wins_over_isSidechain():
    """Entries with top-level parent_tool_use_id get agent_id = that value (branch 1 wins).

    The fixture has 'parent_tool_use_id' at both the entry top-level AND inside the
    message dict, covering both shapes.  Both should resolve to the ptui value, NOT
    the isSidechain grouping.
    """
    out = _run(replay_events(FIX_PTUI))

    # sub1 entry has top-level parent_tool_use_id = "toolu_top1"
    # → its events should carry agent_id == "toolu_top1"
    sub1_events = [e for e in out if getattr(e, "agent_id", None) == "toolu_top1"]
    assert sub1_events, "events from entry with parent_tool_use_id=toolu_top1 must get agent_id=toolu_top1"

    # sub2 entry has message-level parent_tool_use_id = "toolu_nested1"
    # → its events should carry agent_id == "toolu_nested1"
    sub2_events = [e for e in out if getattr(e, "agent_id", None) == "toolu_nested1"]
    assert sub2_events, "events from entry with message.parent_tool_use_id=toolu_nested1 must get agent_id=toolu_nested1"


def test_replay_ptui_nested_task_emits_subagent_update_with_correct_parent():
    """A Task tool_use inside a sub-agent entry must emit SubAgentUpdate(running) with
    parent_agent_id = the issuing entry's agent_id (not None).

    The ptui fixture has a Task issued inside sub1 (agent_id="toolu_top1").
    The SubAgentUpdate for toolu_nested1 must have:
      - agent_id = "toolu_nested1"
      - parent_agent_id = "toolu_top1"   (the issuing sub-agent's id)
    And the terminal update (from the tool_result) must match the same parent.
    """
    out = _run(replay_events(FIX_PTUI))
    subs = [e for e in out if isinstance(e, ev.SubAgentUpdate)]

    # The "toolu_nested1" sub-agent should have been created by sub1 (agent_id="toolu_top1")
    nested_running = [e for e in subs if e.agent_id == "toolu_nested1" and e.status == "running"]
    assert nested_running, "nested Task must emit a SubAgentUpdate(running) for toolu_nested1"
    assert nested_running[0].parent_agent_id == "toolu_top1", (
        f"nested SubAgentUpdate must have parent_agent_id='toolu_top1', got {nested_running[0].parent_agent_id!r}"
    )

    nested_terminal = [e for e in subs if e.agent_id == "toolu_nested1" and e.status in ("done", "failed")]
    assert nested_terminal, "nested Task must emit a terminal SubAgentUpdate(done/failed) for toolu_nested1"
    assert nested_terminal[0].parent_agent_id == "toolu_top1", (
        f"nested terminal SubAgentUpdate must have parent_agent_id='toolu_top1', got {nested_terminal[0].parent_agent_id!r}"
    )


def test_replay_plain_transcript_all_main_agent():
    """The no-sidechain, no-ptui transcript.jsonl must replay with every event's agent_id == None.

    Regression: confirms backward-compat — existing sessions without sub-agents produce no
    sub-agent tagging.
    """
    out = _run(replay_events(FIX_PLAIN))
    tagged = [e for e in out if getattr(e, "agent_id", None) is not None]
    assert tagged == [], (
        f"transcript.jsonl has no sub-agents; expected all agent_id=None, "
        f"but {len(tagged)} event(s) were tagged: {tagged[:3]}"
    )

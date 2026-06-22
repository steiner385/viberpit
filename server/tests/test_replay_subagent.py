import asyncio, os
from server.sessions.replay import replay_events
from server.sessions import events as ev

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "transcript_subagent.jsonl")

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

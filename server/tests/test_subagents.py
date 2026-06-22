from server.sessions import events as ev
from server.sessions.subagents import summarize

def test_summarize_latest_status_wins():
    evs = [
        ev.SubAgentUpdate("toolu_A", None, "Explore", "find X", "running"),
        ev.SubAgentUpdate("toolu_B", "toolu_A", "agent", "nested", "running"),
        ev.SubAgentUpdate("toolu_A", None, "Explore", "find X", "done"),
    ]
    out = summarize(evs)
    by_id = {s["agentId"]: s for s in out}
    assert by_id["toolu_A"]["status"] == "done"
    assert by_id["toolu_B"]["parentAgentId"] == "toolu_A"
    assert len(out) == 2

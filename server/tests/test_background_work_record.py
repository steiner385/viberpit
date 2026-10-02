"""SessionRecord carries background_work (and sub_agents) across the daemon→edge RPC
(#2635). sub_agents was never in to_dict/from_dict, so fleet-mode cards showed no
sub-agent rows — fixed in the same hunk."""
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _rec(**over):
    base = dict(id="s1", name="n", account="a", engine=AgentEngine.CLAUDE_CODE,
                engine_session_id="c1", cols=80, rows=24, cwd="/tmp", status="done")
    base.update(over)
    return SessionRecord(**base)


def test_background_work_and_sub_agents_round_trip():
    snap = {"items": [{"toolUseId": "tu1", "kind": "task", "name": "Bash",
                       "description": "pytest", "startedAt": None}],
            "wakeup": False, "wakeupAt": None}
    agents = [{"agentId": "tu9", "parentAgentId": None, "agentType": "Explore",
               "description": "d", "status": "running"}]
    d = _rec(background_work=snap, sub_agents=agents).to_dict()
    assert d["backgroundWork"] == snap and d["subAgents"] == agents
    back = SessionRecord.from_dict(d)
    assert back.background_work == snap and back.sub_agents == agents


def test_from_dict_tolerates_an_older_daemon_without_the_keys():
    d = _rec().to_dict()
    d.pop("backgroundWork")
    d.pop("subAgents")
    back = SessionRecord.from_dict(d)
    assert back.background_work is None and back.sub_agents is None

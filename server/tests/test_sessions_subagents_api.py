"""Tests for _record_to_dict sub-agent serialisation (Task 5)."""
from server.api.sessions import _record_to_dict


class _Rec:
    id = "s1"
    name = "n"
    account = "a"
    status = "active"
    repo = None
    created_at = updated_at = None
    pid = 1
    cwd = "/tmp"
    title = None
    permission_mode = "interactive"
    claude_session_id = None
    name_is_auto = False
    tool_allowlist = None
    detached = False
    sub_agents = [{"agentId": "toolu_A", "parentAgentId": None,
                   "agentType": "Explore", "description": "d", "status": "done"}]


def test_record_to_dict_includes_subagents():
    d = _record_to_dict(_Rec())
    assert d["subAgents"][0]["agentId"] == "toolu_A"


def test_record_to_dict_subagents_empty_when_null():
    r = _Rec()
    r.sub_agents = None
    assert _record_to_dict(r)["subAgents"] == []

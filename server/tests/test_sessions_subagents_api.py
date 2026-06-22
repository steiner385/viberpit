"""Tests for _record_to_dict sub-agent serialisation (Task 5)."""
from server.api.sessions import _record_to_dict
from server.sessions.types import SessionRecord


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


def test_real_session_record_with_sub_agents():
    """SessionRecord.sub_agents field populates subAgents in _record_to_dict (active-session path)."""
    agent_summary = {"agentId": "toolu_B", "parentAgentId": None,
                     "agentType": "Explore", "description": "search", "status": "done"}
    record = SessionRecord(
        id="sess-real-1",
        name="real session",
        account="acct1",
        claude_session_id="csid-1",
        cols=80,
        rows=24,
        cwd="/tmp",
        sub_agents=[agent_summary],
    )
    d = _record_to_dict(record)
    assert d["subAgents"] == [agent_summary]

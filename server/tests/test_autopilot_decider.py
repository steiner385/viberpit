# server/tests/test_autopilot_decider.py
import pytest
from server.automations.autopilot_policy import DecisionContext
from server.automations.autopilot_decider import decide


def _ctx():
    return DecisionContext(
        session_id="s1", status="waiting_input", account="a", repo=None, branch=None,
        effective_goal="ship the PR", goal_source="explicit",
        stuck={"kind": "permission", "tool_name": "Bash", "tool_input": {"command": "pytest"}},
        transcript_tail=[], ledger={})


def _fake_query(reply_text):
    async def gen(*, prompt, options):
        class _Block:
            text = reply_text
        class _Msg:
            content = [_Block()]
        yield _Msg()
    return gen


@pytest.mark.asyncio
async def test_valid_json_parses_to_decision():
    fn = _fake_query('{"action": "approve", "reason": "safe test run"}')
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "approve" and d.reason == "safe test run"


@pytest.mark.asyncio
async def test_malformed_reply_escalates():
    fn = _fake_query("not json at all")
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"


@pytest.mark.asyncio
async def test_model_raising_escalates():
    async def boom(*, prompt, options):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    d = await decide(_ctx(), _query_fn=boom, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"

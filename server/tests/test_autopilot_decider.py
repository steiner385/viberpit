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


# Production failure shape (2026-07-03): the model role-slips into prose before the
# JSON. Prose with brace-bearing text around a fenced block must still parse —
# the old greedy find("{")..rfind("}") slice raised "Extra data" here.
_PROSE_WITH_FENCED_JSON = (
    'I see you\'re in autopilot mode {status: needs_attention} so let me think.\n'
    'Some more prose with a stray { brace and } another.\n'
    '```json\n{"action": "nudge", "prompt": "continue the work", "reason": "resume"}\n```\n'
    'Trailing remark.'
)


@pytest.mark.asyncio
async def test_prose_around_fenced_json_still_parses():
    fn = _fake_query(_PROSE_WITH_FENCED_JSON)
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "nudge"
    assert d.prompt == "continue the work"


@pytest.mark.asyncio
async def test_prose_with_unfenced_json_object_still_parses():
    # No fences: prose, then a valid object, then trailing prose with braces.
    fn = _fake_query('Thinking aloud first.\n'
                     '{"action": "complete", "reason": "goal met"}\n'
                     'PS: {unrelated} trailing {braces}.')
    d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "complete"


@pytest.mark.asyncio
async def test_parse_failure_logs_raw_reply(caplog):
    import logging
    fn = _fake_query("pure prose, no json anywhere")
    with caplog.at_level(logging.WARNING, logger="server.automations.autopilot_decider"):
        d = await decide(_ctx(), _query_fn=fn, _config_dir="", _cwd="/tmp")
    assert d.action == "escalate"
    # The raw reply head must be in the log so production failures are diagnosable.
    assert any("pure prose" in r.getMessage() for r in caplog.records)


def test_prompt_demands_json_only():
    # The output-format demand must live in the USER prompt too (the proven
    # rank_issues pattern) — a system-prompt-only instruction drifts under weak
    # context and the model answers in prose.
    from server.automations.autopilot_decider import _build_prompt
    p = _build_prompt(_ctx())
    assert "ONLY" in p and "JSON" in p


from server.automations.autopilot_decider import _build_prompt
from server.automations.autopilot_policy import DecisionContext
from server.automations.completion_bar import PrFact, PrFacts


def test_build_prompt_includes_pr_facts():
    ctx = DecisionContext(
        session_id="s1", status="complete", account="a", repo="o/r", branch="feat/x",
        effective_goal="fix ci", goal_source="explicit", stuck={"kind": "complete"},
        transcript_tail=[], ledger={},
        pr_facts=PrFacts(has_repo=True, prs=(PrFact(7, "open", False, False, "u"),)))
    prompt = _build_prompt(ctx)
    assert "pr_facts" in prompt and "\"number\": 7" in prompt and "has_repo" in prompt

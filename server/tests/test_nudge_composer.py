"""Composer contract: injected query_fn only (the real model is never called),
None on decline/garbage/exception — never a canned string."""
import pytest
from server.automations.autopilot_policy import DecisionContext
from server.automations.nudge_composer import compose_nudge


def _ctx():
    return DecisionContext(
        session_id="s1", status="done", account="a", repo="o/r", branch="b",
        effective_goal="ship the parser fix", goal_source="inferred",
        stuck={"kind": "done"}, transcript_tail=[])


def _fake_query(reply: str):
    async def query_fn(*, prompt, options=None):
        class Block:
            text = reply
        class Msg:
            content = [Block()]
        yield Msg()
    return query_fn


@pytest.mark.asyncio
async def test_composes_nudge_from_json_reply():
    out = await compose_nudge(
        _ctx(), intent_hint="fix CI", verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('{"nudge": "Fix the 2 failing tests in test_ws.py, then rerun pytest."}'))
    assert out == "Fix the 2 failing tests in test_ws.py, then rerun pytest."


@pytest.mark.asyncio
async def test_escalate_reply_returns_none():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('{"escalate": true, "reason": "nothing concrete to say"}'))
    assert out is None


@pytest.mark.asyncio
async def test_prose_reply_with_embedded_json_still_parses():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query('Sure! {"nudge": "Rebase onto origin/master."}'))
    assert out == "Rebase onto origin/master."


@pytest.mark.asyncio
async def test_garbage_reply_returns_none_not_a_template():
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False,
        _query_fn=_fake_query("I think you should keep going."))
    assert out is None


@pytest.mark.asyncio
async def test_query_exception_returns_none():
    async def boom(*, prompt, options=None):
        raise RuntimeError("model down")
        yield  # pragma: no cover
    out = await compose_nudge(
        _ctx(), intent_hint=None, verdict_reason=None, verdict_remedy=None,
        recent_nudges=[], rich_tail=[], facts_verified=False, _query_fn=boom)
    assert out is None


@pytest.mark.asyncio
async def test_prompt_carries_context_and_history():
    seen = {}
    async def capture(*, prompt, options=None):
        seen["prompt"] = prompt
        class Block:
            text = '{"nudge": "ok"}'
        class Msg:
            content = [Block()]
        yield Msg()
    await compose_nudge(
        _ctx(), intent_hint="arm the PR", verdict_reason="PR #7 is open but not armed",
        verdict_remedy="gh pr merge 7 --squash --auto",
        recent_nudges=["previous nudge text"],
        rich_tail=[{"role": "user", "text": "please fix"}], facts_verified=True,
        _query_fn=capture)
    p = seen["prompt"]
    assert "arm the PR" in p and "PR #7 is open but not armed" in p
    assert "previous nudge text" in p and "please fix" in p
    assert '"facts_verified": true' in p

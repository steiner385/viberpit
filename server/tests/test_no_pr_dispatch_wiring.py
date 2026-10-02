"""A no-PR dispatch must reach every judge of the session, not just ``met_bar``.

#2480 taught ``completion_bar.met_bar`` to read "do not implement / open no PR" out of
the dispatch prompt, but the live callers read it via ``getattr(rec, "initial_prompt")``
on the in-memory ``SessionRecord`` — which had no such field, so the check was inert and
a backlog investigation (#2600, session a47573c5) was still nudged to "open a PR". The
inferred goal had the same blind spot: it told the model to project past investigation
to a shipped PR, contradicting the prompt the session was following.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.automations.backlog_prompt import build_investigate_prompt
from server.automations.completion_bar import PrFacts, has_no_pr_dispatch, met_bar
from server.sessions.engine import AgentEngine
from server.sessions.goal_inference import build_codex_goal_prompt, infer_goal
from server.sessions.types import SessionRecord

_NO_PR_PROMPT = ("These are auto-filed error reports. Investigate; do not implement. "
                 "Read-only on code: open no PR, push nothing.")


def _rec(**kw) -> SessionRecord:
    return SessionRecord(id="s1", name="backlog o/r#1", account="a",
                         engine=AgentEngine.CLAUDE_CODE, engine_session_id="c",
                         cols=80, rows=24, cwd="/tmp", **kw)


def test_record_carries_initial_prompt():
    assert _rec().initial_prompt is None
    assert _rec(initial_prompt="x").initial_prompt == "x"


def test_record_from_row_keeps_initial_prompt():
    from server.sessions.orchestrator import SessionOrchestrator
    row = SimpleNamespace(
        id="s1", name="n", account_name="a", engine_session_id="c", cwd="/tmp",
        repo="o/r", status="idle", name_is_auto=False, permission_mode="interactive",
        created_at=None, initial_prompt=_NO_PR_PROMPT)
    orch = SessionOrchestrator(sdk_factory=None, poll_interval=1, usage_fn=None,
                               repo_root="/tmp")
    assert orch._record_from_row(row).initial_prompt == _NO_PR_PROMPT


def test_real_investigate_prompt_satisfies_the_bar_with_a_code_goal():
    """Prompt, goal and bar agree: the prompt the runner really sends, plus the
    code-flavoured goal the inferrer produced, is a met investigation."""
    from types import SimpleNamespace as NS
    batch = NS(repo="o/r", issues=["o/r#1"], primary="o/r#1", titles={}, decisions=None)
    prompt = build_investigate_prompt(batch, engine=AgentEngine.CLAUDE_CODE)
    assert has_no_pr_dispatch(prompt)
    v = met_bar("Implement quota logging fix and open PR with auto-merge armed",
                "inferred", PrFacts(has_repo=True), "not-needed",
                pr_resolution_source="tool", instructions=prompt)
    assert v.met and v.confidence == "high"


def test_codex_goal_prompt_states_no_pr_scope():
    plain = build_codex_goal_prompt("n", [])
    scoped = build_codex_goal_prompt("n", [], no_pr_dispatch=True)
    assert "forbid" not in plain.lower()
    assert "forbid" in scoped.lower() and "no pull request" in scoped.lower()


@pytest.mark.asyncio
async def test_infer_goal_tells_the_model_the_dispatch_forbids_a_pr():
    seen: list[str] = []

    async def fn(*, prompt, options):
        seen.append(prompt)
        return
        yield  # pragma: no cover

    await infer_goal("n", [], config_dir="", cwd="/tmp", query_fn=fn,
                     no_pr_dispatch=True)
    assert "forbid" in seen[0].lower()

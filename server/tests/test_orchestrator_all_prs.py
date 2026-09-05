"""Tests for PipelinePr dataclass and SessionOrchestrator.all_prs()."""
from unittest import mock

import pytest

from server.github.pr_status import PipelinePr, PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)


def _make_record(orch, *, session_id, name, repo, authored_branches):
    rec = SessionRecord(
        id=session_id, name=name, account="a", engine=AgentEngine.CLAUDE_CODE, engine_session_id="", cols=80, rows=24,
        cwd="/tmp", repo=repo, authored_branches=authored_branches,
    )
    orch._records[session_id] = rec
    return rec


def test_pipeline_pr_round_trip_with_and_without_session():
    pr = PullRequestInfo(number=1, url="u", state="open", checks="none", title="t", repo="o/n")
    linked = PipelinePr(pr=pr, session_id="s1", session_name="Fix it")
    d = linked.to_dict()
    assert d["number"] == 1 and d["repo"] == "o/n"
    assert d["session"] == {"id": "s1", "name": "Fix it"}
    back = PipelinePr.from_dict(d)
    assert back.session_id == "s1" and back.session_name == "Fix it" and back.pr.repo == "o/n"

    unlinked = PipelinePr(pr=pr)
    assert unlinked.to_dict()["session"] is None
    assert PipelinePr.from_dict(unlinked.to_dict()).session_id is None


@pytest.mark.asyncio
async def test_all_prs_joins_population_to_sessions():
    """all_prs() attaches a session to population PRs that a session authored,
    and leaves unsessioned PRs with session_id=None. Stage order: parked first."""
    orch = _orch()
    # Population cache: repo o/n has PR #1 (parked) with a session, #2 (ready) without.
    orch._pr_population._cache["o/n"] = (
        PullRequestInfo(number=1, url="u1", state="open", checks="failing",
                        title="broken", repo="o/n", stage="parked"),
        PullRequestInfo(number=2, url="u2", state="open", checks="passing",
                        title="fine", repo="o/n", stage="ready"),
    )
    # A session that authored PR #1 (its session-branch poller knows it).
    orch._pr_poller._cache[("o/n", "feat/x")] = PullRequestInfo(
        number=1, url="u1", state="open", checks="failing", title="broken")
    _make_record(orch, session_id="s1", name="Fix it", repo="o/n",
                 authored_branches=["feat/x"])

    result = await orch.all_prs()
    by_num = {p.pr.number: p for p in result}
    assert by_num[1].session_id == "s1" and by_num[1].session_name == "Fix it"
    assert by_num[2].session_id is None
    # parked (urgency 0) sorts before ready
    assert [p.pr.number for p in result][0] == 1

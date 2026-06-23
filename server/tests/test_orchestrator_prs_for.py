import pytest
from unittest import mock

from server.github.pr_status import PullRequestInfo
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord


def _orch():
    return SessionOrchestrator(sdk_factory=mock.MagicMock(), poll_interval=9999.0,
                               usage_fn=lambda name: None)


def _pr(n, state="open"):
    return PullRequestInfo(n, f"https://github.com/o/r/pull/{n}", state, "passing", f"PR {n}")


def test_prs_for_gathers_dedups_and_orders():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b", "feat/c"],
    )
    cache = {
        ("o/r", "feat/a"): _pr(2, "merged"),
        ("o/r", "feat/b"): _pr(1, "open"),
        ("o/r", "feat/c"): _pr(1, "open"),   # duplicate PR #1 via another branch
    }
    orch._pr_poller.cached = lambda repo, br: cache.get((repo, br))
    out = orch.prs_for("s1")
    assert [p.number for p in out] == [1, 2]  # open before merged; #1 deduped


def test_prs_for_empty_when_no_repo_or_cache():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo=None, authored_branches=["feat/a"],
    )
    assert orch.prs_for("s1") == []


def test_pr_pairs_unions_authored_branches():
    orch = _orch()
    orch._records["s1"] = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp", repo="o/r", authored_branches=["feat/a", "feat/b"],
    )
    assert set(orch._pr_pairs()) == {("o/r", "feat/a"), ("o/r", "feat/b")}


@pytest.mark.asyncio
async def test_refresh_authored_branches_unions_and_keeps_existing(monkeypatch):
    orch = _orch()
    rec = SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/master-s1",
        authored_branches=["feat/old"],
    )
    orch._records["s1"] = rec
    monkeypatch.setattr(
        "server.sessions.orchestrator.worktrees.authored_branches",
        lambda cwd, scratch: ["feat/new"],
    )
    # No DB write should be required to update the in-memory set; stub db_session
    # so the persistence branch is exercised without a real engine.
    import contextlib

    class _FakeDb:
        async def execute(self, *a, **k):
            return None

    @contextlib.asynccontextmanager
    async def _fake_session():
        yield _FakeDb()

    monkeypatch.setattr("server.sessions.orchestrator.db_session", _fake_session)
    await orch.refresh_authored_branches()
    assert rec.authored_branches == ["feat/new", "feat/old"]  # union, sorted

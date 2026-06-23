import pytest

from server.api.sessions import _record_to_dict, _resolve_pr
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


class _SyncOrch:
    """In-process orchestrator: pr_for is synchronous."""
    def pr_for(self, repo, branch):
        return PullRequestInfo(1, "u", "open", "none", "t") if branch else None


class _AsyncOrch:
    """Remote orchestrator: pr_for awaits an RPC."""
    async def pr_for(self, repo, branch):
        return PullRequestInfo(2, "u", "merged", "passing", "t") if branch else None


class _NoPrOrch:
    """An orchestrator with no pr_for at all (must not raise)."""


@pytest.mark.asyncio
async def test_resolve_pr_handles_sync_async_and_missing():
    rec = _rec()
    assert (await _resolve_pr(_SyncOrch(), rec)).number == 1   # in-process: sync
    assert (await _resolve_pr(_AsyncOrch(), rec)).number == 2  # remote: awaited
    assert await _resolve_pr(_NoPrOrch(), rec) is None         # absent: graceful


def test_record_to_dict_includes_pr_when_given():
    pr = PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")
    d = _record_to_dict(_rec(), repo="o/r", pr=pr)
    assert d["pr"] == {
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t",
        "deployed": False,
    }


def test_record_to_dict_pr_null_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["pr"] is None

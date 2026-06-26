import pytest

from server.api.sessions import _record_to_dict, _resolve_prs
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec():
    return SessionRecord(
        id="s1", name="n", account="a", claude_session_id="", cols=80, rows=24,
        cwd="/tmp/wt", repo="o/r", branch="ccswitch/main-abcd",
    )


def test_record_to_dict_includes_prs_when_given():
    prs = [PullRequestInfo(9, "https://github.com/o/r/pull/9", "open", "passing", "t")]
    d = _record_to_dict(_rec(), repo="o/r", prs=prs)
    assert d["prs"] == [{
        "number": 9, "url": "https://github.com/o/r/pull/9",
        "state": "open", "checks": "passing", "title": "t", "deployed": False,
        "head_sha": "", "is_draft": False, "merged": False, "updated_at": "",
        "checks_detail": [], "stage": "", "repo": "",
    }]
    assert "pr" not in d


def test_record_to_dict_prs_empty_by_default():
    assert _record_to_dict(_rec(), repo="o/r")["prs"] == []


class _SyncOrch:
    def prs_for(self, sid):
        return [PullRequestInfo(1, "u", "open", "none", "t")]


class _AsyncOrch:
    async def prs_for(self, sid):
        return [PullRequestInfo(2, "u", "merged", "passing", "t")]


class _NoOrch:
    pass


@pytest.mark.asyncio
async def test_resolve_prs_handles_sync_async_and_missing():
    rec = _rec()
    assert [p.number for p in await _resolve_prs(_SyncOrch(), rec)] == [1]
    assert [p.number for p in await _resolve_prs(_AsyncOrch(), rec)] == [2]
    assert await _resolve_prs(_NoOrch(), rec) == []

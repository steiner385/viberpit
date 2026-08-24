"""The page makes three enrichment RPCs, not three per session (#742).

The count assertion is the whole point of the change; everything else here guards the
shape of what those three calls return.
"""
import pytest

from server.api.sessions import _enrich_batch
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


def _rec(sid):
    return SessionRecord(id=sid, name=sid, account="a", claude_session_id="",
                         cols=80, rows=24, cwd="/tmp/wt", repo="o/r", branch="b")


class _BatchOrch:
    def __init__(self):
        self.calls = []

    async def prs_for_many(self, session_ids, *, timeout=None):
        self.calls.append("prs_for_many")
        return {s: [PullRequestInfo(1, "u", "open", "none", f"t-{s}")] for s in session_ids}

    async def latest_suggestions_many(self, session_ids, *, timeout=None):
        self.calls.append("latest_suggestions_many")
        return {s: [f"s-{s}"] for s in session_ids}

    async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
        self.calls.append("latest_summary_many")
        return {s: f"sum-{s}" for s in session_ids}


@pytest.mark.asyncio
async def test_ten_sessions_cost_three_rpcs_not_thirty():
    orch = _BatchOrch()
    records = [_rec(f"s{i}") for i in range(10)]
    await _enrich_batch(orch, records, viewing=False, timeout=3.0)
    assert len(orch.calls) == 3, f"expected 3 batched RPCs, got {len(orch.calls)}"
    assert sorted(orch.calls) == ["latest_suggestions_many", "latest_summary_many",
                                  "prs_for_many"]


@pytest.mark.asyncio
async def test_results_align_positionally_with_records():
    orch = _BatchOrch()
    records = [_rec("a"), _rec("b")]
    _repos, suggs, prs, summaries = await _enrich_batch(
        orch, records, viewing=False, timeout=3.0)
    assert list(suggs) == [["s-a"], ["s-b"]]
    assert [p[0].title for p in prs] == ["t-a", "t-b"]
    assert list(summaries) == ["sum-a", "sum-b"]


@pytest.mark.asyncio
async def test_a_session_missing_from_the_reply_degrades_to_empty():
    class _Partial(_BatchOrch):
        async def latest_summary_many(self, session_ids, *, viewing=False, timeout=None):
            self.calls.append("latest_summary_many")
            return {}  # daemon knows none of these ids

    _repos, _suggs, _prs, summaries = await _enrich_batch(
        _Partial(), [_rec("a")], viewing=False, timeout=3.0)
    assert list(summaries) == [""]


@pytest.mark.asyncio
async def test_a_failing_batch_rpc_degrades_that_field_only():
    class _Broken(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise RuntimeError("daemon down")

    _repos, suggs, prs, _summaries = await _enrich_batch(
        _Broken(), [_rec("a")], viewing=False, timeout=3.0)
    assert list(suggs) == [[]]
    assert [p[0].title for p in prs] == ["t-a"]  # the other two still landed


@pytest.mark.asyncio
async def test_unknown_method_falls_back_to_per_session_calls():
    """An old daemon 404s the batched method; the page must still enrich (spec D3)."""
    from server.ipc.client import DaemonError

    class _OldDaemon(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise DaemonError("UnknownMethod", "latest_suggestions_many")

        async def latest_suggestions(self, session_id, *, timeout=None):
            self.calls.append("latest_suggestions")
            return [f"s-{session_id}"]

    orch = _OldDaemon()
    _repos, suggs, _prs, _summaries = await _enrich_batch(
        orch, [_rec("a"), _rec("b")], viewing=False, timeout=3.0)
    assert list(suggs) == [["s-a"], ["s-b"]]
    assert orch.calls.count("latest_suggestions") == 2


@pytest.mark.asyncio
async def test_a_normal_daemon_error_does_not_trigger_fallback():
    """Only UnknownMethod means 'old daemon'. Any other error degrades to empty —
    retrying every failure per session would restore the N+1 under load."""
    from server.ipc.client import DaemonError

    class _Erroring(_BatchOrch):
        async def latest_suggestions_many(self, session_ids, *, timeout=None):
            self.calls.append("latest_suggestions_many")
            raise DaemonError("InternalError", "boom")

        async def latest_suggestions(self, session_id, *, timeout=None):
            self.calls.append("latest_suggestions")
            return ["should not be reached"]

    orch = _Erroring()
    _repos, suggs, _prs, _summaries = await _enrich_batch(
        orch, [_rec("a")], viewing=False, timeout=3.0)
    assert list(suggs) == [[]]
    assert "latest_suggestions" not in orch.calls

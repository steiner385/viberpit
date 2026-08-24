"""Batched enrichment: one call per field for the whole page, with per-session isolation.

The isolation tests are the point. A batch that fails whole-hog on one bad session would
turn a single wedged session into a blank page — strictly worse than the per-session calls
it replaces (issue #742, spec 2026-08-23-batched-enrichment-rpcs-design.md).
"""
import pytest

from server.github.pr_status import PullRequestInfo


class _Orch:
    """Minimal stand-in exposing only what the *_many methods call."""

    def __init__(self, *, bad: set[str] | None = None):
        self._bad = bad or set()

    def prs_for(self, session_id):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return [PullRequestInfo(1, "u", "open", "none", f"t-{session_id}")]

    async def latest_suggestions(self, session_id):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return [f"s-{session_id}"]

    async def latest_summary(self, session_id, *, viewing):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return f"sum-{session_id}-{viewing}"


def _bind(orch):
    """Bind the real *_many implementations onto the stand-in."""
    from server.sessions.orchestrator import SessionOrchestrator
    for name in ("prs_for_many", "latest_suggestions_many", "latest_summary_many"):
        setattr(type(orch), name, getattr(SessionOrchestrator, name))
    return orch


def test_prs_for_many_returns_one_entry_per_session():
    orch = _bind(_Orch())
    out = orch.prs_for_many(["a", "b"])
    assert set(out) == {"a", "b"}
    assert out["a"][0].title == "t-a"


def test_prs_for_many_isolates_a_failing_session():
    orch = _bind(_Orch(bad={"b"}))
    out = orch.prs_for_many(["a", "b"])
    assert out["a"][0].title == "t-a"
    assert out["b"] == []


@pytest.mark.asyncio
async def test_latest_suggestions_many_isolates_a_failing_session():
    orch = _bind(_Orch(bad={"b"}))
    out = await orch.latest_suggestions_many(["a", "b"])
    assert out["a"] == ["s-a"]
    assert out["b"] == []


@pytest.mark.asyncio
async def test_latest_summary_many_forwards_viewing_and_isolates():
    orch = _bind(_Orch(bad={"b"}))
    out = await orch.latest_summary_many(["a", "b"], viewing=True)
    assert out["a"] == "sum-a-True"
    assert out["b"] == ""


@pytest.mark.asyncio
async def test_empty_input_makes_no_calls():
    orch = _bind(_Orch())
    assert orch.prs_for_many([]) == {}
    assert await orch.latest_suggestions_many([]) == {}
    assert await orch.latest_summary_many([], viewing=False) == {}

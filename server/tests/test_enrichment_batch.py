"""Batched enrichment: one call per field for the whole page, with per-session isolation.

The isolation tests are the point. A batch that fails whole-hog on one bad session would
turn a single wedged session into a blank page — strictly worse than the per-session calls
it replaces (issue #742, spec 2026-08-23-batched-enrichment-rpcs-design.md).
"""
import pytest

from server.github.pr_status import PullRequestInfo


class _Orch:
    """Minimal stand-in exposing only what the *_many methods call."""

    def __init__(self, *, bad: set[str] | None = None, records=None):
        self._bad = bad or set()
        self._records = records or {}

    def prs_for(self, session_id):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return [PullRequestInfo(1, "u", "open", "none", f"t-{session_id}")]

    async def latest_suggestions(self, session_id, *, copilot_accounts=None):
        if session_id in self._bad:
            raise RuntimeError("boom")
        rec = self._records.get(session_id)
        if rec is not None and await self._is_copilot_account(rec.account, copilot_accounts):
            return []
        return [f"s-{session_id}"]

    async def latest_summary(self, session_id, *, viewing, copilot_accounts=None):
        if session_id in self._bad:
            raise RuntimeError("boom")
        return f"sum-{session_id}-{viewing}"


def _bind(orch):
    """Bind the real *_many implementations onto the stand-in."""
    from server.sessions.orchestrator import SessionOrchestrator
    for name in ("prs_for_many", "latest_suggestions_many", "latest_summary_many",
                 "_copilot_accounts_of", "_is_copilot_account"):
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


@pytest.mark.asyncio
async def test_many_rpcs_resolve_each_account_once_off_the_event_loop(monkeypatch):
    """#2848: each provider lookup reconciles the manifest against the account store
    (sshfs on runners). Resolved per record on the event loop, every poll made N
    blocking mount round-trips and stalled the daemon. One worker-thread lookup per
    DISTINCT account per batch instead."""
    import threading
    from types import SimpleNamespace

    from server.sessions import orchestrator as orch_mod

    loop_thread = threading.get_ident()
    calls: list[tuple[str, int]] = []

    def provider(acct):
        calls.append((acct, threading.get_ident()))
        return "copilot" if acct == "cp" else "anthropic"

    monkeypatch.setattr(orch_mod, "_provider_of", provider)
    records = {f"s{i}": SimpleNamespace(account="work" if i % 2 else "cp") for i in range(10)}
    orch = _bind(_Orch(records=records))

    out = await orch.latest_suggestions_many(list(records))
    assert sorted(a for a, _ in calls) == ["cp", "work"]
    assert all(t != loop_thread for _, t in calls), "provider lookups must run off the loop"
    assert out["s0"] == [] and out["s1"] == ["s-s1"]

    calls.clear()
    await orch.latest_summary_many(list(records), viewing=False)
    assert sorted(a for a, _ in calls) == ["cp", "work"]

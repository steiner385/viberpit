# server/tests/test_orchestrator_pr_push.py
"""Tests for M2b.2: debounced push alert when a session's PR transitions into failing CI."""
from __future__ import annotations

import pytest
from server.sessions.types import SessionRecord


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._pushed_pr = set()
    return orch


def _add_record(
    orch,
    session_id: str,
    name: str = "test-session",
    repo: str | None = "o/r",
    branch: str | None = "feat/x",
) -> None:
    orch._records[session_id] = SessionRecord(
        id=session_id,
        name=name,
        account="acct-a",
        claude_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
    )


@pytest.mark.asyncio
async def test_pr_push_fires_once_on_transition_into_failing(monkeypatch):
    """passing→failing fires push once; a second identical call does NOT re-fire."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body, url))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", repo="o/r", branch="feat/x")

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(calls) == 1
    assert "my-session" in calls[0][1]
    assert "o/r" in calls[0][1]
    assert calls[0][2] == "/session/s1"

    # Debounce: second call with same condition must NOT re-fire
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_pr_push_re_fires_after_recovery(monkeypatch):
    """failing→passing clears debounce; a later failing fires again."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    _add_record(orch, "s1", name="my-session", repo="o/r", branch="feat/x")

    # First entry fires
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(calls) == 1

    # Recovery clears debounce; no push on non-failing
    await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
    assert len(calls) == 1

    # Re-break fires again
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_pr_push_no_match_no_crash(monkeypatch):
    """A (repo, branch) with no matching session → no push, no crash."""
    import server.sessions.orchestrator as orch_mod
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    orch = _make_orch()
    # No records registered

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(calls) == 0


@pytest.mark.asyncio
async def test_pr_poller_set_on_change_wired_in_init():
    """After __init__ the _pr_poller has _on_change set to orch._on_pr_change."""
    from server.sessions.orchestrator import SessionOrchestrator
    from unittest.mock import MagicMock, patch

    fake_sdk = MagicMock()
    # PrStatusPoller is imported locally inside __init__ (from server.github.pr_poller),
    # so patch it at its source module — the local import picks up the patch at call time.
    with patch("server.sessions.orchestrator.real_sdk_factory", fake_sdk), \
         patch("server.sessions.orchestrator.SuggestionService"), \
         patch("server.sessions.orchestrator.UsagePoller"), \
         patch("server.github.pr_poller.PrStatusPoller") as MockPoller:
        instance = MockPoller.return_value
        orch = SessionOrchestrator(sdk_factory=fake_sdk)
        # Verify set_on_change was called with orch._on_pr_change
        instance.set_on_change.assert_called_once_with(orch._on_pr_change)

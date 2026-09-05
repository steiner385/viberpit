# server/tests/test_orchestrator_pr_push.py
"""Tests for M2b.2: debounced push alert when a session's PR transitions into failing CI."""
from __future__ import annotations

import pytest
from server.github.pr_status import PullRequestInfo
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


class _StubPoller:
    """Returns a fixed failing PR for the (repo, branch) the push tests use, so
    the durable loop-guard can resolve a target (``repo#number``). The mode is
    'propose' in these tests, so the auto path is never taken regardless."""

    def cached(self, repo: str, branch: str):
        return PullRequestInfo(
            number=7, url="https://x/pull/7", state="open", checks="failing", title="t")


async def _init_pr_doctor_db(tmp_path) -> None:
    from server.db import init_db
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    await registry.ensure_rows()


def _make_orch():
    from server.sessions.orchestrator import SessionOrchestrator
    from server.automations.context import AutomationContext
    orch = SessionOrchestrator.__new__(SessionOrchestrator)
    orch._records = {}
    orch._pushed_pr = set()
    orch._pr_doctor_ctx = AutomationContext("pr-doctor")
    orch._pr_poller = _StubPoller()  # type: ignore[assignment]
    orch._drivers = {}
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
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
    )


@pytest.mark.asyncio
async def test_pr_push_fires_once_on_transition_into_failing(monkeypatch, tmp_path):
    """passing→failing fires push once; a second identical call does NOT re-fire."""
    import server.sessions.orchestrator as orch_mod
    await _init_pr_doctor_db(tmp_path)
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body, url))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    # Mode "propose" skips the auto path so the notify-only path is exercised.
    async def _mode_off(self):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_off, raising=True)

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
async def test_pr_push_re_fires_after_recovery(monkeypatch, tmp_path):
    """failing→passing clears debounce; a later failing fires again."""
    import server.sessions.orchestrator as orch_mod
    await _init_pr_doctor_db(tmp_path)
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    # Mode "propose" skips the auto path so the notify-only path is exercised.
    async def _mode_off(self):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_off, raising=True)

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
async def test_pr_push_no_match_no_crash(monkeypatch, tmp_path):
    """A (repo, branch) with no matching session → no push, no crash."""
    import server.sessions.orchestrator as orch_mod
    await _init_pr_doctor_db(tmp_path)
    calls = []

    async def fake_notify(title, body, *, url=None):
        calls.append((title, body))

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    # Mode "propose" skips the auto path so the notify-only path is exercised.
    async def _mode_off(self):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_off, raising=True)

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

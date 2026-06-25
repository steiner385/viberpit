# server/tests/test_pr_autofix.py
"""Tests for Task 3: request_pr_fix, auto-mode loop guard, _on_pr_change wiring."""
from __future__ import annotations

import pytest
from server.github.pr_status import PullRequestInfo
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr(number=7, state="open", checks="failing"):
    return PullRequestInfo(
        number=number,
        url=f"https://x/pull/{number}",
        state=state,
        checks=checks,
        title="t",
    )


def _make_record(
    session_id="sess1",
    name="test-session",
    repo="o/r",
    branch="feat/x",
    authored_branches=None,
):
    return SessionRecord(
        id=session_id,
        name=name,
        account="acct-a",
        claude_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
        authored_branches=authored_branches or [branch],
    )


class _StubPoller:
    """Minimal stub for _pr_poller: cached() returns a fixed PR for known (repo, branch)."""

    def __init__(self, pr: PullRequestInfo, repo: str = "o/r", branch: str = "feat/x"):
        self._pr = pr
        self._repo = repo
        self._branch = branch

    def cached(self, repo: str, branch: str):
        if repo == self._repo and branch == self._branch:
            return self._pr
        return None


def _build_orch(pr: PullRequestInfo, *, has_driver: bool = True):
    """Build a minimal SessionOrchestrator with only the attributes Task-3 touches."""
    import server.sessions.orchestrator as orch_mod

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = {"sess1": _make_record()}
    orch._pushed_pr = set()
    orch._pr_fix_attempts = set()
    orch._pr_poller = _StubPoller(pr)  # type: ignore[assignment]  # stub stands in for PrStatusPoller
    # Sentinel driver object — only presence in the dict matters.
    orch._drivers = {"sess1": object()} if has_driver else {}  # type: ignore[dict-item]
    return orch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def orch_with_failing_pr(monkeypatch):
    """Orch wired with a live failing PR and a live driver; send_prompt recorded."""
    import server.sessions.orchestrator as orch_mod

    pr = _pr(state="open", checks="failing")
    orch = _build_orch(pr, has_driver=True)
    sent: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    # Patch push_mod.notify to avoid network
    async def fake_notify(title, body, *, url=None):
        pass

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    # Default mode: monkeypatch _pr_autofix_mode so no DB touch is needed.
    # Individual tests can override this per-test.
    async def _mode_propose(self_):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_propose, raising=True)

    return orch, sent


@pytest.fixture
def orch_with_pr(monkeypatch):
    """Factory fixture: returns a callable that takes a PullRequestInfo and builds the orch."""
    import server.sessions.orchestrator as orch_mod

    def _make(pr: PullRequestInfo):
        orch = _build_orch(pr, has_driver=True)
        sent: list[str] = []

        async def fake_send(session_id, text):
            sent.append(text)
            return "sent"

        orch.send_prompt = fake_send  # type: ignore[method-assign]
        return orch, sent

    # Patch push_mod.notify to avoid network
    async def fake_notify(title, body, *, url=None):
        pass

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)
    return _make


@pytest.fixture
def orch_with_failing_pr_no_driver(monkeypatch):
    """Orch with failing PR but NO live driver; push notifications recorded."""
    import server.sessions.orchestrator as orch_mod

    pr = _pr(state="open", checks="failing")
    orch = _build_orch(pr, has_driver=False)
    sent: list[str] = []
    pushed: list[str] = []

    async def fake_send(session_id, text):
        sent.append(text)
        return "sent"

    orch.send_prompt = fake_send  # type: ignore[method-assign]

    async def fake_notify(title, body, *, url=None):
        pushed.append(title)

    monkeypatch.setattr(orch_mod.push_mod, "notify", fake_notify)

    # Default mode: propose (tests override to "auto" as needed).
    async def _mode_propose(self_):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_propose, raising=True)

    return orch, sent, pushed


# ---------------------------------------------------------------------------
# request_pr_fix tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_pr_fix_injects_for_actionable_failing(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr
    result = await orch.request_pr_fix("sess1", 7)
    assert result == "sent"
    assert len(sent) == 1
    assert "gh pr checkout 7" in sent[0]


@pytest.mark.asyncio
async def test_request_pr_fix_skips_terminal_pr(orch_with_pr):
    orch, sent = orch_with_pr(_pr(state="merged", checks="failing"))
    assert await orch.request_pr_fix("sess1", 7) == "not_actionable"
    assert sent == []


@pytest.mark.asyncio
async def test_request_pr_fix_no_pr_number_match(orch_with_pr):
    orch, sent = orch_with_pr(_pr(number=7))
    assert await orch.request_pr_fix("sess1", 999) == "no_pr"
    assert sent == []


@pytest.mark.asyncio
async def test_request_pr_fix_unknown_session(orch_with_failing_pr):
    orch, sent = orch_with_failing_pr
    result = await orch.request_pr_fix("no_such_session", 7)
    assert result == "no_session"
    assert sent == []


# ---------------------------------------------------------------------------
# _on_pr_change / auto-mode loop-guard tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_mode_injects_once_per_failure_event(orch_with_failing_pr, monkeypatch):
    import server.sessions.orchestrator as orch_mod

    orch, sent = orch_with_failing_pr

    # Switch mode to "auto"
    async def _mode_auto(self_):
        return "auto"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_auto, raising=True)

    # First failure → one auto-attempt.
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 1

    # Second failing signal without recovery → NO second attempt (guard holds).
    await orch._on_pr_change("o/r", "feat/x", "failing", "failing")
    assert len(sent) == 1

    # Recovery clears the guard; a fresh break re-attempts.
    await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_auto_mode_pending_interloper_does_not_re_arm(orch_with_failing_pr, monkeypatch):
    """A transient failing→pending→failing CI cycle (the real path while a pushed
    fix re-runs) must NOT re-arm the guard — only a genuine ``passing`` does.
    Otherwise a PR the agent can't fix loops on every CI run."""
    import server.sessions.orchestrator as orch_mod

    orch, sent = orch_with_failing_pr

    async def _mode_auto(self_):
        return "auto"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_auto, raising=True)

    # First failure → one auto-attempt.
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 1

    # Pushed fix re-runs CI: failing → pending → failing. The ``pending`` hop
    # must NOT clear the guard, so the second ``failing`` does NOT re-attempt.
    await orch._on_pr_change("o/r", "feat/x", "failing", "pending")
    await orch._on_pr_change("o/r", "feat/x", "pending", "failing")
    assert len(sent) == 1

    # Only a real green re-arms.
    await orch._on_pr_change("o/r", "feat/x", "failing", "passing")
    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_off_mode_never_injects(orch_with_failing_pr, monkeypatch):
    import server.sessions.orchestrator as orch_mod

    orch, sent = orch_with_failing_pr

    async def _mode_off(self_):
        return "off"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_off, raising=True)

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []


@pytest.mark.asyncio
async def test_propose_mode_never_auto_injects(orch_with_failing_pr, monkeypatch):
    import server.sessions.orchestrator as orch_mod

    orch, sent = orch_with_failing_pr

    async def _mode_propose(self_):
        return "propose"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_propose, raising=True)

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []  # propose surfaces an action; it does not auto-inject


@pytest.mark.asyncio
async def test_auto_mode_no_live_driver_falls_back(orch_with_failing_pr_no_driver, monkeypatch):
    import server.sessions.orchestrator as orch_mod

    orch, sent, pushed = orch_with_failing_pr_no_driver

    async def _mode_auto(self_):
        return "auto"

    monkeypatch.setattr(orch_mod.SessionOrchestrator, "_pr_autofix_mode", _mode_auto, raising=True)

    await orch._on_pr_change("o/r", "feat/x", "passing", "failing")
    assert sent == []  # no injection without a live driver
    assert any("PR broke" in t for t in pushed)  # fell back to the notification

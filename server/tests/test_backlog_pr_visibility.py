"""Backlog runner PR visibility — the sweep must find the PR a session authored.

The branch is unknowable at spawn time (a fresh session has no branch yet), so
the spawn-time snapshot in the target state is always null. The sweep must
resolve branches from the SESSION row (live record or DB) at lookup time, and —
once the session has ended, the moment an attempt would be charged — fall back
to asking GitHub which PRs cross-reference the issue. Without this the runner
is blind to its own PRs: 66 spawns / 74 give-ups / 0 handoffs ever, while the
PRs actually merged (KinDash#11467→PR 11470 etc.).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from server.automations.context import AutomationContext
from server.db import db_session, init_db
from server.github.pr_status import PullRequestInfo


def _info(number: int, state: str = "open", branch: str = "") -> PullRequestInfo:
    return PullRequestInfo(number=number, url=f"https://x/pr/{number}", state=state,
                           checks="none", title=f"pr {number}", head_ref=branch)


class StubPoller:
    def __init__(self, cache: dict | None = None) -> None:
        self._cache = cache or {}

    def cached(self, repo, branch):
        if not repo or not branch:
            return None
        return self._cache.get((repo, branch))


def _orch(poller: StubPoller, records: dict | None = None):
    import server.sessions.orchestrator as orch_mod
    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._backlog_ctx = AutomationContext("backlog-runner")
    orch._pr_poller = poller  # type: ignore[assignment]
    orch._records = records or {}
    orch._gh_run = lambda args, token=None: None  # gh must not be hit unless faked
    return orch


async def _init(tmp_path, name: str) -> None:
    from server.automations.registry import registry
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    await registry.ensure_rows()


async def _seed_session(sid: str, *, status: str = "working",
                        branch: str | None = None,
                        authored: list[str] | None = None) -> None:
    from server.db.models import Session
    async with db_session() as db:
        db.add(Session(id=sid, name=f"backlog {sid}", account_name="t",
                       cwd="/tmp", status=status, branch=branch,
                       authored_branches=authored))


def test_resolves_branch_from_db_session_row(tmp_path):
    """Target-state branch is null (spawn-time snapshot) but the DB session row
    recorded authored_branches — the sweep must find the cached PR through it."""
    async def run():
        await _init(tmp_path, "a.db")
        await _seed_session("s1", authored=["feat/x"])
        want = _info(99, "merged", "feat/x")
        orch = _orch(StubPoller({("o/r", "feat/x"): want}))
        await orch._backlog_ctx.set_target_state(
            "o/r#5", {"session_id": "s1", "branch": None, "claimed": True})

        assert await orch._backlog_pr_status_for("o/r#5") is want

    asyncio.run(run())


def test_resolves_branch_from_live_record(tmp_path):
    """A live in-memory record's branch/authored_branches are consulted too."""
    async def run():
        await _init(tmp_path, "b.db")
        want = _info(7, "open", "feat/y")
        rec = SimpleNamespace(branch=None, authored_branches=["feat/y"])
        orch = _orch(StubPoller({("o/r", "feat/y"): want}), records={"s1": rec})
        await orch._backlog_ctx.set_target_state(
            "o/r#5", {"session_id": "s1", "claimed": True})

        assert await orch._backlog_pr_status_for("o/r#5") is want

    asyncio.run(run())


def test_gh_cross_ref_fallback_fires_only_after_session_ends(tmp_path, monkeypatch):
    """Once the session has ended (the moment the sweep would charge an attempt)
    and no cached branch matches, ask GitHub which PRs reference the issue."""
    async def run():
        await _init(tmp_path, "c.db")
        await _seed_session("s1", status="archived")
        want = _info(1234, "merged")
        calls: list = []

        def fake_find(ref, *, gh_run):
            calls.append(ref)
            return want

        import server.automations.backlog_github as bg
        monkeypatch.setattr(bg, "find_referencing_pr", fake_find)

        orch = _orch(StubPoller())
        await orch._backlog_ctx.set_target_state(
            "o/r#5", {"session_id": "s1", "claimed": True})

        assert await orch._backlog_pr_status_for("o/r#5") is want
        assert calls == ["o/r#5"]

    asyncio.run(run())


def test_no_gh_fallback_while_session_alive(tmp_path, monkeypatch):
    """A live session's target must NOT trigger a gh lookup every sweep tick."""
    async def run():
        await _init(tmp_path, "d.db")
        await _seed_session("s1", status="working")
        calls: list = []

        def fake_find(ref, *, gh_run):
            calls.append(ref)
            return _info(1, "merged")

        import server.automations.backlog_github as bg
        monkeypatch.setattr(bg, "find_referencing_pr", fake_find)

        orch = _orch(StubPoller())
        await orch._backlog_ctx.set_target_state(
            "o/r#5", {"session_id": "s1", "claimed": True})

        assert await orch._backlog_pr_status_for("o/r#5") is None
        assert calls == []

    asyncio.run(run())


def test_never_matches_unrelated_pr_by_issue_number(tmp_path):
    """Regression: the old fallback scanned the poller cache for a PR whose
    NUMBER equals the issue number — an unrelated PR that happens to share the
    number must never be returned."""
    async def run():
        await _init(tmp_path, "e.db")
        await _seed_session("s1", status="working")
        decoy = _info(5, "merged", "unrelated-branch")  # PR #5 == issue #5
        orch = _orch(StubPoller({("o/r", "unrelated-branch"): decoy}))
        await orch._backlog_ctx.set_target_state(
            "o/r#5", {"session_id": "s1", "claimed": True})

        assert await orch._backlog_pr_status_for("o/r#5") is None

    asyncio.run(run())

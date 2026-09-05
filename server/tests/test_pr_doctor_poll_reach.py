# server/tests/test_pr_doctor_poll_reach.py
"""PR Doctor poll-reach: archived-session PRs appear; terminal PRs are pruned.

Tests that `_pr_pairs()` in the orchestrator:
  1. Includes (repo, branch) pairs from ARCHIVED sessions in the DB (not in
     _records), so PR Doctor keeps polling their open PRs after the session ends.
  2. Still includes live-session branches (union preserved).
  3. Self-prunes branches whose cached PR is MERGED or CLOSED.
  4. Keeps branches with no cache (None) — not yet classified.
"""
from __future__ import annotations

import asyncio
import uuid

from server.github.pr_status import PullRequestInfo
from server.sessions.engine import AgentEngine
from server.sessions.types import SessionRecord


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pr(state: str = "open") -> PullRequestInfo:
    return PullRequestInfo(
        number=42,
        url="https://github.com/o/r/pull/42",
        state=state,
        checks="passing",
        title="feat: something",
        merge_state_status="CLEAN",
        mergeable="MERGEABLE",
    )


class _StubPoller:
    """Minimal PR poller stub: returns a configured PR for a specific pair."""

    def __init__(self, cache: dict[tuple[str, str], PullRequestInfo | None] | None = None):
        self._cache: dict[tuple[str, str], PullRequestInfo | None] = cache or {}

    def cached(self, repo: str, branch: str) -> PullRequestInfo | None:
        return self._cache.get((repo, branch), None)


def _make_record(
    session_id: str = "sess1",
    repo: str = "o/r",
    branch: str = "feat/live",
    authored_branches: list[str] | None = None,
) -> SessionRecord:
    return SessionRecord(
        id=session_id,
        name="live-session",
        account="acct-a",
        engine=AgentEngine.CLAUDE_CODE,
        engine_session_id="",
        cols=80,
        rows=24,
        cwd="/tmp",
        repo=repo,
        branch=branch,
        authored_branches=authored_branches or [branch],
    )


def _build_orch(
    *,
    records: dict | None = None,
    poller_cache: dict[tuple[str, str], PullRequestInfo | None] | None = None,
) -> object:
    """Build a minimal SessionOrchestrator via __new__ without real init."""
    import server.sessions.orchestrator as orch_mod

    orch = orch_mod.SessionOrchestrator.__new__(orch_mod.SessionOrchestrator)
    orch._records = records or {}
    orch._pr_poller = _StubPoller(poller_cache or {})  # type: ignore[assignment]

    # Stub _refresh_live_branches to a no-op (it needs real worktree state).
    async def _noop(self=None):
        pass

    orch._refresh_live_branches = _noop  # type: ignore[method-assign]
    return orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_archived_session_branch_included(tmp_path):
    """An ARCHIVED DB session (not in _records) contributes its authored_branches."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'reach.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        # Insert an archived/ended session with authored_branches.
        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="archived-session",
                account_name="acct-a",
                cwd="/tmp/old",
                status="ended",
                repo="o/r",
                authored_branches=["feat/x"],
            ))

        # Orchestrator has NO live records for this session.
        orch = _build_orch(records={}, poller_cache={})
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/x") in pairs

    asyncio.run(run())


def test_live_session_branches_still_included(tmp_path):
    """Live sessions in _records remain in the pair set (union preserved)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"

    async def run():
        from server.db import init_db
        await init_db(db_url=db_url)

        orch = _build_orch(
            records={"sess1": _make_record(repo="o/r", branch="feat/live",
                                           authored_branches=["feat/live"])},
            poller_cache={},
        )
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/live") in pairs

    asyncio.run(run())


def test_terminal_merged_pr_is_pruned(tmp_path):
    """A branch whose cached PR is MERGED is excluded (self-prune)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prune_merged.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        # Archived session with a merged branch.
        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="merged-session",
                account_name="acct-a",
                cwd="/tmp/merged",
                status="ended",
                repo="o/r",
                authored_branches=["feat/merged"],
            ))

        # Cache says this PR is MERGED.
        orch = _build_orch(
            records={},
            poller_cache={("o/r", "feat/merged"): _pr(state="merged")},
        )
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/merged") not in pairs

    asyncio.run(run())


def test_terminal_closed_pr_is_pruned(tmp_path):
    """A branch whose cached PR is CLOSED is excluded (self-prune)."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'prune_closed.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="closed-session",
                account_name="acct-a",
                cwd="/tmp/closed",
                status="ended",
                repo="o/r",
                authored_branches=["feat/closed"],
            ))

        orch = _build_orch(
            records={},
            poller_cache={("o/r", "feat/closed"): _pr(state="closed")},
        )
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/closed") not in pairs

    asyncio.run(run())


def test_uncached_branch_is_kept(tmp_path):
    """A branch with no cache entry (None) is kept — not yet classified."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'uncached.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="new-session",
                account_name="acct-a",
                cwd="/tmp/new",
                status="ended",
                repo="o/r",
                authored_branches=["feat/new"],
            ))

        # No cache entry for this branch.
        orch = _build_orch(records={}, poller_cache={})
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/new") in pairs

    asyncio.run(run())


def test_union_live_and_archived(tmp_path):
    """Live record branches and archived DB branches are both present."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'union.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        # Archived session in DB.
        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="archived",
                account_name="acct-a",
                cwd="/tmp/arch",
                status="ended",
                repo="o/r",
                authored_branches=["feat/archived"],
            ))

        # Live session in _records.
        orch = _build_orch(
            records={"sess1": _make_record(repo="o/r", branch="feat/live",
                                           authored_branches=["feat/live"])},
            poller_cache={},
        )
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/live") in pairs
        assert ("o/r", "feat/archived") in pairs

    asyncio.run(run())


def test_remote_prefixed_branches_are_excluded(tmp_path):
    """origin/* names (legacy reflog pollution in authored_branches) never reach
    the poller: a PR headRefName has no remote prefix, so such pairs only bloat
    the batched GraphQL query."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'remote_refs.db'}"

    async def run():
        from server.db import init_db, db_session
        from server.db.models import Session as DbSession
        await init_db(db_url=db_url)

        async with db_session() as db:
            db.add(DbSession(
                id=str(uuid.uuid4()),
                name="polluted",
                account_name="acct-a",
                cwd="/tmp/old",
                status="ended",
                repo="o/r",
                branch="origin/main",
                authored_branches=["origin/dependabot/npm_and_yarn/x", "feat/real"],
            ))

        orch = _build_orch(
            records={"sess1": _make_record(
                repo="o/r", branch="feat/live",
                authored_branches=["feat/live", "origin/main"])},
            poller_cache={},
        )
        pairs = await orch._pr_pairs()

        assert ("o/r", "feat/real") in pairs
        assert ("o/r", "feat/live") in pairs
        assert not any(br.startswith("origin/") for _, br in pairs)

    asyncio.run(run())


def test_db_failure_falls_back_to_live_only(tmp_path):
    """If the DB read raises, _pr_pairs still returns the live-session pairs."""
    # Do NOT init_db — so db_session() will fail.
    orch = _build_orch(
        records={"sess1": _make_record(repo="o/r", branch="feat/live",
                                       authored_branches=["feat/live"])},
        poller_cache={},
    )

    # No DB initialised — the DB query will throw; we just need no exception.
    pairs = asyncio.run(orch._pr_pairs())
    assert ("o/r", "feat/live") in pairs

"""Task 4 (M-Track-3): _pr_repos unions live in-memory repos with archived DB rows."""
from __future__ import annotations
import asyncio
import os
import pytest


class _FakeRecord:
    def __init__(self, id: str, name: str, repo: str | None) -> None:
        self.id, self.name, self.repo = id, name, repo


@pytest.fixture()
def orch_with_db(tmp_path):
    """Yield (orch, seed_session) over a temp SQLite DB.

    seed_session(repo, status) inserts a DbSession row.
    """
    db_path = tmp_path / "ccswitch.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"

    async def _setup():
        from server.db import init_db
        await init_db(db_url=db_url)

    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(
        os.environ, {"CCSWITCH_DB_URL": db_url}
    ):
        asyncio.run(_setup())

        from server.sessions.orchestrator import SessionOrchestrator
        orch = SessionOrchestrator(sdk_factory=lambda *a, **k: None, poll_interval=9999.0)

        _counter = [0]

        def seed_session(*, repo: str, status: str) -> None:
            _counter[0] += 1
            sid = f"seed-{_counter[0]}"

            async def _insert():
                from server.db import db_session
                from server.db.models import Session as DbSession
                async with db_session() as db:
                    db.add(DbSession(
                        id=sid,
                        name=f"seeded-{sid}",
                        account_name="test-acct",
                        engine_session_id=f"claude-{sid}",
                        status=status,
                        repo=repo,
                        cwd=str(tmp_path),
                    ))
                    await db.commit()

            asyncio.run(_insert())

        yield orch, seed_session


def test_pr_repos_includes_archived_session_repos(orch_with_db):
    """_pr_repos unions live in-memory repos with persisted (incl. archived) session repos."""
    orch, seed_session = orch_with_db
    # one ARCHIVED session in the DB on repo "owner/archived" (no live record)
    seed_session(repo="owner/archived", status="archived")
    # one LIVE in-memory record on repo "owner/live"
    orch._records["s-live"] = _FakeRecord(id="s-live", name="live", repo="owner/live")

    repos = asyncio.run(orch._pr_repos())
    assert "owner/archived" in repos
    assert "owner/live" in repos
    assert repos == sorted(set(repos))  # sorted + distinct


def test_pr_repos_deduplicates_overlapping_repos(orch_with_db):
    """If a repo appears in both DB and live records, it should appear once."""
    orch, seed_session = orch_with_db
    seed_session(repo="owner/shared", status="working")
    orch._records["s-shared"] = _FakeRecord(id="s-shared", name="shared", repo="owner/shared")

    repos = asyncio.run(orch._pr_repos())
    assert repos.count("owner/shared") == 1


def test_pr_repos_skips_null_repo_rows(orch_with_db):
    """DB rows with repo=None should not appear in the result."""
    orch, seed_session = orch_with_db
    seed_session(repo="owner/real", status="idle")
    # Manually seed a row with no repo
    async def _insert_null():
        from server.db import db_session
        from server.db.models import Session as DbSession
        async with db_session() as db:
            db.add(DbSession(
                id="null-repo-row",
                name="no-repo",
                account_name="test-acct",
                engine_session_id="claude-null",
                status="idle",
                repo=None,
                cwd="/tmp",
            ))
            await db.commit()

    asyncio.run(_insert_null())

    repos = asyncio.run(orch._pr_repos())
    assert "owner/real" in repos
    assert None not in repos
    assert "" not in repos

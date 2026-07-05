"""orchestrator.cicd_file_issue_now — the CicdDoctorActionProvider's file seam (#465).

Exercised against a real SQLite DB via ``__new__`` (the method only touches the
module-global ``db_session`` and a patched ``ci_issues.create_issue``), so the
heavy SessionOrchestrator constructor is skipped.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from sqlalchemy import select

from server.db import db_session, init_db
from server.db.models import CiFinding
from server.sessions.orchestrator import SessionOrchestrator


def _orch():
    return object.__new__(SessionOrchestrator)


def _add_finding(**kw):
    defaults = dict(id="f-1", repo="o/r", kind="flaky_test", fingerprint="fp",
                    status="open", title="flaky test: t::a", detail={"flake_pct": 8.0})
    defaults.update(kw)
    return CiFinding(**defaults)


def test_file_issue_now_files_and_writes_back(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        orch = _orch()
        async with db_session() as db:
            db.add(_add_finding())
        with patch("server.github.ci_issues.create_issue", return_value=99) as m:
            assert await orch.cicd_file_issue_now("f-1") == "filed"
        assert m.called
        async with db_session() as db:
            row = (await db.execute(select(CiFinding).where(
                CiFinding.id == "f-1"))).scalar_one()
            assert row.issue_number == 99
        # Already filed -> exists (idempotent, no duplicate file).
        with patch("server.github.ci_issues.create_issue", return_value=100) as m2:
            assert await orch.cicd_file_issue_now("f-1") == "exists"
        assert not m2.called
    asyncio.run(run())


def test_file_issue_now_unknown_and_failure(tmp_path):
    async def run():
        await init_db(f"sqlite+aiosqlite:///{tmp_path}/t.db")
        orch = _orch()
        assert await orch.cicd_file_issue_now("missing") == "unknown"
        async with db_session() as db:
            db.add(_add_finding())
        with patch("server.github.ci_issues.create_issue", return_value=None):
            assert await orch.cicd_file_issue_now("f-1") == "file_failed"
        async with db_session() as db:  # left unfiled so it can be retried
            row = (await db.execute(select(CiFinding).where(
                CiFinding.id == "f-1"))).scalar_one()
            assert row.issue_number is None
    asyncio.run(run())

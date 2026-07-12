"""Phase-3 tables: timeline, group runs/failures, deploy config (SPEC §1-§3)."""
from __future__ import annotations

import asyncio
import json

from server.db import db_session, init_db
from server.db.models import GroupFailure, GroupRun, MergedPr, RepoDeployConfig

import pytest


@pytest.fixture(autouse=True)
def _clean_phase3_tables():
    """Table-wide asserts + the session-shared conftest DB (and sibling suites
    writing the same repo keys) — wipe the phase-3 tables per test."""
    async def wipe():
        from sqlalchemy import delete
        await init_db()
        async with db_session() as db:
            for model in (GroupRun, GroupFailure, MergedPr, RepoDeployConfig):
                await db.execute(delete(model))
    asyncio.run(wipe())



def test_merged_pr_upsert_semantics():
    async def run():
        await init_db()
        async with db_session() as db:
            row = MergedPr(
                repo="o/r",
                number=7,
                merged_at="2026-07-12T00:00:00Z",
                title="t",
                url="u",
                merge_commit_sha="abc",
            )
            db.add(row)
        async with db_session() as db:
            got = await db.get(MergedPr, ("o/r", 7))
            assert got is not None
            assert got.first_green_at is None
            assert got.qa_live_at is None
            assert got.merged_at == "2026-07-12T00:00:00Z"

    asyncio.run(run())


def test_group_run_and_failure_rows():
    async def run():
        await init_db()
        async with db_session() as db:
            db.add(GroupRun(
                repo="o/r",
                duration_secs=600,
                completed_at="2026-07-12T00:00:00Z",
            ))
            db.add(GroupFailure(
                repo="o/r",
                check_name="e2e",
                group_sha="g1",
                observed_at="2026-07-12T00:00:00Z",
                conclusion="FAILURE",
            ))
        async with db_session() as db:
            from sqlalchemy import select
            runs = (await db.execute(select(GroupRun).where(GroupRun.repo == "o/r"))).scalars().all()
            assert len(runs) == 1
            assert runs[0].duration_secs == 600
            failures = (await db.execute(select(GroupFailure).where(GroupFailure.repo == "o/r"))).scalars().all()
            assert len(failures) == 1
            assert failures[0].check_name == "e2e"

    asyncio.run(run())


def test_repo_deploy_config_roundtrip():
    async def run():
        await init_db()
        envs = [{"name": "qa", "healthUrl": "https://qa/health", "auto": True, "shaKey": "commitSha"}]
        async with db_session() as db:
            db.add(RepoDeployConfig(repo="o/r", environments=json.dumps(envs)))
        async with db_session() as db:
            got = await db.get(RepoDeployConfig, "o/r")
            assert got is not None
            assert json.loads(got.environments)[0]["name"] == "qa"
            assert got.enabled is True

    asyncio.run(run())
